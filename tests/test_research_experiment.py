from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from research.fhir_profile.artifacts import ROOT, fingerprint, freeze_policy, load_experiment_config, verify_policy, write_json
from research.fhir_profile.cohorts import create_cohort_lock, verify_locked_cohort
from research.fhir_profile.data.extractors import CSVRecordExtractor
from research.fhir_profile.experiment import evaluate_experiment, generate_profiles, prepare_cohort, verify_cohort, verify_runtime, save_evaluation
from research.fhir_profile.reference import load_reference_resources
from research.fhir_profile.source_rag import SourceRetriever, build_source_index
from research.fhir_profile.splits import create_fixed_patient_split, create_patient_split, validate_patient_split


def patient_profile(dataset):
    return {"profile_id": f"{dataset}-patient", "profile_version": "1", "target_resource": "Patient",
            "fhir_version": "4.0.1", "source": {"dataset": dataset, "table": "patients"},
            "identity": {"source_fields": ["id"]}, "mappings": [
                {"id": "gender", "target_path": "gender", "operation": "lookup", "source_field": "gender",
                 "table": {"F": "female", "M": "male"}, "fallback": "error"},
                {"id": "birth", "target_path": "birthDate", "operation": "date", "source_field": "birthdate"},
            ]}


class FakeClient:
    """실제 외부평가 성적을 만들지 않는, 배선 검증용 응답기."""
    embedding_model = "nomic-embed-text"

    def __init__(self):
        self.calls = []
        self.prompts = []

    def identity(self, model=None):
        return {"name": model or "gemma4:e4b", "digest": "fixture"}

    def chat(self, system, prompt):
        payload = json.loads(prompt)
        self.prompts.append(payload)
        self.calls.append({"kind": "chat", "success": True, "prompt_tokens": 10, "output_tokens": 10})
        if "source_records" in payload:
            return json.dumps({"resources": [{"resourceType": "Patient", "id": row["id"],
                                             "gender": {"F": "female", "M": "male"}[row["gender"]],
                                             "birthDate": row["birthdate"]} for row in payload["source_records"]]})
        return json.dumps(patient_profile(payload["source_metadata"]["dataset"]))


def setup_experiment(tmp_path, dataset="synthea", *, unsafe_keys=False):
    keys = [f"patient:{i}" if unsafe_keys else f"p-{i}" for i in range(10)]
    path = tmp_path / "patients.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Id", "Gender", "BirthDate"])
        writer.writerows([[key, "F", "1980-01-01"] for key in keys])
    spec = {"dataset": dataset, "dataset_version": "fixture", "schema_version": "v1", "resources": {
        "Patient": {"source_resource": "patients", "table": "patients", "columns": ["id", "gender", "birthdate"],
                    "primary_keys": ["id"], "identity_fields": ["id"], "patient_column": "id"}}}
    split = create_patient_split(keys, dataset, seed=12)
    extractor = CSVRecordExtractor(tmp_path, spec)
    role = "calibration" if dataset == "synthea" else "development"
    prepared = prepare_cohort(spec, split, role, extractor)
    held_out = prepare_cohort(spec, split, "test", extractor)
    config = load_experiment_config(ROOT / "experiments/configs/pilot.json")
    config["resources"] = ["Patient"]
    config["rag"]["enabled"] = False
    config["validation"].update({"validator_jar": None, "require_hl7": False})
    client = FakeClient()
    policy = freeze_policy(config, model_identity=client.identity(), rag_identity={"enabled": False})
    return prepared, held_out, policy, client


def test_split_prepare_generate_and_external_evaluate_without_sql(tmp_path):
    calibration, test, policy, client = setup_experiment(tmp_path)
    profiles = generate_profiles(policy, calibration, client)
    profile_before = deepcopy(profiles)
    gold = [{"resourceType": "Patient", "id": key, "gender": "female", "birthDate": "1980-01-01"}
            for key in test["patient_keys"]]
    result = evaluate_experiment(policy, test, profiles, client, reference_resources=gold)
    assert result["evaluation_kind"] == "synthetic_external"
    assert all(group["overall_pass"] for group in result["groups"].values())
    assert len(result["groups"]["P1"]["calls"]) == len(result["groups"]["P2"]["calls"]) == 0
    assert result["groups"]["B1"]["calls"]
    assert profiles == profile_before
    assert len(client.prompts[0]["source_metadata"]["sample_records"]) <= 3
    assert "gold" not in json.dumps(client.prompts[0])
    save_evaluation(result, tmp_path / "result")
    assert (tmp_path / "result/manifest.json").is_file()
    with pytest.raises(ValueError, match="비어"):
        save_evaluation(result, tmp_path / "result")


