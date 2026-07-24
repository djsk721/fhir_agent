from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from string import Formatter
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _terminology_systems(
    terminology: dict[str, Any], resource_name: str
) -> dict[int, str]:
    """새 범용 형식과 기존 MIMIC 형식을 모두 읽습니다."""

    generic = terminology.get(f"{resource_name}_systems")
    if generic:
        return {int(version): str(system) for version, system in generic.items()}
    return {
        9: str(terminology[f"{resource_name}_icd9_system"]),
        10: str(terminology[f"{resource_name}_icd10_system"]),
    }


@dataclass(frozen=True)
class TableColumns:
    values: dict[str, str]

    def get(self, logical_name: str) -> str:
        try:
            return self.values[logical_name]
        except KeyError as exc:
            raise ValueError(f"매핑 프로파일에 컬럼이 없습니다: {logical_name}") from exc


@dataclass(frozen=True)
class SourceMappingConfig:
    patient_columns: TableColumns
    admission_columns: TableColumns
    lab_columns: TableColumns
    diagnosis_columns: TableColumns
    procedure_columns: TableColumns
    patient_gender_map: dict[str, str]
    encounter_class_system: str
    encounter_default_class: str
    encounter_class_by_type: dict[str, str]
    encounter_status_when_ended: str
    encounter_status_when_open: str
    patient_identifier_system: str
    encounter_identifier_system: str
    lab_item_system: str
    lab_event_identifier_system: str
    condition_identifier_system: str
    procedure_identifier_system: str
    resource_id_rules: dict[str, dict[str, Any]]
    diagnosis_icd_systems: dict[int, str]
    procedure_icd_systems: dict[int, str]
    diagnosis_decimal_positions: dict[int, int | None]
    procedure_decimal_positions: dict[int, int | None]
    diagnosis_prefix_decimal_positions: dict[int, dict[str, int]]
    procedure_prefix_decimal_positions: dict[int, dict[str, int]]
    fhir_defaults: dict[str, Any]
    allowed_top_level_fields: dict[str, frozenset[str]]
    source_path: Path

    @classmethod
    def load(cls, path: Path) -> "SourceMappingConfig":
        if not path.is_file():
            raise FileNotFoundError(f"원천 매핑 설정 파일을 찾을 수 없습니다: {path}")
        with path.open(encoding="utf-8") as handle:
            payload: dict[str, Any] = json.load(handle)
        columns = payload["source_columns"]
        encounter = payload["encounter"]
        patient = payload["patient"]
        identifiers = payload["identifiers"]
        terminology = payload["terminology"]
        resource_ids = payload["resource_ids"]
        fhir_defaults = payload["fhir_defaults"]
        evaluation = payload["evaluation"]
        config = cls(
            patient_columns=TableColumns(dict(columns["patient"])),
            admission_columns=TableColumns(dict(columns["admission"])),
            lab_columns=TableColumns(dict(columns["lab_event"])),
            diagnosis_columns=TableColumns(dict(columns["diagnosis"])),
            procedure_columns=TableColumns(dict(columns["procedure"])),
            patient_gender_map={
                str(key).upper(): str(value)
                for key, value in patient["gender_map"].items()
            },
            encounter_class_system=str(encounter["class_system"]),
            encounter_default_class=str(encounter["default_class"]),
            encounter_class_by_type={
                str(key).upper(): str(value)
                for key, value in encounter["class_by_admission_type"].items()
            },
            encounter_status_when_ended=str(encounter["status_when_ended"]),
            encounter_status_when_open=str(encounter["status_when_open"]),
            patient_identifier_system=str(identifiers["patient_system"]),
            encounter_identifier_system=str(identifiers["encounter_system"]),
            lab_item_system=str(identifiers["lab_item_system"]),
            lab_event_identifier_system=str(identifiers["lab_event_system"]),
            condition_identifier_system=str(identifiers["condition_system"]),
            procedure_identifier_system=str(identifiers["procedure_system"]),
            resource_id_rules={
                str(key).lower(): dict(value)
                for key, value in resource_ids.items()
            },
            diagnosis_icd_systems=_terminology_systems(terminology, "diagnosis"),
            procedure_icd_systems=_terminology_systems(terminology, "procedure"),
            diagnosis_decimal_positions={
                int(key): (int(value) if value is not None else None)
                for key, value in terminology["diagnosis_decimal_positions"].items()
            },
            procedure_decimal_positions={
                int(key): (int(value) if value is not None else None)
                for key, value in terminology["procedure_decimal_positions"].items()
            },
            diagnosis_prefix_decimal_positions={
                int(version): {str(prefix).upper(): int(position) for prefix, position in rules.items()}
                for version, rules in terminology.get(
                    "diagnosis_prefix_decimal_positions", {}
                ).items()
            },
            procedure_prefix_decimal_positions={
                int(version): {str(prefix).upper(): int(position) for prefix, position in rules.items()}
                for version, rules in terminology.get(
                    "procedure_prefix_decimal_positions", {}
                ).items()
            },
            fhir_defaults=dict(fhir_defaults),
            allowed_top_level_fields={
                str(resource_type): frozenset(
                    str(field_name) for field_name in fields
                )
                for resource_type, fields in evaluation[
                    "allowed_top_level_fields"
                ].items()
            },
            source_path=path,
        )
        config._validate_columns()
        config._validate_fhir_defaults()
        config._validate_patient_rules()
        config._validate_resource_id_rules()
        config._validate_evaluation_rules()
        return config

    def _validate_patient_rules(self) -> None:
        allowed = {"male", "female", "other", "unknown"}
        invalid = set(self.patient_gender_map.values()) - allowed
        if invalid:
            raise ValueError(f"FHIR Patient gender 코드가 올바르지 않습니다: {sorted(invalid)}")

    def _validate_resource_id_rules(self) -> None:
        allowed_fields = {
            "patient": set(self.patient_columns.values),
            "encounter": set(self.admission_columns.values),
            "condition": set(self.diagnosis_columns.values),
            "procedure": set(self.procedure_columns.values),
            "observation": set(self.lab_columns.values),
        }
        missing = set(allowed_fields) - set(self.resource_id_rules)
        if missing:
            raise ValueError(f"resource_ids 규칙 누락: {sorted(missing)}")
        for resource_name, allowed in allowed_fields.items():
            rule = self.resource_id_rules[resource_name]
            template = rule.get("template")
            candidates = rule.get("candidates")
            if bool(template) == bool(candidates):
                raise ValueError(
                    f"resource_ids.{resource_name}은 template 또는 candidates 하나가 필요합니다."
                )
            fields = (
                {
                    field_name
                    for _, field_name, _, _ in Formatter().parse(str(template))
                    if field_name
                }
                if template
                else {str(item) for item in candidates}
            )
            unknown = fields - allowed
            if unknown:
                raise ValueError(
                    f"resource_ids.{resource_name}의 알 수 없는 논리 필드: {sorted(unknown)}"
                )

    def resource_id_rule(self, resource_name: str) -> dict[str, Any]:
        try:
            return dict(self.resource_id_rules[resource_name.lower()])
        except KeyError as exc:
            raise ValueError(f"FHIR 리소스 ID 규칙이 없습니다: {resource_name}") from exc

    def columns_for(self, resource_name: str) -> TableColumns:
        """FHIR 리소스에 대응하는 원천 컬럼 그룹을 반환합니다."""

        groups = {
            "Patient": self.patient_columns,
            "Encounter": self.admission_columns,
            "Condition": self.diagnosis_columns,
            "Procedure": self.procedure_columns,
            "Observation": self.lab_columns,
        }
        try:
            return groups[resource_name]
        except KeyError as exc:
            raise ValueError(
                f"지원하지 않는 FHIR 리소스입니다: {resource_name}"
            ) from exc

    def identifier_system_for(self, resource_name: str) -> str:
        """리소스별 identifier.system 정책을 한 곳에서 제공합니다."""

        systems = {
            "Patient": self.patient_identifier_system,
            "Encounter": self.encounter_identifier_system,
            "Condition": self.condition_identifier_system,
            "Procedure": self.procedure_identifier_system,
            "Observation": self.lab_event_identifier_system,
        }
        try:
            return systems[resource_name]
        except KeyError as exc:
            raise ValueError(
                f"지원하지 않는 FHIR 리소스입니다: {resource_name}"
            ) from exc

    def resource_id_from_row(
        self, resource_name: str, row: dict[str, Any]
    ) -> str:
        """설정된 ID 규칙과 원천 컬럼 매핑으로 행의 FHIR ID를 생성합니다."""

        rule = self.resource_id_rule(resource_name)
        template = rule.get("template")
        logical_names = (
            [
                field_name
                for _, field_name, _, _ in Formatter().parse(str(template))
                if field_name
            ]
            if template
            else [str(item) for item in rule.get("candidates", [])]
        )
        columns = self.columns_for(resource_name)
        return self.resource_id(
            resource_name,
            {
                logical_name: row.get(columns.get(logical_name))
                for logical_name in logical_names
            },
        )

    def resource_id(self, resource_name: str, logical_values: dict[str, Any]) -> str:
        rule = self.resource_id_rule(resource_name)
        if rule.get("template"):
            normalized_values = {
                key: int(value)
                if isinstance(value, float) and value.is_integer()
                else value
                for key, value in logical_values.items()
            }
            try:
                value = str(rule["template"]).format(**normalized_values)
            except KeyError as exc:
                raise ValueError(
                    f"{resource_name} ID 생성에 필요한 값이 없습니다: {exc.args[0]}"
                ) from exc
            if not value or "None" in value:
                raise ValueError(f"{resource_name} ID를 생성할 수 없습니다: {logical_values}")
            return value
        for candidate in rule.get("candidates", []):
            value = logical_values.get(str(candidate))
            if value is not None and value != "":
                if isinstance(value, float) and value.is_integer():
                    return str(int(value))
                return str(value)
        raise ValueError(f"{resource_name} ID 후보 값이 모두 비어 있습니다.")

    def _validate_columns(self) -> None:
        required = {
            "patient": (self.patient_columns, {"subject_id", "gender", "death_date"}),
            "admission": (
                self.admission_columns,
                {"subject_id", "admission_id", "admission_type", "start_time", "end_time"},
            ),
            "lab_event": (
                self.lab_columns,
                {
                    "subject_id",
                    "admission_id",
                    "event_id",
                    "row_id",
                    "item_id",
                    "event_time",
                    "numeric_value",
                    "text_value",
                    "unit",
                    "reference_low",
                    "reference_high",
                    "interpretation",
                },
            ),
            "diagnosis": (
                self.diagnosis_columns,
                {"subject_id", "admission_id", "sequence", "code", "version", "display"},
            ),
            "procedure": (
                self.procedure_columns,
                {"subject_id", "admission_id", "sequence", "date", "code", "version", "display"},
            ),
        }
        for section, (group, expected) in required.items():
            missing = expected - set(group.values)
            if missing:
                raise ValueError(
                    f"매핑 프로파일 source_columns.{section} 누락: {sorted(missing)}"
                )
            for value in group.values.values():
                if not _IDENTIFIER.fullmatch(value):
                    raise ValueError(f"안전하지 않은 소스 컬럼명입니다: {value!r}")

    def _validate_fhir_defaults(self) -> None:
        required = {
            "observation_status",
            "observation_category_system",
            "observation_category_code",
            "condition_category_system",
            "condition_category_code",
            "procedure_status",
            "ucum_system",
            "observation_interpretation_system",
            "observation_interpretation",
        }
        missing = required - set(self.fhir_defaults)
        if missing:
            raise ValueError(
                f"매핑 프로파일 fhir_defaults 누락: {sorted(missing)}"
            )

    def _validate_evaluation_rules(self) -> None:
        missing = {
            "Patient", "Encounter", "Condition", "Procedure", "Observation"
        } - set(self.allowed_top_level_fields)
        if missing:
            raise ValueError(
                "evaluation.allowed_top_level_fields 리소스 누락: "
                f"{sorted(missing)}"
            )

    def allowed_top_level_for(self, resource_name: str) -> frozenset[str]:
        try:
            return self.allowed_top_level_fields[resource_name]
        except KeyError as exc:
            raise ValueError(
                f"평가 필드 정책이 없는 FHIR 리소스입니다: {resource_name}"
            ) from exc

    def diagnosis_icd_system(self, version: Any) -> str:
        return self._icd_system(self.diagnosis_icd_systems, version, "진단")

    def procedure_icd_system(self, version: Any) -> str:
        return self._icd_system(self.procedure_icd_systems, version, "처치")

    def format_diagnosis_code(self, code: Any, version: Any) -> str:
        return self._format_icd_code(
            code,
            version,
            self.diagnosis_decimal_positions,
            self.diagnosis_prefix_decimal_positions,
        )

    def format_procedure_code(self, code: Any, version: Any) -> str:
        return self._format_icd_code(
            code,
            version,
            self.procedure_decimal_positions,
            self.procedure_prefix_decimal_positions,
        )

    @staticmethod
    def _icd_system(systems: dict[int, str], version: Any, label: str) -> str:
        try:
            return systems[int(version)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"지원하지 않는 {label} ICD 버전입니다: {version!r}") from exc

    @staticmethod
    def _format_icd_code(
        code: Any,
        version: Any,
        decimal_positions: dict[int, int | None],
        prefix_decimal_positions: dict[int, dict[str, int]],
    ) -> str:
        normalized = str(code or "").strip().replace(".", "")
        try:
            normalized_version = int(version)
            position = decimal_positions[normalized_version]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"지원하지 않는 ICD 버전입니다: {version!r}") from exc
        for prefix, prefix_position in prefix_decimal_positions.get(
            normalized_version, {}
        ).items():
            if normalized.upper().startswith(prefix):
                position = prefix_position
                break
        if position is None or len(normalized) <= position:
            return normalized
        return f"{normalized[:position]}.{normalized[position:]}"

    def fhir_default(self, key: str) -> Any:
        try:
            return self.fhir_defaults[key]
        except KeyError as exc:
            raise ValueError(f"매핑 프로파일 fhir_defaults 누락: {key}") from exc

    def ucum_code(self, display_unit: Any) -> str:
        unit = str(display_unit or "").strip()
        rules = self.fhir_defaults.get("ucum_code_map", {})
        return str(rules.get(unit, unit))


# 이전 import를 사용하는 외부 코드와 테스트를 위한 호환 별칭입니다.
MimicMappingConfig = SourceMappingConfig
