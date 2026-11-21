from copy import deepcopy
import json

import pytest

from research.fhir_profile.analysis import paired_bootstrap, compare_results, call_cost
from research.fhir_profile.pipeline import HybridFhirPipeline, issue_changes
from research.fhir_profile.reference import load_reference_resources
from tests.test_research_validation import patient_profile, patient_batch
from tests.test_research_experiment import setup_experiment, patient_profile as experiment_patient_profile
from research.fhir_profile.experiment import generate_profiles, evaluate_experiment


def test_source_contract_detects_valid_but_wrong_mapping_and_corrects_without_gold():
    profile = patient_profile()
    profile["mappings"][0] = {"id": "gender", "target_path": "gender", "operation": "constant", "value": "female"}
    check = {"id": "source-sex", "target_path": "gender", "operation": "copy", "source_field": "sex"}
    def revise(current, issues, records):
        assert issues[0]["category"] == "source_preservation"
        current["mappings"] = [{**check, "id": "gender"}]
        return current
    result = HybridFhirPipeline(correct=True, source_checks=[check], profile_reviser=revise).run(patient_batch(), profile)
    assert result["resources"][0]["gender"] == "male"
    assert result["metrics"]["source_preservation_before"]["accuracy"] == 0
    assert result["metrics"]["source_preservation_after"]["accuracy"] == 1
    assert result["metrics"]["resolved_error_count"] == 1
    assert result["metrics"]["introduced_error_count"] == 0
    assert result["fidelity"]["status"] == "not_evaluated"


def test_correction_rejects_new_errors_even_when_total_decreases():
    def validator(resources):
        errors = ["old-a", "old-b"] if resources[0]["gender"] == "male" else ["new-c"]
        return {"valid": False, "errors": errors}
    def revise(profile, issues, records):
        profile["mappings"][0].update(operation="lookup", table={"male": "female"})
        return profile
    result = HybridFhirPipeline(correct=True, validator=validator, profile_reviser=revise).run(patient_batch(), patient_profile())
    assert result["resources"][0]["gender"] == "male"
    assert result["metrics"]["accepted_revisions"] == 0
    assert len(result["history"][0]["candidate_issue_changes"]["introduced"]) == 1


def test_error_changes_preserve_multiplicity():
    issue = {"category": "source_preservation", "message": "x"}
    assert len(issue_changes([issue, issue], [issue])["resolved"]) == 1


def test_reference_identifier_alignment_updates_subject_encounter_and_fullurl(tmp_path):
    entries = [
        {"fullUrl": "urn:uuid:p", "resource": {"resourceType": "Patient", "id": "uuid-p", "identifier": [{"system": "urn:source:patient", "value": "17"}]}},
        {"fullUrl": "urn:uuid:e", "resource": {"resourceType": "Encounter", "id": "uuid-e", "identifier": [{"system": "urn:source:visit", "value": "42"}], "subject": {"reference": "urn:uuid:p"}}},
        {"resource": {"resourceType": "Observation", "id": "uuid-o", "subject": {"reference": "Patient/uuid-p"}, "encounter": {"reference": "urn:uuid:e"}}},
    ]
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": entries}))
    result = load_reference_resources(path, patient_keys=[17], resource_types=["Patient", "Encounter", "Observation"],
        identity_rules={"Patient": {"identifier_system": "urn:source:patient"}, "Encounter": {"identifier_system": "urn:source:visit"}})
    assert result[0]["id"] == "17"
    assert result[1]["id"] == "42"
    assert result[1]["subject"]["reference"] == "Patient/17"
    assert result[2]["encounter"]["reference"] == "Encounter/42"


def test_reference_ambiguous_identifier_is_rejected(tmp_path):
    path = tmp_path / "patient.json"
    path.write_text(json.dumps({"resourceType": "Patient", "id": "p", "identifier": [{"system": "urn:p", "value": "1"}, {"system": "urn:p", "value": "2"}]}))
    with pytest.raises(ValueError, match="하나로"):
        load_reference_resources(path, patient_keys=[1], resource_types=["Patient"], identity_rules={"Patient": {"identifier_system": "urn:p"}})