def test_evaluation_patients_cannot_generate_profiles(tmp_path):
    calibration, test, policy, client = setup_experiment(tmp_path)
    with pytest.raises(ValueError, match="평가 환자"):
        generate_profiles(policy, test, client)
    assert not client.calls


@pytest.mark.parametrize(("dataset", "partitions"), [
    ("mimic_iv", {"development": [9], "validation": [], "test": [18, 3]}),
    ("synthea", {"calibration": ["p-a"], "test": ["p-c", "p-b"]}),
])
def test_fixed_patient_split_reuses_roles_without_dependence_on_input_order(dataset, partitions):
    fixed = create_fixed_patient_split(partitions, dataset)
    assert fixed == create_fixed_patient_split({role: list(reversed(keys)) for role, keys in partitions.items()}, dataset)
    assert validate_patient_split(fixed) == fixed
    corrupted = deepcopy(fixed)
    corrupted["roles"]["test"] = list(reversed(corrupted["roles"]["test"]))
    with pytest.raises(ValueError, match="고정 환자"):
        validate_patient_split(corrupted)
    prep = next(role for role in partitions if role in {"development", "calibration"})
    with pytest.raises(ValueError, match="중복"):
        create_fixed_patient_split({**partitions, "test": [str(partitions[prep][0])]}, dataset)
    with pytest.raises(ValueError, match="평가 환자"):
        create_fixed_patient_split({**partitions, "test": []}, dataset)


@pytest.mark.parametrize("dataset", ["mimic_iv", "synthea"])
def test_locked_cohort_checks_membership_snapshot_reference_and_profile_origin(tmp_path, dataset):
    prep, evaluation, policy, client = setup_experiment(tmp_path, dataset)
    profiles = generate_profiles(policy, prep, client)
    gold = [{"resourceType": "Patient", "id": str(key), "gender": "female", "birthDate": "1980-01-01"}
            for key in evaluation["patient_keys"]]
    gold_profiles = {"Patient": patient_profile(dataset)}
    lock = create_cohort_lock(prep, evaluation, reference_resources=gold, reference_profiles=gold_profiles)
    assert lock == create_cohort_lock(prep, evaluation, reference_resources=gold, reference_profiles=gold_profiles)
    assert "patient_keys" not in json.dumps(lock)
    verify_locked_cohort(lock, prep, "preparation")
    result = evaluate_experiment(policy, evaluation, profiles, client, reference_resources=gold,
                                 reference_profiles=gold_profiles, groups=("P1",), cohort_lock=lock)
    assert result["cohort_lock_sha256"] == lock["cohort_lock_sha256"]
    assert result["groups"]["P1"]["mapping_accuracy"]["Patient"]["field_mapping_accuracy"]["accuracy"] == 1
    changed = deepcopy(evaluation)
    changed["resources"]["Patient"]["records"][0]["gender"] = "M"
    changed["cohort_sha256"] = fingerprint({k: v for k, v in changed.items() if k != "cohort_sha256"})
    with pytest.raises(ValueError, match="원천 자료"):
        verify_locked_cohort(lock, changed, "evaluation", reference_resources=gold, reference_profiles=gold_profiles)
    with pytest.raises(ValueError, match="독립 참조 FHIR"):
        verify_locked_cohort(lock, evaluation, "evaluation", reference_profiles=gold_profiles)
    with pytest.raises(ValueError, match="독립 매핑 정답"):
        verify_locked_cohort(lock, evaluation, "evaluation", reference_resources=gold)
    with pytest.raises(ValueError, match="준비 원천"):
        verify_locked_cohort(lock, evaluation, "evaluation", reference_resources=gold, reference_profiles=gold_profiles,
                             profiles={**profiles, "preparation_cohort_sha256": "other-preparation"})


