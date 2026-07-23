from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agents import FhirResourceGenerationAgent
from .config import Settings
from .fhir_schema import FhirSchemaRepository
from .json_utils import dump_json
from .mapping_config import MimicMappingConfig
from .resource_registry import resource_semantic_errors
from .vector_store import PgVectorKnowledgeStore


def empty_array_errors(value: Any, path: str = "resource") -> list[str]:
    """JSON Schema가 놓치는 FHIR 기본 규칙인 빈 반복 요소를 찾습니다."""

    errors: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            errors.extend(empty_array_errors(nested, f"{path}.{key}"))
    elif isinstance(value, list):
        if not value:
            errors.append(f"{path}는 빈 배열일 수 없습니다. 값이 없으면 속성을 제거하세요.")
        else:
            for index, nested in enumerate(value):
                errors.extend(empty_array_errors(nested, f"{path}[{index}]"))
    return errors


@dataclass(frozen=True)
class FhirGenerationResult:
    resources: list[dict[str, Any]]
    llm_calls_used: int
    batches: int
    first_pass_valid_batches: int
    repaired_batches: int
    repair_attempts: int
    initial_error_count: int

    def metrics(self) -> dict[str, int | float]:
        return {
            "batches": self.batches,
            "llm_calls_used": self.llm_calls_used,
            "first_pass_valid_batches": self.first_pass_valid_batches,
            "first_pass_valid_rate": (
                1.0
                if not self.batches
                else self.first_pass_valid_batches / self.batches
            ),
            "repaired_batches": self.repaired_batches,
            "repair_success_rate": (
                1.0
                if not self.batches - self.first_pass_valid_batches
                else self.repaired_batches
                / (self.batches - self.first_pass_valid_batches)
            ),
            "repair_attempts": self.repair_attempts,
            "initial_error_count": self.initial_error_count,
            "final_error_count": 0,
            "error_reduction_rate": 1.0,
        }


