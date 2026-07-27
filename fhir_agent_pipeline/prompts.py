"""LLM FHIR 생성 프롬프트를 중앙 관리합니다."""


FHIR_GENERATION_SYSTEM_PROMPT = """You generate final FHIR R4 JSON resources from PostgreSQL query rows.
The returned resources are used directly by the conversion pipeline.

Rules:
- Use only facts present in the supplied source rows and retrieved FHIR Schema context.
- Never invent identifiers, codes, timestamps, units, values, names, or references.
- Produce exactly one resource for each source row.
- Preserve the required source_row index for provenance.
- Follow the supplied ID and reference contract exactly.
- Never emit an empty JSON array; omit an optional property when it has no values.
- Use valid FHIR R4 JSON and return JSON only.
"""


FHIR_GENERATION_USER_PROMPT_TEMPLATE = """Target resourceType: {resource_type}

Source rows from LLM-generated, read-only SQL:
{source_rows}

Required ID and reference contract:
{identity_contract}

FHIR R4 Schema context retrieved from VectorDB:
{fhir_context}

{correction_context}

Return exactly this JSON shape:
{{
  "resources": [
    {{"source_row": 0, "resource": {{"resourceType": "{resource_type}"}}}}
  ]
}}

The resources array length must equal the source row count. source_row is zero-based within this batch.
"""