def test_matched_schema_only_source_rag_preparation_and_evaluation(tmp_path):
    from research.fhir_profile.analysis import compare_results

    prep, evaluation, prior_policy, client = setup_experiment(tmp_path)
    spec = {"dataset": "synthea", "dataset_version": "fixture", "schema_version": "v1", "resources": {
        "Patient": {"source_resource": "patients", "table": "patients", "columns": ["id", "gender", "birthdate"],
                    "primary_keys": ["id"], "identity_fields": ["id"], "patient_column": "id"}}}
    fhir_identity = {"kind": "fhir_schema_only", "index_sha256": "a" * 64,
                     "schema_sha256": prior_policy["fhir_schema_sha256"],
                     "embedding_identity": client.identity(client.embedding_model)}

    class StubFhirRetriever:
        identity = fhir_identity

        def context(self, resource_type):
            return f"FHIR excerpt for {resource_type}"

    config = deepcopy(prior_policy["config"])
    config["generation"]["sample_rows"] = 0
    config["rag"].update({"enabled": True, "source_contract": True, "source_enabled": False})
    fhir_only = freeze_policy(deepcopy(config), model_identity=client.identity(), rag_identity=fhir_identity)
    config["rag"]["source_enabled"] = True
    dual = freeze_policy(config, model_identity=client.identity(), rag_identity=fhir_identity)
    client.embed_documents = lambda docs: [[1.0, float(len(doc))] for doc in docs]
    client.embed_query = lambda text: [1.0, float(len(text))]
    index = build_source_index(spec, config, client)
    index_path = tmp_path / "source-index.json"
    write_json(index_path, index)
    source = SourceRetriever(index_path, client)
    fhir = StubFhirRetriever()
    with pytest.raises(ValueError, match="--source-index"):
        generate_profiles(dual, prep, client, fhir, source_spec=spec)
    assert not client.prompts
    baseline_profiles = generate_profiles(fhir_only, prep, client, fhir, source_spec=spec)
    dual_profiles = generate_profiles(dual, prep, client, fhir, source_spec=spec, source_retriever=source)
    a, b = client.prompts[:2]
    assert a["source_metadata"] == b["source_metadata"]
    assert a["fhir_context"] == b["fhir_context"]
    assert "sample_records" not in b["source_metadata"] and b["source_metadata"]["columns"] == ["id", "gender", "birthdate"]
    assert all(patient not in json.dumps(b) for patient in prep["patient_keys"])
    assert a["source_context"] == {key: value for key, value in b["source_context"].items() if key != "retrieved_documents"}
    assert b["source_context"]["retrieved_documents"]
    assert dual_profiles["source_rag_identity"]["index_sha256"] == index["index_sha256"]
    gold = [{"resourceType": "Patient", "id": str(key), "gender": "female", "birthDate": "1980-01-01"}
            for key in evaluation["patient_keys"]]
    inputs = {"groups": ("P1", "P2"), "reference_resources": gold, "source_spec": spec}
    left = evaluate_experiment(fhir_only, evaluation, baseline_profiles, client, fhir, **inputs)
    right = evaluate_experiment(dual, evaluation, dual_profiles, client, fhir, **inputs, source_retriever=source)
    assert compare_results(left, right, source_rag_ablation=True)["groups"]["P2"]["source_field_accuracy"]["mean_difference"] == 0
    right["policy_config"] = deepcopy(right["policy_config"])
    right["policy_config"]["correction"]["max_iterations"] += 1
    with pytest.raises(ValueError, match="source_enabled"):
        compare_results(left, right, source_rag_ablation=True)
    changed = {**index, "vectors": [[x * 2 for x in vector] for vector in index["vectors"]]}
    changed["index_sha256"] = fingerprint({k: v for k, v in changed.items() if k != "index_sha256"})
    changed_path = tmp_path / "changed-index.json"
    write_json(changed_path, changed)
    with pytest.raises(ValueError, match="준비와 평가"):
        evaluate_experiment(dual, evaluation, dual_profiles, client, fhir, **inputs,
                            source_retriever=SourceRetriever(changed_path, client))


def test_managed_id_and_patient_partition_ignore_model_identity_choice(tmp_path):
    prep, test, old, client = setup_experiment(tmp_path, unsafe_keys=True)
    config = deepcopy(old["config"])
    config["generation"]["managed_identity"] = True
    policy = freeze_policy(config, model_identity=client.identity(), rag_identity={"enabled": False})

    original = client.chat
    def bad_identity(system, prompt):
        profile = json.loads(original(system, prompt))
        profile["identity"] = {"source_fields": ["gender"]}
        return json.dumps(profile)

    client.chat = bad_identity
    profiles = generate_profiles(policy, prep, client)
    assert profiles["record_identity_contract"] == {"Patient": {"source_fields": ["id"]}}
    assert profiles["profiles"]["Patient"]["initial"]["identity"] == {"source_fields": ["id"]}
    result = evaluate_experiment(policy, test, profiles, client, groups=("P1",))
    group = result["groups"]["P1"]
    assert group["resource_count"] == len(test["patient_keys"])
    assert all(":" not in r["id"] for r in group["resources"])
    assert all(p["resource_count_alignment"]["aligned"] for p in group["patients"].values())


