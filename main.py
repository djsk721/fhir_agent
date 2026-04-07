from __future__ import annotations

import json

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.db import fetch_patient_demo_row
from fhir_agent_pipeline.pipeline import MultiAgentFhirPipeline

def main() -> dict:
    """DB 추출부터 Bundle 생성/검증까지 전체 파이프라인을 실행합니다."""

    settings = load_settings()
    patient_demo_row = fetch_patient_demo_row(settings)

    pipeline = MultiAgentFhirPipeline(settings)
    result = pipeline.run(patient_demo_row)

    if result.verification_errors:
        print(json.dumps(result.fhir_payload, ensure_ascii=False, indent=2))
        raise RuntimeError(
            "FHIR verification failed: " + "; ".join(result.verification_errors)
        )

    print(json.dumps(result.fhir_payload, ensure_ascii=False, indent=2))
    return result.fhir_payload


if __name__ == "__main__":
    main()