class FhirLlmResourceService:
    """Vector 문맥 조회, LLM FHIR 생성, 소스 충실도와 Schema 검사를 담당합니다."""

    def __init__(
        self,
        settings: Settings,
        mapping: MimicMappingConfig,
        schema: FhirSchemaRepository,
        agent: FhirResourceGenerationAgent,
        vector_store: PgVectorKnowledgeStore | None,
    ) -> None:
        self.settings = settings
        self.mapping = mapping
        self.schema = schema
        self.agent = agent
        self.vector_store = vector_store

    def generate(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        subject_id: int,
    ) -> FhirGenerationResult:
        context = self._fhir_context(resource_type)
        resources: list[dict[str, Any]] = []
        calls = 0
        batches = 0
        first_pass_valid_batches = 0
        repaired_batches = 0
        initial_error_count = 0
        batch_size = self.settings.llm_fhir_batch_size
        for start in range(0, len(rows), batch_size):
            batches += 1
            batch = rows[start : start + batch_size]
            previous: Any = None
            validation_errors: list[str] = []
            for attempt in range(1, self.settings.llm_fhir_max_attempts + 1):
                calls += 1
                try:
                    previous = self.agent.run(
                        resource_type,
                        batch,
                        self._identity_contract(resource_type, batch, subject_id),
                        context,
                        previous,
                        validation_errors,
                    )
                    generated, validation_errors = self._candidate_errors(
                        resource_type, batch, previous, subject_id
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    generated = []
                    validation_errors = [f"{type(exc).__name__}: {exc}"]
                if not validation_errors:
                    resources.extend(generated)
                    if attempt == 1:
                        first_pass_valid_batches += 1
                    else:
                        repaired_batches += 1
                    break
                if attempt == 1:
                    initial_error_count += len(validation_errors)
                if attempt == self.settings.llm_fhir_max_attempts:
                    raise RuntimeError(
                        f"{resource_type} LLM 생성이 {attempt}회 후에도 실패했습니다: "
                        + "; ".join(validation_errors[:10])
                    )
        return FhirGenerationResult(
            resources=resources,
            llm_calls_used=calls,
            batches=batches,
            first_pass_valid_batches=first_pass_valid_batches,
            repaired_batches=repaired_batches,
            repair_attempts=calls - batches,
            initial_error_count=initial_error_count,
        )

    def _fhir_context(self, resource_type: str) -> str:
        query = (
            f"FHIR R4 {resource_type} required fields identifiers references MIMIC "
            "source columns timezone ICD terminology UCUM mapping"
        )
        matches = (
            self.vector_store.search(
                self.settings.fhir_vector_table,
                query,
                k=self.settings.llm_fhir_context_search_k,
            )
            if self.settings.vectors_enabled and self.vector_store
            else []
        )
        if self.settings.vectors_enabled and not matches:
            raise RuntimeError(
                f"{self.settings.fhir_vector_table}에서 {resource_type} 문맥을 찾지 못했습니다. "
                "먼저 build-vectors를 실행하세요."
            )
        definition = self.schema.definition(resource_type)
        schema_contract = {
            "resourceType": resource_type,
            "required_top_level_properties": definition.get("required", []),
            "allowed_top_level_properties": list(
                definition.get("properties", {}).keys()
            ),
            "additional_top_level_properties_allowed": False,
        }
        return (
            "Official local FHIR JSON Schema contract (authoritative):\n"
            + dump_json(schema_contract)
            + (
                "\n\nVectorDB retrieval:\n"
                + "\n\n".join(item["content"] for item in matches)
                if matches
                else "\n\nVectorDB retrieval disabled for RAG ablation baseline."
            )
        )

    def _identity_contract(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        subject_id: int,
    ) -> str:
        patient_id = self.mapping.resource_id(
            "Patient", {"subject_id": subject_id}
        )
        common: dict[str, Any] = {
            "patient_id": patient_id,
            "patient_reference": f"Patient/{patient_id}",
            "default_timezone_for_naive_datetime": self.settings.fhir_default_timezone,
            "source_row_count": len(rows),
            "expected_identity_by_source_row": [
                {
                    "source_row": index,
                    "resource_id": self._expected_id(resource_type, row),
                    "identifier_value": self._expected_id(resource_type, row),
                }
                for index, row in enumerate(rows)
            ],
        }
        contract_builders = {
            "Patient": self._patient_contract,
            "Encounter": self._encounter_contract,
            "Condition": self._condition_contract,
            "Procedure": self._procedure_contract,
            "Observation": self._observation_contract,
        }
        try:
            builder = contract_builders[resource_type]
        except KeyError as exc:
            raise ValueError(f"지원하지 않는 FHIR 리소스입니다: {resource_type}") from exc
        common.update(
            builder() if resource_type == "Patient" else builder(subject_id)
        )
        return dump_json(common)

    def _patient_contract(self) -> dict[str, Any]:
        mapping = self.mapping
        return {
            "id_rule": mapping.resource_id_rule("Patient"),
            "identifier_system": mapping.patient_identifier_system,
            "gender_map": mapping.patient_gender_map,
            "gender_rule": "Use gender_map exactly; do not invent birthDate",
            "death_date_rule": (
                "When source death_date is present, use it as deceasedDateTime; "
                "otherwise omit deceasedDateTime"
            ),
            "required_json_shape": {
                "resourceType": "Patient",
                "id": "<configured Patient ID>",
                "identifier": [
                    {"system": "<configured system>", "value": "<same id>"}
                ],
                "gender": "<male|female|other|unknown when supported>",
            },
        }

    def _encounter_contract(self, subject_id: int) -> dict[str, Any]:
        mapping = self.mapping
        patient_id = mapping.resource_id("Patient", {"subject_id": subject_id})
        return {
            "id_rule": mapping.resource_id_rule("Encounter"),
            "identifier_system": mapping.encounter_identifier_system,
            "class_system": mapping.encounter_class_system,
            "class_by_admission_type": mapping.encounter_class_by_type,
            "default_class": mapping.encounter_default_class,
            "status_when_end_exists": mapping.encounter_status_when_ended,
            "status_when_end_missing": mapping.encounter_status_when_open,
            "required_json_shape": {
                "resourceType": "Encounter",
                "id": "<configured Encounter ID>",
                "identifier": [
                    {"system": "<configured system>", "value": "<same id>"}
                ],
                "status": "<configured status>",
                "class": {"system": "<class system>", "code": "<mapped class>"},
                "subject": {"reference": f"Patient/{patient_id}"},
                "period": {
                    "start": "<admittime with timezone>",
                    "end": "<dischtime with timezone>",
                },
            },
        }

    def _condition_contract(self, subject_id: int) -> dict[str, Any]:
        mapping = self.mapping
        patient_id = mapping.resource_id("Patient", {"subject_id": subject_id})
        return {
            "id_rule": mapping.resource_id_rule("Condition"),
            "identifier_system": mapping.condition_identifier_system,
            "icd_systems": mapping.diagnosis_icd_systems,
            "icd_system_rule": "Use each source row's fhir_icd_system exactly",
            "icd_decimal_positions": mapping.diagnosis_decimal_positions,
            "icd_prefix_decimal_positions": mapping.diagnosis_prefix_decimal_positions,
            "category_system": mapping.fhir_default("condition_category_system"),
            "category_code": mapping.fhir_default("condition_category_code"),
            "required_json_shape": {
                "resourceType": "Condition",
                "id": "<configured Condition ID>",
                "identifier": [
                    {"system": "<configured system>", "value": "<same id>"}
                ],
                "category": [
                    {"coding": [{"system": "<category system>", "code": "<category code>"}]}
                ],
                "code": {
                    "coding": [
                        {"system": "<ICD system>", "code": "<ICD code>", "display": "<long_title>"}
                    ]
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "encounter": {"reference": "Encounter/<configured Encounter ID>"},
            },
        }

    def _procedure_contract(self, subject_id: int) -> dict[str, Any]:
        mapping = self.mapping
        patient_id = mapping.resource_id("Patient", {"subject_id": subject_id})
        return {
            "id_rule": mapping.resource_id_rule("Procedure"),
            "identifier_system": mapping.procedure_identifier_system,
            "status": mapping.fhir_default("procedure_status"),
            "icd_systems": mapping.procedure_icd_systems,
            "icd_system_rule": "Use each source row's fhir_icd_system exactly",
            "icd_decimal_positions": mapping.procedure_decimal_positions,
            "required_json_shape": {
                "resourceType": "Procedure",
                "id": "<configured Procedure ID>",
                "identifier": [
                    {"system": "<configured system>", "value": "<same id>"}
                ],
                "status": "<configured status>",
                "code": {
                    "coding": [
                        {"system": "<ICD system>", "code": "<ICD code>", "display": "<long_title>"}
                    ]
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "encounter": {"reference": "Encounter/<configured Encounter ID>"},
                "performedDateTime": "<chartdate when present>",
            },
        }

    def _observation_contract(self, subject_id: int) -> dict[str, Any]:
        mapping = self.mapping
        patient_id = mapping.resource_id("Patient", {"subject_id": subject_id})
        ucum_system = str(mapping.fhir_default("ucum_system"))
        return {
            "id_rule": mapping.resource_id_rule("Observation"),
            "identifier_system": mapping.lab_event_identifier_system,
            "code_system": mapping.lab_item_system,
            "status": mapping.fhir_default("observation_status"),
            "category_system": mapping.fhir_default("observation_category_system"),
            "category_code": mapping.fhir_default("observation_category_code"),
            "quantity_system": mapping.fhir_default("ucum_system"),
            "ucum_code_map": mapping.fhir_default("ucum_code_map"),
            "required_json_shape": {
                "resourceType": "Observation",
                "id": "<configured Observation ID>",
                "identifier": [
                    {"system": "<configured system>", "value": "<same id>"}
                ],
                "status": "<configured status>",
                "category": [
                    {"coding": [{"system": "<category system>", "code": "<category code>"}]}
                ],
                "code": {
                    "coding": [{"system": "<item system>", "code": "<itemid>"}]
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "encounter": {"reference": "Encounter/<configured Encounter ID>"},
                "effectiveDateTime": "<charttime with timezone>",
                "valueQuantity": {
                    "value": "<valuenum>",
                    "unit": "<valueuom>",
                    "system": ucum_system,
                    "code": "<UCUM code>",
                },
                "referenceRange": [
                    {
                        "low": {
                            "value": "<ref_range_lower>",
                            "unit": "<valueuom>",
                            "system": ucum_system,
                            "code": "<UCUM code>",
                        },
                        "high": {
                            "value": "<ref_range_upper>",
                            "unit": "<valueuom>",
                            "system": ucum_system,
                            "code": "<UCUM code>",
                        },
                    }
                ],
                "interpretation": [{"text": "<flag when present>"}],
            },
            "value_rule": (
                "Use valueQuantity for valuenum, otherwise valueString for value; "
                "never emit both"
            ),
            "reference_range_rule": (
                "referenceRange MUST be a JSON array when present, never an object"
            ),
            "interpretation_rule": "interpretation MUST be a JSON array when present",
        }

    def _expected_id(self, resource_type: str, row: dict[str, Any]) -> str:
        return self.mapping.resource_id_from_row(resource_type, row)

    def _candidate_errors(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        payload: Any,
        subject_id: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
            return [], ["응답에 resources 배열이 없습니다."]
        errors: list[str] = []
        wrappers = payload["resources"]
        if len(wrappers) != len(rows):
            errors.append(
                f"리소스 수 {len(wrappers)}가 소스 행 수 {len(rows)}와 다릅니다."
            )
        indexed: dict[int, dict[str, Any]] = {}
        for wrapper in wrappers:
            if not isinstance(wrapper, dict) or not isinstance(
                wrapper.get("source_row"), int
            ):
                errors.append("각 항목에는 정수 source_row가 필요합니다.")
                continue
            index = wrapper["source_row"]
            resource = wrapper.get("resource")
            if index in indexed:
                errors.append(f"source_row={index}가 중복됐습니다.")
            elif not isinstance(resource, dict):
                errors.append(f"source_row={index}의 resource가 객체가 아닙니다.")
            else:
                indexed[index] = resource

        resources: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            resource = indexed.get(index)
            if resource is None:
                errors.append(f"source_row={index} 결과가 없습니다.")
                continue
            resources.append(resource)
            self._validate_identity(resource_type, row, resource, subject_id, index, errors)
            self._validate_terminology(resource_type, row, resource, index, errors)
            errors.extend(
                f"source_row={index}: {error}"
                for error in empty_array_errors(resource)
            )
            schema_errors = self.schema.errors(resource)
            semantic_errors = resource_semantic_errors(resource)
            if schema_errors or semantic_errors:
                errors.extend(
                    f"source_row={index}: {error}"
                    for error in [*schema_errors, *semantic_errors]
                )
        return resources, errors

    def _validate_identity(
        self,
        resource_type: str,
        row: dict[str, Any],
        resource: dict[str, Any],
        subject_id: int,
        index: int,
        errors: list[str],
    ) -> None:
        expected_id = self._expected_id(resource_type, row)
        if str(resource.get("id")) != expected_id:
            errors.append(f"source_row={index} id는 {expected_id!r}여야 합니다.")
        identifier_system = self.mapping.identifier_system_for(resource_type)
        identifier = (resource.get("identifier") or [{}])[0]
        if identifier.get("system") != identifier_system:
            errors.append(
                f"source_row={index} identifier.system은 "
                f"{identifier_system!r}여야 합니다."
            )
        if str(identifier.get("value")) != expected_id:
            errors.append(
                f"source_row={index} identifier.value는 리소스 ID "
                f"{expected_id!r}와 같아야 합니다."
            )
        if resource_type != "Patient":
            patient_id = self.mapping.resource_id(
                "Patient", {"subject_id": subject_id}
            )
            expected_subject = f"Patient/{patient_id}"
            if resource.get("subject", {}).get("reference") != expected_subject:
                errors.append(
                    f"source_row={index} subject.reference는 {expected_subject!r}여야 합니다."
                )
        if resource_type in {"Condition", "Procedure", "Observation"}:
            columns = self.mapping.columns_for(resource_type)
            admission_id = row.get(columns.get("admission_id"))
            if admission_id is not None:
                encounter_id = self.mapping.resource_id(
                    "Encounter", {"admission_id": admission_id}
                )
                expected_encounter = f"Encounter/{encounter_id}"
                if resource.get("encounter", {}).get("reference") != expected_encounter:
                    errors.append(
                        f"source_row={index} encounter.reference는 "
                        f"{expected_encounter!r}여야 합니다."
                    )

    def _validate_terminology(
        self,
        resource_type: str,
        row: dict[str, Any],
        resource: dict[str, Any],
        index: int,
        errors: list[str],
    ) -> None:
        if resource_type == "Patient":
            self._validate_patient(row, resource, index, errors)
        elif resource_type == "Observation":
            self._validate_ucum(row, resource, index, errors)
        elif resource_type in {"Condition", "Procedure"}:
            self._validate_icd(resource_type, row, resource, index, errors)

    def _validate_patient(
        self,
        row: dict[str, Any],
        resource: dict[str, Any],
        index: int,
        errors: list[str],
    ) -> None:
        source_gender = row.get(self.mapping.patient_columns.get("gender"))
        expected_gender = self.mapping.patient_gender_map.get(
            str(source_gender or "").strip().upper()
        )
        if expected_gender and resource.get("gender") != expected_gender:
            errors.append(
                f"source_row={index} gender는 매핑된 소스 값 "
                f"{expected_gender!r}여야 합니다."
            )
        if "birthDate" in resource:
            errors.append(
                f"source_row={index} birthDate는 조회한 소스 컬럼에 근거가 없어야 합니다."
            )
        source_death = row.get(self.mapping.patient_columns.get("death_date"))
        actual_death = resource.get("deceasedDateTime")
        if source_death and not (
            actual_death == source_death
            or (
                isinstance(source_death, str)
                and len(source_death) == 10
                and isinstance(actual_death, str)
                and actual_death.startswith(source_death + "T00:00:00")
            )
        ):
            errors.append(
                f"source_row={index} deceasedDateTime은 소스 death_date "
                f"{source_death!r}와 같아야 합니다."
            )
        if not source_death and actual_death is not None:
            errors.append(
                f"source_row={index} deceasedDateTime은 소스 death_date가 없어야 합니다."
            )

    def _validate_ucum(
        self,
        row: dict[str, Any],
        resource: dict[str, Any],
        index: int,
        errors: list[str],
    ) -> None:
        source_unit = row.get(self.mapping.lab_columns.get("unit"))
        if not source_unit:
            return
        expected_ucum = self.mapping.ucum_code(source_unit)
        quantities = [resource.get("valueQuantity")]
        for reference_range in resource.get("referenceRange", []):
            if isinstance(reference_range, dict):
                quantities.extend(
                    [reference_range.get("low"), reference_range.get("high")]
                )
        for quantity in quantities:
            if isinstance(quantity, dict) and quantity.get("code") != expected_ucum:
                errors.append(
                    f"source_row={index} UCUM code는 {expected_ucum!r}여야 하고 "
                    f"unit 표시는 {str(source_unit)!r}로 유지해야 합니다."
                )

    def _validate_icd(
        self,
        resource_type: str,
        row: dict[str, Any],
        resource: dict[str, Any],
        index: int,
        errors: list[str],
    ) -> None:
        columns = (
            self.mapping.diagnosis_columns
            if resource_type == "Condition"
            else self.mapping.procedure_columns
        )
        expected_code = str(row[columns.get("code")])
        expected_system = str(row["fhir_icd_system"])
        coding = (resource.get("code", {}).get("coding") or [{}])[0]
        if coding.get("code") != expected_code:
            errors.append(
                f"source_row={index} ICD code는 정규화된 소스 값 "
                f"{expected_code!r}여야 합니다."
            )
        if coding.get("system") != expected_system:
            errors.append(
                f"source_row={index} ICD system은 {expected_system!r}여야 합니다."
            )