def test_no_gold_is_not_evaluated_and_changed_spec_is_rejected(tmp_path):
    calibration, test, policy, client = setup_experiment(tmp_path)
    profiles = generate_profiles(policy, calibration, client)
    result = evaluate_experiment(policy, test, profiles, client, groups=("P1",))
    assert result["groups"]["P1"]["overall_pass"] is None
    assert result["groups"]["P1"]["independent_reference_evaluation"]["status"] == "not_evaluated"
    test["extract_spec_sha256"] = "different"
    test["cohort_sha256"] = fingerprint({k: v for k, v in test.items() if k != "cohort_sha256"})
    with pytest.raises(ValueError, match="추출 명세"):
        evaluate_experiment(policy, test, profiles, client)


def test_artifact_corruption_and_empty_cohort_are_rejected(tmp_path):
    calibration, test, policy, client = setup_experiment(tmp_path)
    profiles = generate_profiles(policy, calibration, client)
    corrupt = deepcopy(policy)
    corrupt["config"]["model"]["seed"] += 1
    with pytest.raises(ValueError, match="hash"):
        verify_policy(corrupt)
    test["resources"] = {}
    test["cohort_sha256"] = fingerprint({k: v for k, v in test.items() if k != "cohort_sha256"})
    with pytest.raises(ValueError, match="Patient"):
        verify_cohort(test)
    with pytest.raises(ValueError):
        evaluate_experiment(policy, test, profiles, client, reference_resources=[])


def test_mimic_and_synthea_profiles_cannot_be_interchanged(tmp_path):
    calibration, test, policy, client = setup_experiment(tmp_path)
    profiles = generate_profiles(policy, calibration, client)
    profiles["profiles"]["Patient"]["initial"]["source"]["table"] = "another_table"
    profiles["profiles_sha256"] = fingerprint({k: v for k, v in profiles.items() if k != "profiles_sha256"})
    result = evaluate_experiment(policy, test, profiles, client, groups=("P1",))
    assert not result["groups"]["P1"]["validation"]["valid"]
    assert result["groups"]["P1"]["resource_count"] == 0


def test_reference_loader_normalizes_urns_and_filters_patients(tmp_path):
    reference = {"resourceType": "Bundle", "entry": [
        {"fullUrl": "urn:uuid:p", "resource": {"resourceType": "Patient", "id": "p"}},
        {"fullUrl": "urn:uuid:e", "resource": {"resourceType": "Encounter", "id": "e", "subject": {"reference": "urn:uuid:p"}}},
        {"resource": {"resourceType": "Patient", "id": "other"}},
    ]}
    write_json(tmp_path / "reference.json", reference)
    result = load_reference_resources(tmp_path / "reference.json", patient_keys=["p"], resource_types=["Patient", "Encounter"])
    assert len(result) == 2
    assert result[1]["subject"]["reference"] == "Patient/p"


def test_model_digest_drift_is_rejected(tmp_path):
    _, _, policy, client = setup_experiment(tmp_path)
    client.identity = lambda model=None: {"name": "gemma4:e4b", "digest": "changed"}
    with pytest.raises(ValueError, match="모델"):
        verify_runtime(policy, client, None)