def test_bootstrap_uses_all_patients_and_does_not_drop_missing_outcomes():
    result = paired_bootstrap({"a": 0, "b": 0}, {"a": 1, "b": 1}, resamples=100)
    assert result["mean_difference"] == 1
    assert result["confidence_interval_95"] == [1, 1]
    assert paired_bootstrap({"a": None}, {"a": 1}, resamples=100)["status"] == "not_evaluated"
    assert paired_bootstrap({"a": 0}, {"a": 1}, resamples=100)["confidence_interval_95"] is None
    with pytest.raises(ValueError):
        paired_bootstrap({"a": 0}, {"b": 1})


def test_unknown_tokens_are_not_reported_as_zero():
    assert call_cost([{"kind": "chat", "success": False}])["prompt_tokens"] is None
    assert call_cost([])["prompt_tokens"] == 0


def test_rag_comparison_rejects_other_configuration_changes():
    run = {"dataset": "test", "cohort_sha256": "c", "split_sha256": "s", "policy_sha256": "p",
           "independent_reference_sha256": "r", "policy_config": {"rag": {"enabled": False}, "model": "one"},
           "model_identity": {"digest": "d"}, "code_sha256": "c", "reference_profiles_sha256": None,
           "groups": {"P1": {"patients": {"1": {"source_field_accuracy": 1, "event_f1": 1, "overall_pass": True}}}}}
    other = deepcopy(run)
    other["policy_config"]["rag"]["enabled"] = True
    assert compare_results(run, other, rag_ablation=True)["groups"]
    other["policy_config"]["model"] = "two"
    with pytest.raises(ValueError, match="rag.enabled"):
        compare_results(run, other, rag_ablation=True)


def test_experiment_records_patient_metrics_mapping_and_preparation_cost(tmp_path):
    prepared, cohort, policy, client = setup_experiment(tmp_path)
    profiles = generate_profiles(policy, prepared, client)
    gold = [{"resourceType": "Patient", "id": key, "gender": "female", "birthDate": "1980-01-01"} for key in cohort["patient_keys"]]
    result = evaluate_experiment(policy, cohort, profiles, client, reference_resources=gold,
                                 reference_profiles={"Patient": experiment_patient_profile("synthea")})
    assert result["groups"]["P1"]["mapping_accuracy"]["Patient"]["field_mapping_accuracy"]["f1"] == 1
    assert set(result["groups"]["P2"]["patients"]) == set(cohort["patient_keys"])
    assert result["groups"]["P2"]["fhir_bundle_pass_rate"] == 1
    assert result["cost_summary"]["P1"]["preparation"]["chat_calls"] == 1
    assert result["cost_summary"]["P1"]["evaluation"]["chat_calls"] == 0
    assert result["paired_comparisons"]["B1_vs_P2"]["source_field_accuracy"]["mean_difference"] == 0
    assert result["pilot_summary"]["groups"]["B1"]["patients"] == len(cohort["patient_keys"])


def test_source_contract_allows_replacing_a_wrong_source_column():
    from research.fhir_profile.contracts import SourceBatch
    batch = SourceBatch("synthetic", "1", "patients", "1", ({"patient_id": "p1", "sex": "male", "other": "female"},), {"columns": ["patient_id", "sex", "other"]})
    profile = patient_profile()
    profile["mappings"][0]["source_field"] = "other"
    check = {"id": "source-sex", "target_path": "gender", "operation": "copy", "source_field": "sex"}
    def revise(profile, issues, records):
        profile["mappings"][0]["source_field"] = "sex"
        return profile
    result = HybridFhirPipeline(correct=True, source_checks=[check], profile_reviser=revise).run(batch, profile)
    assert result["metrics"]["accepted_revisions"] == 1
    assert result["resources"][0]["gender"] == "male"


def test_invalid_revision_preserves_original_results_and_audit():
    result = HybridFhirPipeline(correct=True, profile_reviser=lambda *args: {}).run(patient_batch("invalid"), patient_profile())
    assert result["metrics"]["repair_attempts"] == 2
    assert result["history"][0]["changes"][0]["change"].startswith("rejected:")
    assert result["profile"] == patient_profile()


def test_invalid_revision_is_retried_with_contract_feedback():
    seen = []
    def revise(profile, issues, records):
        seen.append(issues)
        if len(seen) == 1:
            return {}
        assert any(issue["category"] == "revision_contract" for issue in issues)
        profile["mappings"][0].update(operation="lookup", table={"invalid": "male"})
        return profile
    result = HybridFhirPipeline(correct=True, profile_reviser=revise).run(patient_batch("invalid"), patient_profile())
    assert result["metrics"]["repair_attempts"] == 2
    assert result["metrics"]["standard_repair_success"]


