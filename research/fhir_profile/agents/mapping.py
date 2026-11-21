from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re
from typing import Any, Protocol

from fhir_agent_pipeline.json_utils import load_llm_json

from ..profiles.schema import bind_profile_identity, canonical_identity, validate_profile
from ..contracts import ProfileGenerationError


EXECUTION_CONTRACT = """id and resourceType are managed by Renderer. Never include them in mappings.
Use only documented column names as source_field. Scalar constant uses value directly on the mapping, never a nested code or display part.
lookup uses source_field and table {raw_code: FHIR_code}.
reference targets the Reference object itself (e.g. subject), uses resource_type and source_field or identity. Use the declared foreign keys and the related resource's ID encoding.
Respect declared identity_fields and foreign keys. When identity_contract is active, the renderer binds the root ID; do not invent or change it. A record locator in record_identity_fields is not a business key for a referenced resource.
Only coding/codeable_concept uses nested system, code and display parts, e.g. code {operation: string, source_field: column_name}.
Choose a single object-level rule or child-level rules for an object; never both. With a Coding rule put system and code inside the same rule; do not also map the object's child paths.
when uses {field: column_name, present: true/false}, {field: column_name, equals: value}, or {field: column_name, in: [values]}.
Use semantic field descriptions and FHIR enum values. A death field must retain its death meaning.
Use the supplied timezone policy for naive timestamps. Preserve offsets already present.
Unavailable source values must remain unavailable. Never interpret missing information as false or zero.
FHIR choice fields are alternatives per row. When one column represents multiple value types, use mutually exclusive when conditions from the documented discriminator. Apply the same numeric guard to value, unit, and other children of a quantity. Preserve textual source values in a valid text alternative, rather than parsing all values as numbers.
Check all required target fields in fhir_context before returning. Never present a local code as a code from a standard system without a justified mapping.
Only index FHIR array elements. A scalar Coding uses code and system directly, not a coding array.
Use a documented enum value for required status. Use unknown only where the FHIR field allows it and source status is unavailable.
When repairing, edit the supplied previous draft, retaining its correct mappings and removing unsupported optional rules.
"""


