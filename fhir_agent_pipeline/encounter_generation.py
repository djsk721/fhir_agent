from __future__ import annotations

from typing import Any

from .payload_utils import extract_admissions


def normalize_mapping_payload(mapping_payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(mapping_payload)
    encounter_contexts = _dedupe_encounter_contexts(
        _extract_encounter_contexts(mapping_payload)
    )

    if encounter_contexts:
        normalized["encounter_contexts"] = encounter_contexts
        normalized["encounter_context"] = encounter_contexts[0]
    else:
        normalized.pop("encounter_contexts", None)

    return normalized


def build_encounter_generation_inputs(
    raw_payload: dict[str, Any], mapping_payload: dict[str, Any]
) -> list[dict[str, dict[str, Any]]]:
    admissions = extract_admissions(raw_payload)
    encounter_rows = _extract_encounter_rows(raw_payload)
    encounter_contexts = _extract_encounter_contexts(mapping_payload)
    encounter_inputs: list[dict[str, dict[str, Any]]] = []

    if admissions:
        for index, admission in enumerate(admissions):
            base_context = (
                encounter_contexts[index]
                if index < len(encounter_contexts)
                else (encounter_contexts[0] if encounter_contexts else {})
            )
            matching_rows = _match_encounter_rows(encounter_rows, admission)
            encounter_context = {
                **base_context,
                **admission,
            }
            if matching_rows:
                encounter_context["sql_rows"] = matching_rows

            encounter_mapping_payload = _build_encounter_mapping_payload(
                mapping_payload, encounter_context
            )

            encounter_raw_payload = dict(raw_payload)
            encounter_raw_payload["admission"] = admission
            encounter_raw_payload["admissions"] = [admission]
            if isinstance(encounter_raw_payload.get("sql_evidence"), dict):
                encounter_sql_evidence = dict(encounter_raw_payload["sql_evidence"])
                encounter_sql_evidence["Encounter"] = matching_rows or [admission]
                encounter_raw_payload["sql_evidence"] = encounter_sql_evidence

            encounter_inputs.append(
                {
                    "mapping_payload": encounter_mapping_payload,
                    "raw_payload": encounter_raw_payload,
                }
            )

        return encounter_inputs

    if encounter_rows:
        for index, row in enumerate(encounter_rows):
            base_context = (
                encounter_contexts[index]
                if index < len(encounter_contexts)
                else (encounter_contexts[0] if encounter_contexts else {})
            )
            encounter_context = {
                **base_context,
                **row,
            }
            encounter_mapping_payload = _build_encounter_mapping_payload(
                mapping_payload, encounter_context
            )

            encounter_raw_payload = dict(raw_payload)
            encounter_raw_payload["admission"] = row
            encounter_raw_payload["admissions"] = [row]
            if isinstance(encounter_raw_payload.get("sql_evidence"), dict):
                encounter_sql_evidence = dict(encounter_raw_payload["sql_evidence"])
                encounter_sql_evidence["Encounter"] = [row]
                encounter_raw_payload["sql_evidence"] = encounter_sql_evidence

            encounter_inputs.append(
                {
                    "mapping_payload": encounter_mapping_payload,
                    "raw_payload": encounter_raw_payload,
                }
            )

        return encounter_inputs

    return [
        {
            "mapping_payload": _build_encounter_mapping_payload(
                mapping_payload,
                _fallback_encounter_context(mapping_payload),
            ),
            "raw_payload": raw_payload,
        }
    ]


def _extract_encounter_rows(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    sql_evidence = raw_payload.get("sql_evidence")
    if not isinstance(sql_evidence, dict):
        return []
    raw_encounter_rows = sql_evidence.get("Encounter")
    if not isinstance(raw_encounter_rows, list):
        return []
    return [row for row in raw_encounter_rows if isinstance(row, dict)]


def _extract_encounter_contexts(mapping_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_contexts = mapping_payload.get("encounter_contexts")
    if isinstance(raw_contexts, list):
        contexts = [context for context in raw_contexts if isinstance(context, dict)]
        if contexts:
            return contexts

    encounter_context = mapping_payload.get("encounter_context")
    if isinstance(encounter_context, dict):
        return [encounter_context]

    return []


def _dedupe_encounter_contexts(
    encounter_contexts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for encounter_context in encounter_contexts:
        key = _encounter_context_identity_key(encounter_context)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(encounter_context)

    return deduped


def _encounter_context_identity_key(encounter_context: dict[str, Any]) -> str:
    hadm_id = encounter_context.get("hadm_id")
    if hadm_id is not None:
        return f"hadm:{hadm_id}"

    subject_id = encounter_context.get("subject_id")
    admittime = encounter_context.get("admittime")
    return f"fallback:{subject_id}:{admittime}:{str(encounter_context)}"


def _match_encounter_rows(
    encounter_rows: list[dict[str, Any]], admission: dict[str, Any]
) -> list[dict[str, Any]]:
    hadm_id = admission.get("hadm_id")
    if hadm_id is None:
        return []

    return [row for row in encounter_rows if str(row.get("hadm_id")) == str(hadm_id)]


def _fallback_encounter_context(mapping_payload: dict[str, Any]) -> dict[str, Any]:
    encounter_context = mapping_payload.get("encounter_context")
    return encounter_context if isinstance(encounter_context, dict) else {}


def _build_encounter_mapping_payload(
    mapping_payload: dict[str, Any], encounter_context: dict[str, Any]
) -> dict[str, Any]:
    encounter_mapping_payload: dict[str, Any] = {}
    for key in ("patient_demographics", "terminology_mapping", "notes"):
        value = mapping_payload.get(key)
        if value is not None:
            encounter_mapping_payload[key] = value

    encounter_mapping_payload["encounter_context"] = encounter_context
    encounter_mapping_payload["encounter_contexts"] = [encounter_context]
    return encounter_mapping_payload