def test_revision_with_new_blocking_issue_is_retried_with_its_evidence():
    seen = []
    def revise(profile, issues, records):
        seen.append(issues)
        profile["mappings"][0].update(operation="lookup", table={"invalid": "male"})
        if len(seen) == 1:
            profile["mappings"].append({"id": "bad-date", "target_path": "birthDate", "operation": "constant", "value": "not-a-date"})
        else:
            feedback = next(issue for issue in issues if issue["category"] == "revision_contract")
            assert feedback["introduced"]
            assert "bad-date" in {mapping["id"] for mapping in feedback["draft"]["mappings"]}
        return profile
    result = HybridFhirPipeline(correct=True, profile_reviser=revise).run(patient_batch("invalid"), patient_profile())
    assert result["metrics"]["repair_attempts"] == 2
    assert result["metrics"]["accepted_revisions"] == 1
    assert result["metrics"]["standard_repair_success"]


def test_targeted_output_schema_disallows_renderer_managed_fields_and_unknown_columns():
    from jsonschema import Draft202012Validator
    from research.fhir_profile.profiles.output_schema import profile_output_schema
    validator = Draft202012Validator(profile_output_schema("Patient", patient_batch().metadata))
    validator.validate(patient_profile())
    for field, value in [("target_path", "id"), ("source_field", "undocumented")]:
        broken = patient_profile()
        broken["mappings"][0][field] = value
        assert list(validator.iter_errors(broken))


def test_runtime_checks_backend_profile_shape_and_truncation():
    from tests.test_research_runtime import config
    from research.fhir_profile.runtime import ResearchOllamaClient
    client = ResearchOllamaClient(config())
    client._post = lambda *args: {"message": {"content": '{"bad": true}'}, "done_reason": "stop", "eval_count": 3}
    with pytest.raises(ValueError, match="output contract"):
        client.chat_profile("system", json.dumps({"target_resource": "Patient", "source_metadata": patient_batch().metadata}))
    assert client.calls[-1]["success"] is False
    client._post = lambda *args: {"message": {"content": '{}'}, "done_reason": "length"}
    with pytest.raises(ValueError, match="절단"):
        client.chat("system", "input")


def test_checkpoints_preserve_generation_and_each_evaluated_group(tmp_path):
    prepared, cohort, policy, client = setup_experiment(tmp_path)
    checkpoints = []
    profiles = generate_profiles(policy, prepared, client, on_checkpoint=checkpoints.append)
    assert [event["stage"] for event in checkpoints] == ["initial", "correction"]
    assert all("metadata" not in event["profile"] for event in checkpoints)
    checkpoints.clear()
    evaluate_experiment(policy, cohort, profiles, client, groups=("P1", "P2"), on_checkpoint=checkpoints.append)
    assert [event["group"] for event in checkpoints] == ["P1", "P2"]
    assert all(event["complete"] is False for event in checkpoints)


def test_direct_batch_failure_preserves_successful_batches():
    from research.fhir_profile.contracts import SourceBatch
    from research.fhir_profile.experiment import _direct_generate
    class Client:
        count = 0
        def chat(self, *args):
            self.count += 1
            if self.count == 2:
                raise RuntimeError("fixture timeout")
            return json.dumps({"resources": [{"resourceType": "Patient", "id": str(self.count)}]})
    batch = SourceBatch("fixture", "1", "patients", "1", ({"id": 1}, {"id": 2}, {"id": 3}), {"columns": ["id"]})
    policy = {"config": {"generation": {"batch_size": 1}, "prompt_texts": {"direct": "fixture"}}}
    errors = []
    result = _direct_generate(batch, "Patient", "FHIR", policy, Client(), errors=errors)
    assert [resource["id"] for resource in result] == ["1", "3"]
    assert len(errors) == 1


def test_atomic_json_write_keeps_existing_file_on_invalid_content(tmp_path):
    from research.fhir_profile.artifacts import write_json
    destination = tmp_path / "result.json"
    write_json(destination, {"complete": True})
    with pytest.raises(ValueError):
        write_json(destination, {"bad": float("nan")})
    assert json.loads(destination.read_text()) == {"complete": True}


