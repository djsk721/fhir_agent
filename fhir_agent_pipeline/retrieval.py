from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .config import Settings


@dataclass(frozen=True)
class RetrievedChunk:
    source: str
    content: str
    score: float | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RetrievalContext:
    mapping_prompt_context: str
    resource_prompt_context: dict[str, str]
    trace: list[dict[str, Any]]


class RuntimeRetriever:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._schema_json_path = Path(settings.fhir_schema_json_path)
        self._schema_store_path = Path(settings.fhir_schema_db_dir)
        self._medical_store_path = Path(settings.mimic_knowledge_db_dir)
        self._vector_stores: dict[str, Any] = {}
        self._schema_document: dict[str, Any] | None = None

    def build_context(
        self, raw_payload: dict[str, Any], resource_types: list[str]
    ) -> RetrievalContext:
        if not self._settings.rag_enabled:
            return RetrievalContext(
                mapping_prompt_context="RAG runtime is disabled by configuration.",
                resource_prompt_context={
                    resource_type: "RAG runtime is disabled by configuration."
                    for resource_type in resource_types
                },
                trace=[
                    {
                        "type": "runtime",
                        "status": "disabled",
                        "corpus": "all",
                        "detail": "RAG retrieval is disabled by settings.",
                    }
                ],
            )

        mapping_query = self._build_mapping_query(raw_payload, resource_types)
        mapping_chunks, mapping_trace = self._retrieve_for_query(
            query=mapping_query,
            resource_types=resource_types,
            purpose="mapping",
        )

        resource_prompt_context: dict[str, str] = {}
        trace = list(mapping_trace)

        for resource_type in resource_types:
            resource_query = self._build_resource_query(resource_type, raw_payload)
            resource_chunks, resource_trace = self._retrieve_for_query(
                query=resource_query,
                resource_types=[resource_type],
                purpose=f"generation:{resource_type}",
            )
            trace.extend(resource_trace)
            resource_prompt_context[resource_type] = self._format_chunks(
                resource_chunks,
                fallback_message=f"No retrieval context found for {resource_type}; rely on raw mapping evidence only.",
            )

        return RetrievalContext(
            mapping_prompt_context=self._format_chunks(
                mapping_chunks,
                fallback_message="No retrieval context found; rely on raw evidence and local validation only.",
            ),
            resource_prompt_context=resource_prompt_context,
            trace=trace,
        )

    def _build_mapping_query(
        self, raw_payload: dict[str, Any], resource_types: list[str]
    ) -> str:
        patient = raw_payload.get("patient") or {}
        admission = raw_payload.get("admission") or {}
        lab_event = raw_payload.get("lab_event") or {}
        details = [
            f"resource_types={', '.join(resource_types)}",
            f"patient_keys={', '.join(sorted(patient.keys())) or 'none'}",
            f"admission_keys={', '.join(sorted(admission.keys())) or 'none'}",
            f"lab_itemid={lab_event.get('itemid', 'unknown')}",
            f"lab_label={lab_event.get('label', 'unknown')}",
        ]
        return "FHIR mapping guidance for " + " | ".join(details)

    def _build_resource_query(
        self, resource_type: str, raw_payload: dict[str, Any]
    ) -> str:
        lab_event = raw_payload.get("lab_event") or {}
        return (
            f"FHIR R4 {resource_type} structure, required fields, valid references, "
            f"lab label={lab_event.get('label', 'unknown')}, itemid={lab_event.get('itemid', 'unknown')}"
        )

    def _retrieve_for_query(
        self, query: str, resource_types: list[str], purpose: str
    ) -> tuple[list[RetrievedChunk], list[dict[str, Any]]]:
        chunks: list[RetrievedChunk] = []
        trace: list[dict[str, Any]] = []

        schema_chunks, schema_trace = self._retrieve_from_store(
            store_name="schema",
            store_path=self._schema_store_path,
            query=query,
            resource_types=resource_types,
            purpose=purpose,
        )
        chunks.extend(schema_chunks)
        trace.append(schema_trace)

        if self._medical_store_path.exists():
            medical_chunks, medical_trace = self._retrieve_from_store(
                store_name="medical",
                store_path=self._medical_store_path,
                query=query,
                resource_types=resource_types,
                purpose=purpose,
            )
            chunks.extend(medical_chunks)
            trace.append(medical_trace)

        if chunks:
            return chunks, trace

        fallback_chunks = self._fallback_schema_chunks(resource_types)
        trace.append(
            {
                "type": "retrieval",
                "corpus": "schema_json",
                "purpose": purpose,
                "status": "fallback_json",
                "query": query,
                "hit_count": len(fallback_chunks),
                "detail": "Used local FHIR schema JSON fallback because vector retrieval returned no usable hits.",
            }
        )
        return fallback_chunks, trace

    def _retrieve_from_store(
        self,
        store_name: str,
        store_path: Path,
        query: str,
        resource_types: list[str],
        purpose: str,
    ) -> tuple[list[RetrievedChunk], dict[str, Any]]:
        if not store_path.exists():
            return [], {
                "type": "retrieval",
                "corpus": store_name,
                "purpose": purpose,
                "status": "unavailable",
                "query": query,
                "hit_count": 0,
                "detail": f"Vector store path does not exist: {store_path}",
            }

        try:
            vector_store = self._get_vector_store(store_name, store_path)
        except Exception as exc:
            return [], {
                "type": "retrieval",
                "corpus": store_name,
                "purpose": purpose,
                "status": "error",
                "query": query,
                "hit_count": 0,
                "detail": str(exc),
            }

        if vector_store is None:
            return [], {
                "type": "retrieval",
                "corpus": store_name,
                "purpose": purpose,
                "status": "unavailable",
                "query": query,
                "hit_count": 0,
                "detail": "Chroma or embedding runtime is unavailable; falling back to local schema JSON.",
            }

        try:
            documents = vector_store.similarity_search(
                query, k=self._settings.rag_top_k
            )
        except Exception as exc:
            if not self._settings.rag_fail_open:
                raise
            return [], {
                "type": "retrieval",
                "corpus": store_name,
                "purpose": purpose,
                "status": "error",
                "query": query,
                "hit_count": 0,
                "detail": f"Vector search failed: {exc}",
            }

        chunks = [
            RetrievedChunk(
                source=str(getattr(document, "metadata", {}).get("source", store_name)),
                content=str(getattr(document, "page_content", "")),
                score=None,
                metadata={
                    **getattr(document, "metadata", {}),
                    "resource_types": resource_types,
                },
            )
            for document in documents
        ]
        return chunks, {
            "type": "retrieval",
            "corpus": store_name,
            "purpose": purpose,
            "status": "vector",
            "query": query,
            "hit_count": len(chunks),
            "detail": f"Retrieved {len(chunks)} document(s) from {store_name} vector store.",
        }

    def _get_vector_store(self, store_name: str, store_path: Path) -> Any | None:
        cache_key = f"{store_name}:{store_path}"
        if cache_key in self._vector_stores:
            return self._vector_stores[cache_key]

        chroma_class = self._resolve_chroma_class()
        embeddings = self._resolve_embeddings()
        if chroma_class is None or embeddings is None:
            self._vector_stores[cache_key] = None
            return None

        vector_store = chroma_class(
            persist_directory=str(store_path),
            embedding_function=embeddings,
        )
        self._vector_stores[cache_key] = vector_store
        return vector_store

    def _resolve_chroma_class(self) -> Any | None:
        try:
            from langchain_chroma import Chroma

            return Chroma
        except Exception:
            return None

    def _resolve_embeddings(self) -> Any | None:
        provider = self._settings.rag_embedding_provider

        if provider == "ollama":
            try:
                from langchain_ollama import OllamaEmbeddings

                return OllamaEmbeddings(
                    base_url=self._settings.ollama_base_url,
                    model=self._settings.ollama_embed_model
                    or self._settings.ollama_model,
                )
            except Exception:
                if not self._settings.rag_fail_open:
                    raise
                return None

        for import_path in (
            "langchain_huggingface",
            "langchain_community.embeddings",
        ):
            try:
                module = __import__(import_path, fromlist=["HuggingFaceEmbeddings"])
                embedding_class = getattr(module, "HuggingFaceEmbeddings")
                return embedding_class(model_name=self._settings.rag_embedding_model)
            except Exception:
                continue

        if not self._settings.rag_fail_open:
            raise RuntimeError("No embedding runtime is available for retrieval.")
        return None

    def _load_schema_document(self) -> dict[str, Any]:
        if self._schema_document is None:
            with self._schema_json_path.open("r", encoding="utf-8") as handle:
                self._schema_document = json.load(handle)
        assert self._schema_document is not None
        return self._schema_document

    def _fallback_schema_chunks(
        self, resource_types: list[str]
    ) -> list[RetrievedChunk]:
        document = self._load_schema_document()
        definitions = document.get("definitions", {})
        chunks: list[RetrievedChunk] = []
        for resource_type in resource_types:
            if resource_type not in definitions:
                continue
            definition = json.dumps(
                definitions[resource_type], ensure_ascii=False, indent=2
            )
            chunks.append(
                RetrievedChunk(
                    source=f"schema_json:{resource_type}",
                    content=definition,
                    score=None,
                    metadata={"resource_type": resource_type, "fallback": True},
                )
            )
        return chunks[: self._settings.rag_top_k]

    def _format_chunks(
        self, chunks: list[RetrievedChunk], fallback_message: str
    ) -> str:
        if not chunks:
            return fallback_message

        sections = []
        for index, chunk in enumerate(chunks, start=1):
            sections.append(
                f"[Retrieved #{index}] source={chunk.source}\n{chunk.content}"
            )
        return "\n\n".join(sections)
