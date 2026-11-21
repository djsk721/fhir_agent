"""고정 정책으로 Profile을 개발하고 별도 환자에서 비교하는 연구 실행기."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from fhir_agent_pipeline.fhir_models import build_bundle
from fhir_agent_pipeline.fhir_schema import get_fhir_schema
from fhir_agent_pipeline.json_utils import load_llm_json, normalize_database_value
from fhir_agent_pipeline.validation import _run_external_validator
from fhir_agent_pipeline.resource_registry import adapter_for, resource_semantic_errors

from .agents.mapping import ProfileMappingAgent, select_profile_examples
from .artifacts import ROOT, exact_fhir_constraints, fingerprint, verify_policy, write_json
from .contracts import SourceBatch
from .pipeline import HybridFhirPipeline, mapping_accuracy
from .analysis import call_cost, within_run_comparisons, pilot_summary
from .profiles.schema import canonical_identity, validate_profile
from .runtime import FhirRetriever, ResearchOllamaClient
from .splits import patient_keys_for, validate_patient_split


def prepare_cohort(spec: dict[str, Any], split: dict[str, Any], role: str, extractor: Any) -> dict[str, Any]:
    validate_patient_split(split)
    if spec["dataset"] != split["dataset"]:
        raise ValueError("추출 명세와 split의 dataset이 다릅니다.")
    keys = patient_keys_for(split, role)
    if not keys:
        raise ValueError(f"{role}에 환자가 없습니다.")
    resources = {}
    allowed = {str(key) for key in keys}
    for resource_type, entry in spec["resources"].items():
        batch = extractor.extract(resource_type, patient_keys=keys)
        if batch.extraction.get("truncated"):
            raise ValueError("원천 데이터가 LIMIT 등으로 누락되었습니다. 완전한 추출 명세를 사용하세요.")
        patient_field = entry["patient_column"].lower()
        rows = []
        for row in batch.records:
            normalized = {str(name): normalize_database_value(value) for name, value in row.items()}
            if str(normalized.get(patient_field)) not in allowed:
                raise ValueError("추출 결과에 분할 범위를 벗어난 환자가 있습니다.")
            rows.append(normalized)
        payload = batch.as_dict()
        payload["records"] = rows
        payload["metadata"] = {**payload["metadata"], "patient_field": patient_field}
        resources[resource_type] = payload
    actual = {str(row[resources["Patient"]["metadata"]["patient_field"]]) for row in resources["Patient"]["records"]}
    if actual != allowed:
        raise ValueError("Patient 추출 결과가 split의 환자 목록과 일치하지 않습니다.")
    payload = {"dataset": spec["dataset"], "role": role, "patient_keys": keys,
               "split": split, "extract_spec_sha256": fingerprint(spec), "resources": resources}
    return {**payload, "cohort_sha256": fingerprint(payload)}


def verify_cohort(cohort: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in cohort.items() if key != "cohort_sha256"}
    if fingerprint(unsigned) != cohort.get("cohort_sha256"):
        raise ValueError("Source Records의 hash가 일치하지 않습니다.")
    validate_patient_split(cohort["split"])
    if cohort["dataset"] != cohort["split"]["dataset"]:
        raise ValueError("cohort와 split의 dataset이 다릅니다.")
    if cohort["patient_keys"] != patient_keys_for(cohort["split"], cohort["role"]):
        raise ValueError("cohort 환자와 split role이 일치하지 않습니다.")
    allowed = {str(key) for key in cohort["patient_keys"]}
    resources = cohort.get("resources")
    if not allowed or not isinstance(resources, dict) or "Patient" not in resources:
        raise ValueError("비어 있지 않은 환자 목록과 Patient 원천 데이터가 필요합니다.")
    for value in resources.values():
        batch = SourceBatch.from_dict(value)
        if batch.dataset != cohort["dataset"]:
            raise ValueError("SourceBatch dataset이 다릅니다.")
        field = batch.metadata["patient_field"]
        if any(str(row.get(field)) not in allowed for row in batch.records):
            raise ValueError("SourceBatch에 다른 분할의 환자가 있습니다.")
        if batch.extraction.get("truncated"):
            raise ValueError("SourceBatch가 원천 전체를 포함하지 않습니다.")
    patient_data = resources["Patient"]
    identities = [str(row.get(patient_data["metadata"]["patient_field"])) for row in patient_data["records"]]
    if len(identities) != len(set(identities)) or set(identities) != allowed:
        raise ValueError("Patient 원천 행과 분할 환자 목록이 일치하지 않습니다.")


def make_validator(policy: dict[str, Any], *, external: bool = True):
    options = policy["config"]["validation"]
    schema = get_fhir_schema(ROOT / options["schema_path"])

    def validate(resources: list[dict[str, Any]]) -> dict[str, Any]:
        from .validation.fidelity import evaluate_references
        errors = [message for resource in resources for message in [*schema.errors(resource), *resource_semantic_errors(resource)]]
        warnings: list[str] = []
        try:
            bundle = build_bundle(resources, namespace="research-fhir")
            errors.extend(schema.errors(bundle))
            reference_result = evaluate_references([entry["resource"] for entry in bundle["entry"]], bundle["entry"])
            reference_rate = reference_result["accuracy"]
            errors.extend(issue["message"] for issue in reference_result["issues"])
        except (ValueError, TypeError, KeyError) as exc:
            return {"valid": False, "errors": [*errors, str(exc)], "warnings": [],
                    "external_validator_used": False, "external_validation_status": "blocked"}
        jar = options.get("validator_jar")
        used = False
        counts: dict[str, int] = {}
        external_issues = []
        external_errors = []
        external_status = "disabled"
        if jar and external:
            try:
                result = _run_external_validator(bundle, ROOT / jar, "4.0.1", int(options.get("timeout_seconds", 120)), 20000,
                                                 terminology_server=options.get("terminology_server"))
                used = result.used
                errors.extend(result.errors)
                external_errors = result.errors
                external_status = result.status
                warnings.extend(result.warnings)
                counts = result.issue_counts
                external_issues = result.issues
            except OSError as exc:
                errors.append(f"HL7 Validator 실행 불가: {type(exc).__name__}: {exc}")
                external_errors = [errors[-1]]
                external_status = "execution_failed"
        elif options.get("require_hl7", False) and external:
            external_errors = ["이 실험은 HL7 Validator가 필수이지만 validator_jar가 없습니다."]
            errors.extend(external_errors)
            external_status = "unavailable"
        return {"valid": not errors, "errors": errors, "warnings": warnings,
                "external_validator_used": used, "validator_issue_counts": counts,
                "reference_resolution_rate": reference_rate,
                "issues": external_issues,
                "local_errors": errors[:-len(external_errors)] if external_errors else list(errors),
                "external_errors": external_errors,
                "external_validation_status": external_status,
                "validation_level": "hl7" if external_status == "completed" else "json_schema_only"}

    return validate


def verify_runtime(policy: dict[str, Any], client: ResearchOllamaClient, retriever: FhirRetriever | None) -> None:
    verify_policy(policy)
    if client.identity() != policy["model_identity"]:
        raise ValueError("실제 모델이 잠금 모델과 다릅니다.")
    if policy["config"]["rag"].get("enabled", True):
        if retriever is None or retriever.identity != policy["rag_identity"]:
            raise ValueError("RAG index가 잠금 설정과 다릅니다.")
        if client.identity(client.embedding_model) != retriever.identity["embedding_identity"]:
            raise ValueError("임베딩 모델이 잠금 모델과 다릅니다.")
        if retriever.identity["schema_sha256"] != policy["fhir_schema_sha256"]:
            raise ValueError("Vector index와 잠금 FHIR Schema가 다릅니다.")
    elif retriever is not None:
        raise ValueError("RAG 비활성 실험에 검색기를 추가할 수 없습니다.")


def _verify_resource_scope(policy, cohort):
    expected = set(policy["config"].get("resources", cohort["resources"]))
    if set(cohort["resources"]) != expected:
        raise ValueError("cohort의 리소스 범위가 잠금 실험 설정과 다릅니다.")


def _verify_source_inputs(policy, cohort, source_spec, source_retriever):
    rag = policy["config"]["rag"]
    if rag.get("source_contract"):
        if not isinstance(source_spec, dict) or fingerprint(source_spec) != cohort["extract_spec_sha256"]:
            raise ValueError("스키마 전용 실험에는 코호트와 동일한 --source-spec이 필요합니다.")
        from .source_rag import source_metadata
        if source_spec.get("dataset") != cohort["dataset"] or set(source_spec.get("resources", {})) != set(cohort["resources"]):
            raise ValueError("원천 스키마 계약의 데이터셋·리소스 범위가 코호트와 다릅니다.")
        for kind, batch in cohort["resources"].items():
            meta = source_metadata(source_spec, kind)
            if (meta["source_resource"] != batch["source_resource"]
                    or set(meta["columns"]) - set(batch["metadata"]["columns"])):
                raise ValueError("원천 스키마 계약의 테이블·컬럼이 코호트와 다릅니다.")
    elif source_spec is not None:
        raise ValueError("원천 스키마 계약이 비활성화된 정책에는 --source-spec을 연결할 수 없습니다.")
    if not rag.get("source_enabled"):
        if source_retriever is not None:
            raise ValueError("원천 RAG가 비활성화된 정책에는 원천 인덱스를 연결할 수 없습니다.")
        return None
    if source_retriever is None:
        raise ValueError("원천 RAG 정책에는 --source-index가 필요합니다.")
    source_retriever.verify_cohort(cohort)
    if any(source_retriever.metadata(kind) != source_metadata(source_spec, kind) for kind in cohort["resources"]):
        raise ValueError("원천 인덱스의 스키마 내용이 검토된 추출 명세와 다릅니다.")
    identity = source_retriever.identity
    if identity["recipe"] != policy.get("source_rag_recipe"):
        raise ValueError("원천 검색 방법이 잠금 정책과 다릅니다.")
    if identity["embedding_identity"] != policy["rag_identity"]["embedding_identity"]:
        raise ValueError("원천 및 FHIR 인덱스의 임베딩 모델이 다릅니다.")
    return identity


def _source_context(resource_type, source_spec, source_retriever):
    if source_retriever is not None:
        return source_retriever.context(resource_type)
    if source_spec is not None:
        from .source_rag import exact_source_context
        return exact_source_context(source_spec, resource_type)
    return None


def managed_identity_enabled(config: dict[str, Any]) -> bool:
    return config["generation"].get("managed_identity", config["rag"].get("source_contract", False))


def _identity_metadata(metadata: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    # CSV's declared primary_keys can be a column group for discovery rather than
    # an actual primary key. Prefer its extractor-provided snapshot row locator.
    natural = list(metadata.get("identity_fields", []))
    fallback = list(metadata.get("record_identity_fields", []))
    if not fallback and extraction.get("mode") == "predefined-query":
        fallback = list(metadata.get("primary_keys", []))
    if set(natural or fallback) - set(metadata.get("columns", [])):
        raise ValueError("원천 식별자 계약에 추출 명세에 없는 컬럼이 있습니다.")
    return {"identity_contract": True,
            "identity_fields": natural, "record_identity_fields": fallback}


def verify_source_identities(cohort: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    """Preflight canonical encoded IDs without using instances to choose the key."""
    if not managed_identity_enabled(config):
        return None
    from .renderer.operations import validate_record_identity
    result = {}
    for kind, payload in cohort["resources"].items():
        batch = SourceBatch.from_dict(payload)
        try:
            metadata = _identity_metadata(batch.metadata, batch.extraction)
            validate_record_identity(batch.records, metadata)
            result[kind] = canonical_identity(metadata)
        except ValueError as exc:
            raise ValueError(f"{kind} 원천 식별자 계약: {exc}") from exc
    return result


def _source_metadata(batch: SourceBatch, cohort: dict[str, Any], config: dict[str, Any],
                     resource_type: str, source_spec=None) -> dict[str, Any]:
    if source_spec is None:
        metadata = batch.metadata
    else:
        from .source_rag import source_metadata
        metadata = source_metadata(source_spec, resource_type)
    managed = managed_identity_enabled(config)
    related = {}
    for kind, payload in cohort["resources"].items():
        entry = {"table": payload["source_resource"],
                 "primary_keys": payload["metadata"].get("primary_keys", []),
                 "identity_fields": payload["metadata"].get("identity_fields", [])}
        if managed:
            entry["identity"] = canonical_identity(_identity_metadata(payload["metadata"], payload["extraction"]))
        related[kind] = entry
    return {**metadata, **(_identity_metadata(batch.metadata, batch.extraction) if managed else {}),
            "dataset": batch.dataset, "dataset_version": batch.dataset_version,
            "table": batch.source_resource, "datetime_timezone": config.get("generation", {}).get("timezone"),
            "related_sources": related}


def _target_context(resource_type: str, policy: dict[str, Any], retriever: FhirRetriever | None,
                    constraints=None) -> str:
    # 두 군에 같은 기본 구조·enum을 제공하고 RAG 검색 결과를 추가합니다.
    schema = get_fhir_schema(ROOT / policy["config"]["validation"]["schema_path"])
    definition = schema.definition(resource_type)
    properties = definition.get("properties", {})
    references = {value["$ref"].rsplit("/", 1)[-1] for value in properties.values()
                  if "$ref" in value}
    types = {name: {key: value for key, value in schema.definition(name).get("properties", {}).items()
                    if not key.startswith("_")} for name in sorted(references & {"Coding", "CodeableConcept", "Quantity", "Reference"})}
    required = set(definition.get("required", [])) | set(adapter_for(resource_type).required_fields)
    basic = json.dumps({"resourceType": resource_type, "required": sorted(required), "datatype_properties": types,
                        "properties": {name: value for name, value in definition.get("properties", {}).items()
                                       if not name.startswith("_")}}, ensure_ascii=False)
    if constraints is None:
        return basic + ("\n\n" + retriever.context(resource_type) if retriever else "")
    sections = [json.dumps({"resourceType": resource_type, "required": sorted(required)}),
                "Authoritative FHIR R4 snapshot constraints:", constraints.context(resource_type)]
    if retriever:
        sections.extend(("Retrieved FHIR schema excerpts:", retriever.context(resource_type)))
    return "\n\n".join(sections)


def _reference_resource_types(value: Any) -> set[str]:
    if isinstance(value, dict):
        types = set()
        reference = value.get("reference")
        if isinstance(reference, str) and reference.count("/") == 1:
            types.add(reference.split("/", 1)[0])
        for item in value.values():
            types.update(_reference_resource_types(item))
        return types
    if isinstance(value, (list, tuple)):
        return {kind for item in value for kind in _reference_resource_types(item)}
    return set()


def generate_profiles(policy: dict[str, Any], cohort: dict[str, Any], client: Any,
                      retriever: Any = None, *, verify: bool = True, on_checkpoint=None,
                      source_spec=None, source_retriever=None) -> dict[str, Any]:
    """준비 환자에서만 생성/교정합니다. 독립 정답은 이 함수의 입력에 없습니다."""
    verify_cohort(cohort)
    _verify_resource_scope(policy, cohort)
    allowed_roles = {"mimic_iv": {"development", "validation"}, "synthea": {"calibration"}}
    if cohort["role"] not in allowed_roles.get(cohort["dataset"], set()):
        raise ValueError("평가 환자로 Profile을 생성·교정할 수 없습니다.")
    source_identity = _verify_source_inputs(policy, cohort, source_spec, source_retriever)
    record_identities = verify_source_identities(cohort, policy["config"])
    if verify:
        verify_runtime(policy, client, retriever)
    config = policy["config"]
    constraints = exact_fhir_constraints(config)
    agent = ProfileMappingAgent(client, config.get("prompt_version", "v1"), config["prompt_texts"]["mapping"],
                                schema_path=ROOT / config["validation"]["schema_path"])
    generated: dict[str, Any] = {}
    contexts: dict[str, str] = {}
    source_contexts: dict[str, dict[str, Any]] = {}
    started = perf_counter()
    before = len(client.calls)
    sample_size = int(config["generation"].get("sample_rows", 3))
    def checkpoint(stage, kind):
        if on_checkpoint is not None:
            entry = {name: deepcopy(value) for name, value in generated[kind].items() if name != "metadata"}
            on_checkpoint({"stage": stage, "resource_type": kind, "complete": False,
                           "policy_sha256": policy["policy_sha256"], "cohort_sha256": cohort["cohort_sha256"],
                           "profile": entry, "calls": deepcopy(client.calls[before:])})
    for resource_type in sorted(cohort["resources"], key=lambda kind: adapter_for(kind).order):
        payload = cohort["resources"][resource_type]
        batch = SourceBatch.from_dict(payload)
        if not batch.records and source_spec is None:
            generated[resource_type] = {"initial": None, "corrected": None, "status": "empty_calibration_source"}
            continue
        metadata = _source_metadata(batch, cohort, config, resource_type, source_spec)
        if sample_size:
            metadata["sample_records"] = select_profile_examples(
                batch.records, sample_size, batch.metadata.get("sample_group_fields", []))
        if record_identities is None:
            for kind, entry in generated.items():
                if entry.get("initial") is not None:
                    metadata["related_sources"][kind]["identity"] = deepcopy(entry["initial"]["identity"])
        context = _target_context(resource_type, policy, retriever, constraints)
        contexts[resource_type] = context
        source_contexts[resource_type] = _source_context(resource_type, source_spec, source_retriever)
        initial_calls_start = len(client.calls)
        initial_started = perf_counter()
        previous_error = None
        initial = None
        structural_failures = 0
        for _attempt in range(int(config["generation"].get("max_attempts", 2))):
            try:
                initial = agent.generate(metadata, context, resource_type, previous_error=previous_error,
                                         source_context=source_contexts.get(resource_type))
                if initial["source"]["dataset"] != batch.dataset or initial["source"]["table"] != batch.source_resource or initial["target_resource"] != resource_type:
                    raise ValueError("Mapping Agent가 요청한 데이터셋·테이블·리소스를 변경했습니다.")
                break
            except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                initial = None
                structural_failures += 1
                previous_error = {"message": f"{type(exc).__name__}: {exc}",
                                  "draft": deepcopy(getattr(exc, "draft", None)),
                                  "instruction": "Repair this draft into the execution contract. Preserve valid rules, verify all required target fields, and use at most one FHIR choice variant per row. Omit unsupported optional defaults."}
        if initial is None:
            generated[resource_type] = {"initial": None, "corrected": None, "status": "profile_generation_failed", "error": previous_error,
                                        "structural_generation_failures": structural_failures}
            checkpoint("initial", resource_type)
            continue
        generated[resource_type] = {"initial": initial, "corrected": initial, "metadata": metadata, "history": [],
                                    "structural_generation_failures": structural_failures,
                                    "initial_generation_calls": deepcopy(client.calls[initial_calls_start:]),
                                    "initial_generation_seconds": perf_counter() - initial_started}
        checkpoint("initial", resource_type)

    initial_preparation_calls = deepcopy(client.calls[before:])
    initial_preparation_seconds = perf_counter() - started
    # 교정 검증 시 다른 리소스의 참조 대상도 함께 제공합니다.
    from .renderer.operations import render_records
    def all_resources(variant: str, replace_type: str, replacement):
        combined = list(replacement)
        selected = {replace_type}
        pending = _reference_resource_types(replacement)
        while pending:
            kind = min(pending)
            pending.remove(kind)
            if kind in selected or kind not in generated or generated[kind][variant] is None:
                continue
            selected.add(kind)
            other = SourceBatch.from_dict(cohort["resources"][kind])
            metadata = _source_metadata(other, cohort, config, kind, source_spec) if record_identities is not None else None
            resources, _ = render_records(generated[kind][variant], list(other.records),
                                          config["generation"].get("timezone"), metadata=metadata,
                                          schema_path=ROOT / config["validation"]["schema_path"])
            combined.extend(resources)
            pending.update(_reference_resource_types(resources) - selected)
        return combined

    validator = make_validator(policy)
    for resource_type, entry in generated.items():
        if entry["initial"] is None:
            continue
        batch = SourceBatch.from_dict(cohort["resources"][resource_type])
        def revise(current, issues, _records, entry=entry, resource_type=resource_type):
            return agent.revise(current, issues, entry["metadata"], contexts[resource_type],
                                source_context=source_contexts.get(resource_type))
        correction_calls_start = len(client.calls)
        correction_started = perf_counter()
        try:
            result = HybridFhirPipeline(
                correct=True, max_iterations=int(config["correction"]["max_iterations"]),
                profile_reviser=revise,
                validator=lambda resources, kind=resource_type: validator(all_resources("corrected", kind, resources)),
                source_checks=config.get("source_checks", {}).get(batch.dataset, {}).get(resource_type, []),
                schema_path=ROOT / config["validation"]["schema_path"],
                timezone=config.get("generation", {}).get("timezone"),
            ).run(replace(batch, metadata=entry["metadata"]) if record_identities is not None else batch,
                  entry["initial"])
            entry["corrected"] = result["profile"]
            entry["history"] = result["history"]
            entry["calibration_metrics"] = result["metrics"]
            entry["calibration_issues"] = result["issues"]
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            entry["status"] = "profile_correction_failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["correction_calls"] = deepcopy(client.calls[correction_calls_start:])
        entry["correction_seconds"] = perf_counter() - correction_started
        entry.pop("metadata", None)
        checkpoint("correction", resource_type)
    payload = {
        "format_version": 1, "dataset": cohort["dataset"], "policy_sha256": policy["policy_sha256"],
        "split_sha256": cohort["split"]["manifest_sha256"], "prepared_role": cohort["role"],
        "preparation_cohort_sha256": cohort["cohort_sha256"], "preparation_patient_keys": cohort["patient_keys"],
        "extract_spec_sha256": cohort["extract_spec_sha256"],
        "source_rag_identity": source_identity,
        "record_identity_contract": record_identities,
        "profiles": generated, "elapsed_seconds": perf_counter() - started,
        "calls": deepcopy(client.calls[before:]),
        "initial_preparation_calls": initial_preparation_calls,
        "initial_preparation_seconds": initial_preparation_seconds,
    }
    return {**payload, "profiles_sha256": fingerprint(payload)}


def _direct_generate(batch: SourceBatch, target: str, context: str, policy: dict[str, Any], client: Any, *, errors=None):
    size = int(policy["config"]["generation"]["batch_size"])
    resources = []
    for start in range(0, len(batch.records), size):
        prompt = json.dumps({"target_resource": target, "source_metadata": {**batch.metadata, "datetime_timezone": policy["config"]["generation"].get("timezone")},
                             "source_records": list(batch.records[start:start + size]), "fhir_context": context,
                             "output_contract": {"resources": ["one FHIR object per source record, in source order"]}}, ensure_ascii=False, default=str)
        try:
            payload = load_llm_json(client.chat(policy["config"]["prompt_texts"]["direct"], prompt))
            if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
                raise ValueError("Direct 응답에 resources 배열이 없습니다.")
            if any(not isinstance(item, dict) or item.get("resourceType") != target for item in payload["resources"]):
                raise ValueError("Direct 응답의 resourceType이 올바르지 않습니다.")
            if len(payload["resources"]) != len(batch.records[start:start + size]):
                raise ValueError("Direct 배치의 원천 행 수와 생성 수가 다릅니다.")
            resources.extend(payload["resources"])
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            if errors is None:
                raise
            errors.append(f"{target} rows[{start}:{start + size}]: {type(exc).__name__}: {exc}")
    return resources


def evaluate_experiment(policy: dict[str, Any], cohort: dict[str, Any], profiles: dict[str, Any], client: Any,
                        retriever: Any = None, *, reference_resources=None, reference_profiles=None,
                        groups=("B1", "P1", "P2"), verify=True, on_checkpoint=None, cohort_lock=None,
                        source_spec=None, source_retriever=None) -> dict[str, Any]:
    """평가 환자의 결과로 Profile이나 프롬프트를 갱신하지 않습니다."""
    from .renderer.operations import render_records_detailed
    from .validation.fidelity import evaluate_reference_resources, project_comparison_resources
    if cohort_lock is None:
        verify_cohort(cohort)
    else:
        from .cohorts import verify_locked_cohort
        verify_locked_cohort(cohort_lock, cohort, "evaluation", reference_resources=reference_resources,
                             reference_profiles=reference_profiles, profiles=profiles)
    _verify_resource_scope(policy, cohort)
    if cohort["role"] != "test" and not (cohort["dataset"] == "mimic_iv" and cohort["role"] == "validation"):
        raise ValueError("평가에는 test 또는 MIMIC validation cohort가 필요합니다.")
    unsigned = {key: value for key, value in profiles.items() if key != "profiles_sha256"}
    if fingerprint(unsigned) != profiles.get("profiles_sha256"):
        raise ValueError("Profile artifact의 hash가 다릅니다.")
    if profiles["dataset"] != cohort["dataset"] or profiles["policy_sha256"] != policy["policy_sha256"]:
        raise ValueError("Profile의 dataset/잠금 정책이 다릅니다.")
    if profiles["split_sha256"] != cohort["split"]["manifest_sha256"]:
        raise ValueError("Profile 준비 단계와 평가 단계의 환자 분할이 다릅니다.")
    if profiles.get("extract_spec_sha256") != cohort["extract_spec_sha256"]:
        raise ValueError("준비와 평가 단계의 추출 명세가 다릅니다.")
    source_identity = _verify_source_inputs(policy, cohort, source_spec, source_retriever)
    if profiles.get("source_rag_identity") != source_identity:
        raise ValueError("Profile 준비와 평가에 사용한 원천 인덱스가 다릅니다.")
    record_identities = verify_source_identities(cohort, policy["config"])
    if profiles.get("record_identity_contract") != record_identities:
        raise ValueError("Profile 준비와 평가의 원천 식별자 계약이 다릅니다.")
    if cohort["role"] == "validation" and profiles["prepared_role"] != "development":
        raise ValueError("MIMIC validation은 development에서 준비한 Profile만 사용합니다.")
    if {str(x) for x in profiles["preparation_patient_keys"]} & {str(x) for x in cohort["patient_keys"]}:
        raise ValueError("Profile 준비 환자와 평가 환자가 중복됩니다.")
    if not groups or set(groups) - {"B1", "P1", "P2"}:
        raise ValueError("비교군은 B1/P1/P2만 허용합니다.")
    if verify:
        verify_runtime(policy, client, retriever)
    if reference_resources is not None:
        expected_patients = {str(item.get("id")) for item in reference_resources if item.get("resourceType") == "Patient"}
        if expected_patients != {str(x) for x in cohort["patient_keys"]}:
            raise ValueError("독립 참조 Patient와 평가 환자 목록이 일치하지 않습니다.")
    validator = make_validator(policy)
    structural_validator = make_validator(policy, external=False)
    constraints = exact_fhir_constraints(policy["config"]) if "B1" in groups else None
    results = {}
    for group in groups:
        started = perf_counter()
        before = len(client.calls)
        resources = []
        resources_by_patient: dict[str, list[dict[str, Any]]] = {}
        errors = []
        source_count = 0
        expected_counts = {}
        for kind, payload in cohort["resources"].items():
            batch = SourceBatch.from_dict(payload)
            source_count += len(batch.records)
            expected_counts[kind] = len(batch.records)
            try:
                if group == "B1":
                    metadata = _source_metadata(batch, cohort, policy["config"], kind, source_spec)
                    if source_spec is not None:
                        metadata["source_context"] = _source_context(kind, source_spec, source_retriever)
                    direct_batch = replace(batch, metadata=metadata, records=tuple(
                        {key: row[key] for key in metadata["columns"] if key in row} for row in batch.records
                    ) if source_spec is not None else batch.records)
                    resources.extend(_direct_generate(direct_batch, kind, _target_context(kind, policy, retriever, constraints), policy, client, errors=errors))
                else:
                    entry = profiles["profiles"].get(kind, {})
                    profile = entry.get("initial" if group == "P1" else "corrected")
                    if profile is None and batch.records:
                        raise ValueError(f"{kind}의 준비된 Profile이 없습니다.")
                    if profile is not None:
                        if profile["target_resource"] != kind or profile["source"]["dataset"] != batch.dataset or profile["source"]["table"] != batch.source_resource:
                            raise ValueError("Profile의 대상 리소스·원천 데이터셋·테이블이 다릅니다.")
                        metadata = (_source_metadata(batch, cohort, policy["config"], kind, source_spec)
                                    if source_spec is not None or record_identities is not None else batch.metadata)
                        validated = validate_profile(profile, metadata, schema_path=ROOT / policy["config"]["validation"]["schema_path"])
                        rendered = render_records_detailed(validated, list(batch.records),
                            policy["config"]["generation"].get("timezone"),
                            metadata=metadata if record_identities is not None else None,
                            schema_path=ROOT / policy["config"]["validation"]["schema_path"])
                        resources.extend(rendered.resources)
                        errors.extend(rendered.errors)
                        if record_identities is not None:
                            for index, resource in zip(rendered.source_indices, rendered.resources, strict=True):
                                source_patient = str(batch.records[index][batch.metadata["patient_field"]])
                                resources_by_patient.setdefault(source_patient, []).append(resource)
            except Exception as exc:
                errors.append(f"{kind}: {type(exc).__name__}: {exc}")
        report = structural_validator(resources)
        actual_counts = {kind: sum(item.get("resourceType") == kind for item in resources) for kind in expected_counts}
        for kind, count in expected_counts.items():
            if count != actual_counts[kind]:
                errors.append(f"{kind} 원천 {count}건과 생성 {actual_counts[kind]}건이 다릅니다.")
        report["errors"] = [*errors, *report["errors"]]
        report["valid"] = not report["errors"]
        reference = None
        options = policy["config"].get("evaluation", {})
        projections = {
            "date_only_paths": options.get("date_only_paths", {}),
            **options.get("projections_by_dataset", {}).get(cohort["dataset"], {}),
        }
        scoring_scope = {
            "match_paths": options.get("match_paths"),
            "field_paths": options.get("field_paths_by_dataset", {}).get(cohort["dataset"], options.get("field_paths")),
        }
        if reference_resources is not None:
            generated_for_comparison = project_comparison_resources(resources, **projections)
            gold_for_comparison = project_comparison_resources(reference_resources, **projections)
            reference = evaluate_reference_resources(generated_for_comparison, gold_for_comparison, **scoring_scope)
            reference["date_only_paths"] = projections["date_only_paths"]
            reference["comparison_projections"] = projections
        patients = {}
        assigned = 0
        for key in cohort["patient_keys"]:
            key = str(key)
            def belongs(item):
                if item.get("resourceType") == "Patient":
                    return str(item.get("id")) == key
                return item.get("subject", {}).get("reference") == f"Patient/{key}"
            source_partitioned = record_identities is not None and group != "B1"
            patient_resources = (resources_by_patient.get(key, []) if source_partitioned
                                 else [item for item in resources if belongs(item)])
            assigned += len(patient_resources)
            patient_report = validator(patient_resources) if patient_resources else {
                "valid": False, "errors": ["환자 Bundle이 생성되지 않았습니다."], "warnings": [],
                "external_validator_used": False, "external_validation_status": "not_generated",
                "validation_level": "not_generated", "validator_issue_counts": {},
            }
            patient_counts = {kind: sum(str(row[payload["metadata"]["patient_field"]]) == key for row in payload["records"])
                              for kind, payload in cohort["resources"].items()}
            patient_actual = {kind: sum(item.get("resourceType") == kind for item in patient_resources) for kind in patient_counts}
            aligned = patient_counts == patient_actual
            patient_reference = None
            if reference is not None:
                patient_reference = evaluate_reference_resources(
                    project_comparison_resources(patient_resources, **projections) if source_partitioned
                    else [item for item in generated_for_comparison if belongs(item)],
                    [item for item in gold_for_comparison if belongs(item)],
                    **scoring_scope)
            patients[key] = {
                "validation": patient_report, "source_record_count": sum(patient_counts.values()),
                "resource_count_alignment": {"aligned": aligned, "expected": patient_counts, "generated": patient_actual},
                "source_field_accuracy": patient_reference["source_field_accuracy"]["accuracy"] if patient_reference else None,
                "event_f1": patient_reference["event_detection"]["f1"] if patient_reference else None,
                "event_match_coverage": patient_reference["event_match_coverage"]["accuracy"] if patient_reference else None,
                "matched_event_field_accuracy": patient_reference["matched_event_field_accuracy"]["accuracy"] if patient_reference else None,
                "expected_reference_coverage": patient_reference["expected_reference_metrics"]["coverage"]["accuracy"] if patient_reference else None,
                "expected_reference_accuracy": patient_reference["expected_reference_metrics"]["target_accuracy"]["accuracy"] if patient_reference else None,
                "overall_pass": None if patient_reference is None else bool(patient_report["valid"] and aligned and patient_reference["pass"]),
            }
            report["errors"].extend(f"Patient/{key}: {error}" for error in patient_report["errors"])
            report["warnings"].extend(patient_report.get("warnings", []))
        if assigned != len(resources):
            report["errors"].append(f"환자에 배정할 수 없는 리소스: {len(resources) - assigned}건")
        report["valid"] = not report["errors"]
        report["external_validator_used"] = all(patient["validation"]["external_validator_used"] for patient in patients.values())
        report["validator_issue_counts"] = {
            severity: sum(patient["validation"].get("validator_issue_counts", {}).get(severity, 0) for patient in patients.values())
            for severity in ("fatal", "error", "warning", "information")}
        statuses = Counter(patient["validation"]["external_validation_status"] for patient in patients.values())
        report["external_validation_status_counts"] = dict(statuses)
        report["external_validation_status"] = next(iter(statuses)) if len(statuses) == 1 else "mixed"
        report["validation_level"] = "hl7" if statuses == {"completed": len(patients)} else "json_schema_only"
        mapping_scores = None
        if reference_profiles is not None and group in {"P1", "P2"}:
            mapping_scores = {}
            for kind in cohort["resources"]:
                predicted = profiles["profiles"].get(kind, {}).get("initial" if group == "P1" else "corrected")
                gold_profile = reference_profiles.get(kind)
                if gold_profile is not None:
                    validate_profile(gold_profile, cohort["resources"][kind]["metadata"])
                    batch_payload = cohort["resources"][kind]
                    if gold_profile["target_resource"] != kind or gold_profile["source"] != {"dataset": cohort["dataset"], "table": batch_payload["source_resource"]}:
                        raise ValueError("독립 참조 매핑의 대상 리소스·원천 범위가 다릅니다.")
                    mapping_scores[kind] = mapping_accuracy(predicted or {}, gold_profile,
                                                            include_identity=record_identities is None)
        count_aligned = actual_counts == expected_counts
        results[group] = {
            "validation": report, "source_record_count": source_count, "resource_count": len(resources),
            "resource_count_alignment": {"expected": source_count, "generated": len(resources), "aligned": count_aligned,
                                         "by_resource": {kind: {"expected": expected_counts[kind], "generated": actual_counts[kind]} for kind in expected_counts}},
            "independent_reference_evaluation": reference or {"status": "not_evaluated"},
            "overall_pass": None if reference is None else bool(report["valid"] and count_aligned and reference.get("pass", reference.get("valid", False))),
            "elapsed_seconds": perf_counter() - started, "calls": deepcopy(client.calls[before:]),
            "resources": resources,
            "patients": patients, "mapping_accuracy": mapping_scores or {"status": "not_evaluated"},
            "fhir_bundle_pass_rate": sum(patient["validation"]["valid"] for patient in patients.values()) / len(patients),
            "cost": call_cost(client.calls[before:]),
        }
        if on_checkpoint is not None:
            on_checkpoint({"stage": "evaluation", "group": group, "complete": False,
                           "policy_sha256": policy["policy_sha256"], "profiles_sha256": profiles["profiles_sha256"],
                           "cohort_sha256": cohort["cohort_sha256"], "result": deepcopy(results[group])})
    calibration = [entry["calibration_metrics"] for entry in profiles["profiles"].values() if "calibration_metrics" in entry]
    initial_failures = sum(not entry["first_pass_valid"] for entry in calibration)
    repaired = sum(entry["standard_repair_success"] for entry in calibration)
    correction_summary = {
        "unit": "resource_profile", "evaluated_profiles": len(calibration),
        "initial_failures": initial_failures, "repaired_profiles": repaired,
        "repair_success_rate": repaired / initial_failures if initial_failures else None,
        "profile_generation_failures": sum(entry.get("initial") is None for entry in profiles["profiles"].values()),
        "initial_attempt_failures": sum(entry.get("structural_generation_failures", 0) for entry in profiles["profiles"].values()),
        "profiles_recovered_during_initial_generation": sum(bool(entry.get("initial") and entry.get("structural_generation_failures"))
                                                             for entry in profiles["profiles"].values()),
        "resolved_error_count": sum(entry.get("resolved_error_count", 0) for entry in calibration),
        "introduced_error_count": sum(entry.get("introduced_error_count", 0) for entry in calibration),
        "proposed_introduced_error_count": sum(entry.get("proposed_introduced_error_count", 0) for entry in calibration),
    }
    preparation_costs = {}
    for group in groups:
        calls = []
        seconds = 0
        if group in {"P1", "P2"}:
            calls.extend(profiles.get("initial_preparation_calls", []))
            seconds += profiles.get("initial_preparation_seconds", 0)
            for entry in profiles["profiles"].values():
                if "initial_preparation_calls" not in profiles:
                    calls.extend(entry.get("initial_generation_calls", []))
                    seconds += entry.get("initial_generation_seconds", 0)
                if group == "P2":
                    calls.extend(entry.get("correction_calls", []))
                    seconds += entry.get("correction_seconds", 0)
        preparation_costs[group] = {"preparation": call_cost(calls), "evaluation": results[group]["cost"],
                                    "total": call_cost([*calls, *results[group]["calls"]]),
                                    "preparation_seconds": seconds,
                                    "total_seconds": seconds + results[group]["elapsed_seconds"]}
    analysis_config = policy["config"].get("analysis", {})
    return {
        "format_version": 1, "dataset": cohort["dataset"], "evaluation_kind": ("development_validation" if cohort["role"] == "validation" else "internal") if cohort["dataset"] == "mimic_iv" else "synthetic_external",
        "policy_sha256": policy["policy_sha256"], "profiles_sha256": profiles["profiles_sha256"],
        "cohort_sha256": cohort["cohort_sha256"], "split_sha256": cohort["split"]["manifest_sha256"],
        "cohort_lock_sha256": cohort_lock["cohort_lock_sha256"] if cohort_lock is not None else None,
        "patient_count": len(cohort["patient_keys"]), "profile_preparation_calls": profiles["calls"],
        "profile_preparation_seconds": profiles["elapsed_seconds"],
        "independent_reference_sha256": fingerprint(reference_resources) if reference_resources is not None else None,
        "reference_profiles_sha256": fingerprint(reference_profiles) if reference_profiles is not None else None,
        "correction_summary": correction_summary, "cost_summary": preparation_costs,
        "paired_comparisons": within_run_comparisons(results, seed=analysis_config.get("seed", 42), resamples=analysis_config.get("resamples", 2000)),
        "pilot_summary": pilot_summary(results, target_half_width=analysis_config.get("target_half_width")),
        "policy_config": deepcopy(policy["config"]),
        "model_identity": policy["model_identity"], "code_sha256": policy["code_sha256"],
        "rag_identity": policy["rag_identity"], "fhir_constraints_identity": policy.get("fhir_constraints_identity"),
        "source_rag_identity": source_identity,
        "record_identity_contract": record_identities,
        "groups": results,
    }


def save_evaluation(result: dict[str, Any], output_directory: str | Path) -> None:
    directory = Path(output_directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("결과 디렉터리는 비어 있어야 합니다. 기존 실험과 다른 경로를 지정하세요.")
    manifest = deepcopy(result)
    for group, data in manifest["groups"].items():
        resources = data.pop("resources")
        write_json(directory / f"{group}.resources.json", resources)
        try:
            write_json(directory / f"{group}.bundle.json", build_bundle(resources, namespace="research-fhir"))
        except ValueError:
            # 중복 ID 등도 실패 산출물로 보존합니다.
            pass
    write_json(directory / "manifest.json", manifest)