def test_decoder_schema_has_no_union_or_recursive_definitions():
    from jsonschema import Draft202012Validator
    from research.fhir_profile.profiles.output_schema import profile_generation_schema
    schema = profile_generation_schema("Patient", patient_batch().metadata)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(patient_profile())
    def visit(value):
        if isinstance(value, dict):
            assert not ({"anyOf", "oneOf", "$defs", "$ref"} & value.keys())
            for nested in value.values(): visit(nested)
        elif isinstance(value, list):
            for nested in value: visit(nested)
    visit(schema)
    keys = list(schema["properties"]["mappings"]["items"]["properties"])
    assert keys[:3] == ["id", "target_path", "operation"]
    assert keys.index("resource_type") < keys.index("source_field") < keys.index("code")
    assert "string" in schema["properties"]["mappings"]["items"]["properties"]["value"]["type"]


def test_thinking_setting_is_forwarded_for_reasoning_models():
    from tests.test_research_runtime import config
    from research.fhir_profile.runtime import ResearchOllamaClient
    settings = config()
    settings["model"]["think"] = "low"
    client = ResearchOllamaClient(settings)
    requests = []
    def post(endpoint, payload):
        requests.append(payload)
        return {"message": {"content": '{}'}}
    client._post = post
    client.chat("system", "input")
    assert requests[0]["think"] == "low"


def test_streaming_fingerprint_preserves_existing_artifact_hashes():
    import hashlib
    from research.fhir_profile.artifacts import fingerprint
    value = {"한글": [1, 1.5, None, True, {"key": "value"}], "vectors": [[0.1, 0.2]] * 100}
    expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    assert fingerprint(value) == expected


def test_correction_prompt_groups_repeated_rows_without_losing_occurrence_counts():
    from research.fhir_profile.agents.mapping import summarize_issues
    issues = [
        {"category": "source_preservation", "check_id": "numeric", "path": "valueQuantity.value",
         "message": f"record[{index}] different amount", "expected_value": index, "actual_value": None}
        for index in range(300)
    ]
    feedback = {"category": "revision_contract", "message": "invalid mapping", "draft": {}}
    summary = summarize_issues([*issues, feedback])
    assert summary["issue_count"] == 300
    assert summary["distinct_issue_count"] == 1
    assert summary["groups"][0]["count"] == 300
    assert len(summary["groups"][0]["examples"]) == 2
    assert summary["previous_revision_errors"] == [feedback]


def test_hl7_structured_diagnostics_preserve_distinct_local_references():
    def validator(resources):
        local = "JSON Schema gender: 'invalid' is not one of ['male', 'female', 'other', 'unknown']"
        missing_reference = "Patient/p1 reference target is unavailable"
        return {
            "valid": False,
            "errors": [local, missing_reference, "HL7 error"],
            "local_errors": [local, missing_reference],
            "issues": [{"category": "validator", "path": "Patient.gender", "message": "HL7 error"}],
        }
    result = HybridFhirPipeline(validator=validator).run(patient_batch("invalid"), patient_profile())
    messages = [issue["message"] for issue in result["issues"]]
    assert sum("JSON Schema gender:" in message for message in messages) == 1
    assert "Patient/p1 reference target is unavailable" in messages
    assert "HL7 error" in messages


def test_hl7_timeout_is_reported_without_fabricating_an_issue(tmp_path, monkeypatch):
    import subprocess
    from fhir_agent_pipeline.validation import _run_external_validator
    jar = tmp_path / "validator.jar"
    jar.touch()
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr("fhir_agent_pipeline.validation.subprocess.run", timeout)
    report = _run_external_validator({}, jar, "4.0.1", 3, 20)
    assert report.status == "timeout"
    assert report.used and sum(report.issue_counts.values()) == 0
    assert report.errors == ["HL7 Validator 시간 초과(3초)"]


def test_hl7_timeout_does_not_trigger_a_profile_revision_by_itself():
    def validator(resources):
        return {"valid": False, "errors": ["run timeout"], "local_errors": [],
                "external_validation_status": "timeout"}
    def revise(*args):
        pytest.fail("A model revision cannot repair Validator availability")
    result = HybridFhirPipeline(correct=True, validator=validator, profile_reviser=revise).run(
        patient_batch(), patient_profile())
    assert result["metrics"]["repair_attempts"] == 0
    assert not result["metrics"]["standard_valid"]
    assert result["issues"][0]["category"] == "validator_execution"


