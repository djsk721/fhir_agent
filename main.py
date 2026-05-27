from __future__ import annotations

import json
from typing import Any

from app.main import app as fastapi_app
from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.pipeline import MultiAgentFhirPipeline
from fhir_agent_pipeline.retrieval import RuntimeRetriever
from fhir_agent_pipeline.sql_runtime import SafeSqlRuntime


app = fastapi_app


def main() -> dict[str, Any]:
    """Seed 기반 SQL 계획부터 Bundle 생성/검증까지 전체 파이프라인을 실행합니다."""

    settings = load_settings()
    seed_payload: dict[str, Any] = {
        "patient": {"subject_id": settings.patient_subject_id}
        if settings.patient_subject_id is not None
        else {},
        "admission": None,
        "lab_event": None,
    }

    pipeline = MultiAgentFhirPipeline(
        settings,
        retriever=RuntimeRetriever(settings),
        sql_runtime=SafeSqlRuntime(settings),
    )
    result = pipeline.run(seed_payload)
    output = {
        "corrected_resource": result.fhir_payload,
        "corrections": result.corrections,
    }

    if result.verification_errors:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        raise RuntimeError(
            "FHIR verification failed: " + "; ".join(result.verification_errors)
        )

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output


if __name__ == "__main__":
    _ = main()
