from __future__ import annotations

from typing import Any

from app.models.requests import MappingRequest, MimicPayload
from app.models.responses import DebugContext, MappingResponse
from app.services.runtime import get_runtime_retriever, get_settings
from app.services.schema_service import SchemaService
from fhir_agent_pipeline.agents import MappingAgent, SqlPlannerAgent
from fhir_agent_pipeline.sql_runtime import (
    SafeSqlRuntime,
    SqlQueryPlan,
    merge_sql_evidence,
)


class MappingService:
    def __init__(self, schema_service: SchemaService) -> None:
        self._schema_service = schema_service
        self._sql_runtime = SafeSqlRuntime(get_settings())
        self._sql_planner = SqlPlannerAgent(get_settings())

    def _build_payload(self, request: MappingRequest) -> dict[str, Any]:
        if request.payload is not None:
            payload = request.payload.model_dump()
            if request.itemid is not None:
                lab_event = payload.get("lab_event")
                if isinstance(lab_event, dict):
                    payload["lab_event"] = {**lab_event, "itemid": request.itemid}
                else:
                    payload["lab_event"] = {"itemid": request.itemid}

            if request.notes:
                lab_event = payload.get("lab_event")
                if isinstance(lab_event, dict):
                    payload["lab_event"] = {**lab_event, "notes": request.notes}
                else:
                    payload["lab_event"] = {"notes": request.notes}

            return payload

        fallback_payload = MimicPayload(
            patient={},
            admission=None,
            lab_event={"itemid": request.itemid, "notes": request.notes},
        )
        return fallback_payload.model_dump()

    def explain_mapping(self, request: MappingRequest) -> MappingResponse:
        payload = self._build_payload(request)
        retrieval_context = get_runtime_retriever().build_context(
            payload,
            ["Observation"],
        )
        sql_evidence, sql_trace = self._build_sql_evidence(payload, retrieval_context)
        mapping_payload = MappingAgent(get_settings()).run(
            merge_sql_evidence(payload, sql_evidence),
            rag_context=retrieval_context.mapping_prompt_context,
            sql_evidence=sql_evidence,
        )

        notes = mapping_payload.get("notes")
        terminology_mapping = mapping_payload.get("terminology_mapping", {})
        explanation_parts = []
        if request.itemid is not None:
            explanation_parts.append(f"itemid={request.itemid}")
        if notes:
            explanation_parts.append(str(notes))
        if terminology_mapping:
            explanation_parts.append(f"terminology={terminology_mapping}")

        warnings = []
        if request.payload is None:
            warnings.append(
                "Mapping was inferred from a minimal itemid-only payload; provide full MIMIC JSON for stronger context."
            )

        debug_context = DebugContext(
            agent_sequence=["Schema Agent", "Mapping Agent"],
            rag_context=sql_trace + retrieval_context.trace,
            schema_context=self._schema_service.build_schema_context(["Observation"]),
            source_payload_summary={
                "has_patient": bool(payload.get("patient")),
                "has_admission": payload.get("admission") is not None,
                "has_lab_event": payload.get("lab_event") is not None,
            },
            warnings=warnings,
        )

        return MappingResponse(
            itemid=request.itemid,
            mapping_payload=mapping_payload,
            mapping_explanation=" | ".join(explanation_parts)
            if explanation_parts
            else "No mapping notes returned.",
            debug_context=debug_context,
        )

    def _build_sql_evidence(
        self,
        payload: dict[str, Any],
        retrieval_context: Any,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        try:
            plan = self._sql_planner.run(
                resource_type="Observation",
                seed_context=self._sql_runtime.build_sql_seed_context(
                    payload, "Observation"
                ),
                sql_schema_context=self._sql_runtime.build_schema_context(
                    "Observation"
                ),
                rag_context=retrieval_context.mapping_prompt_context,
            )
            execution = self._sql_runtime.execute_plan(
                SqlQueryPlan(
                    resource_type="Observation",
                    sql=plan.sql,
                    params=plan.params,
                    tables=plan.tables,
                    rationale=plan.rationale,
                )
            )
            return {"Observation": execution.rows}, execution.trace
        except Exception as exc:
            return {"Observation": []}, [
                {
                    "type": "sql_plan",
                    "resource_type": "Observation",
                    "status": "error",
                    "detail": str(exc),
                }
            ]