def test_timezone_policy_is_applied_during_profile_preparation_and_evaluation(tmp_path):
    from research.fhir_profile.artifacts import fingerprint, freeze_policy
    prepared, cohort, policy, client = setup_experiment(tmp_path)
    for data in (prepared, cohort):
        payload = data["resources"]["Patient"]
        payload["metadata"]["columns"].append("death_timestamp")
        for row in payload["records"]:
            row["death_timestamp"] = "2020-01-01T12:00:00"
        data["cohort_sha256"] = fingerprint({key: value for key, value in data.items() if key != "cohort_sha256"})
    config = deepcopy(policy["config"])
    config["generation"]["timezone"] = "Z"
    policy = freeze_policy(config, model_identity=client.identity(), rag_identity={"enabled": False})
    original_chat = client.chat
    def chat(system, prompt):
        payload = json.loads(prompt)
        answer = json.loads(original_chat(system, prompt))
        if "source_records" not in payload:
            answer["mappings"].append({"id": "death", "target_path": "deceasedDateTime", "operation": "datetime", "source_field": "death_timestamp"})
        return json.dumps(answer)
    client.chat = chat
    profiles = generate_profiles(policy, prepared, client)
    assert profiles["profiles"]["Patient"]["calibration_metrics"]["standard_valid"]
    result = evaluate_experiment(policy, cohort, profiles, client, groups=("P1", "P2", "B1"))
    assert result["groups"]["P2"]["validation"]["valid"]
    assert all(resource["deceasedDateTime"].endswith("Z") for resource in result["groups"]["P2"]["resources"])
    for prompt in client.prompts:
        assert prompt["source_metadata"]["datetime_timezone"] == "Z"
        assert "Patient" in prompt["source_metadata"]["related_sources"]


def test_validator_issues_are_parsed_before_output_truncation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fhir_agent_pipeline.validation import _run_external_validator
    jar = tmp_path / "validator.jar"
    jar.write_bytes(b"fixture")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1, stdout="[1, 30] Bundle.entry[0].resource.gender: Error - Invalid code\n" + "x" * 200, stderr="")
    monkeypatch.setattr("fhir_agent_pipeline.validation.subprocess.run", run)
    result = _run_external_validator({}, jar, "4.0.1", 10, 20, terminology_server="n/a")
    assert result.issue_counts["error"] == 1
    assert result.issues[0]["path"] == "Bundle.entry[0].resource.gender"
    assert result.issues[0]["message"] == "Invalid code"
    assert commands[0][-2:] == ["-tx", "n/a"]


def test_mapping_label_changes_do_not_create_a_new_error():
    old = {"category": "validator", "mapping_id": "old-label", "message": "same failure"}
    new = {**old, "mapping_id": "new-label"}
    result = issue_changes([old], [new])
    assert result["remaining"] == 1
    assert result["introduced"] == []


def test_generation_failure_is_retained_and_empty_bundles_cannot_pass(tmp_path):
    prepared, cohort, policy, client = setup_experiment(tmp_path)
    def fail(*args):
        client.calls.append({"kind": "chat", "success": False})
        raise RuntimeError("fixture connection failure")
    client.chat = fail
    profiles = generate_profiles(policy, prepared, client)
    assert profiles["profiles"]["Patient"]["status"] == "profile_generation_failed"
    result = evaluate_experiment(policy, cohort, profiles, client, groups=("P1",))
    assert result["groups"]["P1"]["fhir_bundle_pass_rate"] == 0
    assert all(patient["validation"]["validation_level"] == "not_generated" for patient in result["groups"]["P1"]["patients"].values())
    assert result["cost_summary"]["P1"]["preparation"]["failed_calls"] == policy["config"]["generation"]["max_attempts"]


def test_model_profile_output_schema_requires_real_operation_fields():
    from jsonschema import Draft202012Validator
    from research.fhir_profile.profiles.output_schema import profile_output_schema
    schema = profile_output_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(patient_profile())
    broken = patient_profile()
    broken["mappings"][0].pop("source_field")
    assert list(validator.iter_errors(broken))


def test_mapping_agent_uses_structured_profile_capability():
    from research.fhir_profile.agents.mapping import ProfileMappingAgent
    class Client:
        def chat(self, *args):
            raise AssertionError("structured profile method must be used")
        def chat_profile(self, *args):
            return json.dumps(patient_profile())
    assert ProfileMappingAgent(Client()).generate(patient_batch().metadata, "FHIR", "Patient") == patient_profile()