def summarize_issues(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Group repeated row errors for the model. Full issues stay in the run artifacts."""
    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    feedback = []
    for issue in issues:
        category = issue.get("category", "")
        if category == "revision_contract":
            feedback.append(issue)
            continue
        path = re.sub(r"\[\d+\]|/\*[^*]*\*/", "", issue.get("path", ""))
        message = re.sub(r"^record\[\d+\] ?", "", issue.get("message", ""))
        # Paths and check IDs describe the rule, even when row-specific values differ.
        key = (category, issue.get("check_id", ""), path, message[:200] if not path else "")
        bucket = buckets.setdefault(key, {
            "category": category, "path": path, "check_id": issue.get("check_id"),
            "count": 0, "examples": [],
        })
        bucket["count"] += 1
        example = {"message": message[:320]}
        for name in ("expected_value", "actual_value", "expected_missing"):
            if name in issue:
                value = issue[name]
                example[name] = value[:160] if isinstance(value, str) else value
        if len(bucket["examples"]) < 2 and example not in bucket["examples"]:
            bucket["examples"].append(example)
    return {"issue_count": len(issues) - len(feedback), "distinct_issue_count": len(buckets),
            "groups": list(buckets.values())[:24], "previous_revision_errors": feedback[-1:]}


MAPPING_SYSTEM_PROMPT = """You create a reusable FHIR R4 Conversion Profile.
Return one JSON object only.
Do not return SQL, database queries, Python, or patient-specific values.
Use only documented source fields and the allowed declarative operations:
string, integer, decimal, date, datetime, constant, lookup, reference, coding,
codeable_concept, identifier, copy.
"""


class ProfileClient(Protocol):
    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """LLM 응답 문자열을 반환합니다."""


def select_profile_examples(
    records: tuple[dict[str, Any], ...], limit: int, group_fields: list[str],
) -> list[dict[str, Any]]:
    """Cover source-declared types with deterministic, preparation-only examples."""
    if limit < 0:
        raise ValueError("sample_rows는 음수가 될 수 없습니다.")
    if not group_fields or not limit:
        return list(records[:limit])
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for record in records:
        group = tuple(json.dumps(record.get(field), sort_keys=True, default=str) for field in group_fields)
        examples = groups.setdefault(group, [])
        if len(examples) < limit:
            examples.append(record)
    pending = deque(iter(examples) for examples in groups.values())
    selected = []
    while pending and len(selected) < limit:
        example_generator = pending.popleft()
        if (example := next(example_generator, None)) is not None:
            selected.append(example)
            pending.append(example_generator)
    return selected


class ProfileMappingAgent:
    """Source metadata와 FHIR 문맥으로 Conversion Profile만 생성합니다."""

    def __init__(
        self, client: ProfileClient, prompt_version: str = "v1",
        system_prompt: str = MAPPING_SYSTEM_PROMPT,
        *, schema_path: str | Path | None = None,
    ) -> None:
        self.client = client
        self.prompt_version = prompt_version
        self.system_prompt = system_prompt
        self.schema_path = schema_path

    def generate(
        self,
        metadata: dict[str, Any],
        fhir_context: str,
        target_resource: str,
        *, previous_error: str | dict[str, Any] | None = None, source_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = json.dumps(
            {
                "target_resource": target_resource,
                "source_metadata": metadata,
                "fhir_context": fhir_context,
                **({"source_context": source_context} if source_context is not None else {}),
                "previous_profile_validation_error": previous_error,
                "execution_contract": EXECUTION_CONTRACT,
                "required_shape": {
                    "profile_id": "string",
                    "profile_version": "string",
                    "target_resource": target_resource,
                    "fhir_version": "4.0.1",
                    "source": {"dataset": "string", "table": "string"},
                    "identity": (canonical_identity(metadata) or {"source_fields": ["string"]}),
                    "mappings": [],
                },
            },
            ensure_ascii=False,
        )
        generate = getattr(self.client, "chat_profile", self.client.chat)
        payload = load_llm_json(generate(self.system_prompt, prompt))
        if not isinstance(payload, dict):
            raise ValueError("Mapping Agent 응답은 JSON 객체여야 합니다.")
        try:
            return validate_profile(bind_profile_identity(payload, metadata), metadata, schema_path=self.schema_path)
        except ValueError as exc:
            raise ProfileGenerationError(str(exc), payload) from exc

    def revise(
        self, profile: dict[str, Any], issues: list[dict[str, Any]],
        metadata: dict[str, Any], fhir_context: str, *, source_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """정답 FHIR 없이 calibration 검증 오류만으로 Profile을 수정합니다."""
        prompt = json.dumps({
            "task": "Correct the reusable Conversion Profile using validation feedback.",
            "prompt_version": self.prompt_version,
            "source_metadata": metadata,
            "fhir_context": fhir_context,
            **({"source_context": source_context} if source_context is not None else {}),
            "current_profile": profile,
            "validation_issues": summarize_issues(issues),
            "execution_contract": EXECUTION_CONTRACT,
            "rules": [
                "Return a complete Conversion Profile, never individual FHIR records.",
                "Preserve source identity and clinical meaning. Never invent missing clinical values.",
                "Do not change the dataset, source table, or target resource.",
                "Only use documented source fields and justified terminology.",
            ],
        }, ensure_ascii=False, default=str)
        generate = getattr(self.client, "chat_profile", self.client.chat)
        draft = load_llm_json(generate(self.system_prompt, prompt))
        try:
            revised = validate_profile(bind_profile_identity(draft, metadata), metadata, schema_path=self.schema_path)
        except ValueError as exc:
            raise ProfileGenerationError(str(exc), draft) from exc
        for key in ("target_resource", "source"):
            if revised[key] != profile[key]:
                raise ValueError(f"Profile 교정에서 {key}를 변경할 수 없습니다.")
        return revised