def test_four_resource_calibration_correction_and_held_out_reuse(tmp_path):
    """네 리소스 배선·교정·새 환자 적용 검사. 실제 Synthea 성능 실험은 아닙니다."""
    spec = json.loads((ROOT / "research/fhir_profile/data/specs/synthea.json").read_text())
    keys = [f"p-{i}" for i in range(10)]
    rows = {
        "Patient": [{"id": p, "gender": "F", "birthdate": "1980-01-01", "deathdate": ""} for p in keys],
        "Encounter": [{"id": f"e-{p}", "patient": p, "encounterclass": "ambulatory", "start": "2020-01-01T10:00:00Z", "stop": "2020-01-01T11:00:00Z", "code": "visit", "description": "Visit"} for p in keys],
        "Condition": [{"patient": p, "encounter": f"e-{p}", "code": "c", "system": "http://snomed.info/sct", "description": "Condition", "start": "2020-01-01", "stop": ""} for p in keys],
        "Observation": [{"patient": p, "encounter": f"e-{p}", "code": "o", "description": "Result", "category": "laboratory", "date": "2020-01-01T10:30:00Z", "value": "1.2" if t == "numeric" else "negative", "units": "mg/dL" if t == "numeric" else "", "type": t} for p in keys for t in ("numeric", "text")],
    }
    for kind, source_rows in rows.items():
        with (tmp_path / spec["resources"][kind]["csv_file"]).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=spec["resources"][kind]["columns"])
            writer.writeheader()
            writer.writerows(source_rows)
    split = create_patient_split(keys, "synthea")
    extractor = CSVRecordExtractor(tmp_path, spec)
    calibration = prepare_cohort(spec, split, "calibration", extractor)
    test = prepare_cohort(spec, split, "test", extractor)
    config = load_experiment_config(ROOT / "experiments/configs/pilot.json")
    config["rag"]["enabled"] = False
    config["validation"].update({"require_hl7": False, "validator_jar": None})

    def profile(kind):
        if kind == "Patient":
            return patient_profile("synthea")
        def ref(field, target):
            return {"id": field, "target_path": field, "operation": "reference", "source_field": target, "resource_type": "Patient" if field == "subject" else "Encounter"}
        mappings = [ref("subject", "patient")]
        if kind == "Encounter":
            mappings += [{"id": "status", "target_path": "status", "operation": "constant", "value": "finished"},
                         {"id": "class", "target_path": "class", "operation": "coding", "system": {"operation": "constant", "value": "http://terminology.hl7.org/CodeSystem/v3-ActCode"}, "code": {"operation": "lookup", "source_field": "encounterclass", "table": {"ambulatory": "AMB"}}},
                         {"id": "start", "target_path": "period.start", "operation": "datetime", "source_field": "start"},
                         {"id": "stop", "target_path": "period.end", "operation": "datetime", "source_field": "stop"}]
            identity = "id"
        else:
            mappings += [ref("encounter", "encounter"), {"id": "code", "target_path": "code", "operation": "codeable_concept", "code": {"source_field": "code", "operation": "string"}}]
            identity = "_source_record_id"
            if kind == "Condition":
                mappings += [{"id": "onset", "target_path": "onsetDateTime", "operation": "date", "source_field": "start"}]
            else:
                mappings += [
                    {"id": "time", "target_path": "effectiveDateTime", "operation": "datetime", "source_field": "date"},
                    {"id": "number", "target_path": "valueQuantity.value", "operation": "decimal", "source_field": "value", "when": {"field": "type", "equals": "numeric"}},
                    {"id": "text", "target_path": "valueString", "operation": "string", "source_field": "value", "when": {"field": "type", "equals": "text"}},
                ]
        return {"profile_id": f"fixture-{kind}", "profile_version": "1", "target_resource": kind, "fhir_version": "4.0.1", "source": {"dataset": "synthea", "table": spec["resources"][kind]["source_resource"]}, "identity": {"source_fields": [identity]}, "mappings": mappings}

    class FourResourceClient(FakeClient):
        def chat(self, system, prompt):
            payload = json.loads(prompt)
            self.calls.append({"kind": "chat", "success": True})
            if "current_profile" in payload:
                revised = deepcopy(payload["current_profile"])
                revised["mappings"].append({"id": "status", "target_path": "status", "operation": "constant", "value": "unknown"})
                return json.dumps(revised)
            return json.dumps(profile(payload["target_resource"]))

    client = FourResourceClient()
    policy = freeze_policy(config, model_identity=client.identity(), rag_identity={"enabled": False})
    profiles = generate_profiles(policy, calibration, client)
    assert profiles["profiles"]["Observation"]["calibration_metrics"]["accepted_revisions"] == 1
    calls_before = len(client.calls)
    result = evaluate_experiment(policy, test, profiles, client, groups=("P1", "P2"))
    assert len(client.calls) == calls_before
    assert not result["groups"]["P1"]["validation"]["valid"]
    assert result["groups"]["P2"]["validation"]["valid"]
    assert result["groups"]["P2"]["resource_count"] == len(test["patient_keys"]) * 5
    observations = [r for r in result["groups"]["P2"]["resources"] if r["resourceType"] == "Observation"]
    assert sum("valueQuantity" in r for r in observations) == len(test["patient_keys"])
    assert sum("valueString" in r for r in observations) == len(test["patient_keys"])
