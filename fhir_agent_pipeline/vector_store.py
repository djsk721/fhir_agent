from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Iterable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .config import Settings
from .db import build_engine, quote_identifier
from .fhir_schema import get_fhir_schema
from .json_utils import dump_json
from .mapping_config import MimicMappingConfig
from .ollama import OllamaClient


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


@dataclass(frozen=True)
class KnowledgeDocument:
    content: str
    metadata: dict[str, Any]


class PgVectorKnowledgeStore:
    """두 지식 테이블을 동일한 스키마로 관리하는 작은 pgvector 저장소입니다."""

    required_columns = {"content", "metadata", "embedding", "content_hash"}

    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine or build_engine(settings)
        self.embeddings = OllamaClient(
            settings.ollama_base_url,
            settings.ollama_model,
            settings.ollama_embedding_model,
            settings.ollama_timeout_seconds,
            max_retries=settings.ollama_max_retries,
            retry_delay_seconds=settings.ollama_retry_delay_seconds,
        )

    def _qualified(self, table_name: str) -> str:
        return (
            f"{quote_identifier(self.settings.vector_schema)}."
            f"{quote_identifier(table_name)}"
        )

    def ensure_table(self, table_name: str) -> None:
        table = self._qualified(table_name)
        dimension = self.settings.embedding_dimension
        with self.engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(
                text(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id BIGSERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding vector({dimension}) NOT NULL,
                        content_hash VARCHAR(64) NOT NULL UNIQUE,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
            )
        report = self.inspect_table(table_name)
        missing = self.required_columns - set(report.get("columns", {}))
        if missing:
            raise RuntimeError(
                f"{self.settings.vector_schema}.{table_name}은 기존 테이블이지만 호환되지 않습니다. "
                f"누락 컬럼: {sorted(missing)}"
            )

    def inspect_table(self, table_name: str) -> dict[str, Any]:
        inspector = inspect(self.engine)
        exists = inspector.has_table(table_name, schema=self.settings.vector_schema)
        report: dict[str, Any] = {
            "table": f"{self.settings.vector_schema}.{table_name}",
            "exists": exists,
            "columns": {},
            "rows": 0,
            "compatible": False,
        }
        if not exists:
            return report
        with self.engine.connect() as connection:
            column_rows = connection.execute(text("""
                SELECT attribute.attname, format_type(attribute.atttypid, attribute.atttypmod)
                FROM pg_attribute AS attribute
                JOIN pg_class AS relation ON relation.oid = attribute.attrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = :schema AND relation.relname = :table
                  AND attribute.attnum > 0 AND NOT attribute.attisdropped
                ORDER BY attribute.attnum
            """), {"schema": self.settings.vector_schema, "table": table_name})
            report["columns"] = {name: data_type for name, data_type in column_rows}
            report["rows"] = int(
                connection.execute(
                    text(f"SELECT count(*) FROM {self._qualified(table_name)}")
                ).scalar_one()
            )
            report["indexes"] = [
                row[0]
                for row in connection.execute(text("""
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = :schema AND tablename = :table
                    ORDER BY indexname
                """), {"schema": self.settings.vector_schema, "table": table_name})
            ]
        report["compatible"] = self.required_columns.issubset(report["columns"])
        return report

    def ensure_vector_index(self, table_name: str) -> None:
        """cosine 검색용 HNSW 인덱스를 안전하게 추가합니다."""
        index_name = quote_identifier(f"{table_name}_embedding_hnsw")
        with self.engine.begin() as connection:
            connection.execute(text(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {self._qualified(table_name)} "
                "USING hnsw (embedding vector_cosine_ops)"
            ))

    def inspect_all(self) -> list[dict[str, Any]]:
        return [
            self.inspect_table(self.settings.fhir_vector_table),
            self.inspect_table(self.settings.sql_vector_table),
        ]

    def upsert(
        self,
        table_name: str,
        documents: Iterable[KnowledgeDocument],
        batch_size: int | None = None,
    ) -> dict[str, int]:
        """content hash가 없는 신규·변경 문서만 임베딩합니다."""
        self.ensure_table(table_name)
        docs = list(documents)
        effective_batch_size = batch_size or self.settings.vector_batch_size
        table = self._qualified(table_name)
        prepared = [
            (
                doc,
                hashlib.sha256(doc.content.encode("utf-8")).hexdigest(),
            )
            for doc in docs
        ]
        hashes = [content_hash for _, content_hash in prepared]
        with self.engine.connect() as connection:
            existing = set(
                connection.execute(
                    text(
                        f"SELECT content_hash FROM {table} "
                        "WHERE content_hash = ANY(:hashes)"
                    ),
                    {"hashes": hashes},
                ).scalars()
            ) if hashes else set()

        existing_metadata = [
            {
                "metadata": dump_json(doc.metadata),
                "content_hash": content_hash,
            }
            for doc, content_hash in prepared
            if content_hash in existing
        ]
        if existing_metadata:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        f"UPDATE {table} SET metadata=CAST(:metadata AS jsonb), "
                        "updated_at=now() WHERE content_hash=:content_hash"
                    ),
                    existing_metadata,
                )

        pending = [item for item in prepared if item[1] not in existing]
        embedded = 0
        for start in range(0, len(pending), effective_batch_size):
            batch = pending[start : start + effective_batch_size]
            batch_docs = [doc for doc, _ in batch]
            vectors = self.embeddings.embed_documents([doc.content for doc in batch_docs])
            if vectors and len(vectors[0]) != self.settings.embedding_dimension:
                raise ValueError(
                    f"임베딩 차원은 {len(vectors[0])}이지만 EMBEDDING_DIMENSION은 "
                    f"{self.settings.embedding_dimension}입니다. .env 값을 맞추세요."
                )
            if len(vectors) != len(batch_docs):
                raise RuntimeError("Ollama 임베딩 개수가 입력 문서 개수와 다릅니다.")
            rows: list[dict[str, str]] = []
            for (doc, content_hash), vector in zip(batch, vectors):
                rows.append({
                    "content": doc.content,
                    "metadata": dump_json(doc.metadata),
                    "embedding": _vector_literal(vector),
                    "content_hash": content_hash,
                })
            with self.engine.begin() as connection:
                connection.execute(text(f"""
                    INSERT INTO {table} (content, metadata, embedding, content_hash)
                    VALUES (:content, CAST(:metadata AS jsonb), CAST(:embedding AS vector), :content_hash)
                    ON CONFLICT (content_hash) DO UPDATE SET
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                """), rows)
            embedded += len(rows)
        self.ensure_vector_index(table_name)
        return {
            "total": len(docs),
            "embedded": embedded,
            "reused": len(docs) - embedded,
        }

    def prune_generated_documents(
        self,
        table_name: str,
        documents: Iterable[KnowledgeDocument],
    ) -> int:
        """현재 생성 대상에서 빠진 이전 스키마/매핑 문서를 제거합니다."""
        docs = list(documents)
        sources = sorted({
            str(doc.metadata["source"])
            for doc in docs
            if doc.metadata.get("source") is not None
        })
        hashes = [hashlib.sha256(doc.content.encode("utf-8")).hexdigest() for doc in docs]
        if not sources or not hashes:
            return 0
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"DELETE FROM {self._qualified(table_name)} "
                    "WHERE metadata->>'source' = ANY(:sources) "
                    "AND NOT (content_hash = ANY(:hashes))"
                ),
                {"sources": sources, "hashes": hashes},
            )
        return int(result.rowcount or 0)

    def search(self, table_name: str, query: str, k: int | None = None) -> list[dict[str, Any]]:
        report = self.inspect_table(table_name)
        if not report["exists"] or not report["compatible"]:
            return []
        vector = self.embeddings.embed_query(query)
        limit = k or self.settings.vector_search_k
        with self.engine.connect() as connection:
            rows = connection.execute(text(f"""
                SELECT content, metadata, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                FROM {self._qualified(table_name)}
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
            """), {
                "embedding": _vector_literal(vector),
                "limit": limit,
            }).mappings()
            return [dict(row) for row in rows]


def fhir_schema_documents(settings: Settings) -> list[KnowledgeDocument]:
    definitions = get_fhir_schema(settings.fhir_schema_path).definitions
    documents: list[KnowledgeDocument] = []
    for name, definition in definitions.items():
        properties = definition.get("properties", {})
        required = set(definition.get("required", []))
        overview = (
            f"FHIR R4 definition: {name}\n"
            f"Description: {definition.get('description', '')}\n"
            f"Required: {', '.join(sorted(required)) or 'none'}"
        )
        documents.append(KnowledgeDocument(overview, {
            "source": str(settings.fhir_schema_path),
            "definition": name,
            "kind": "overview",
        }))
        for prop_name, prop in properties.items():
            ref = prop.get("$ref") or prop.get("items", {}).get("$ref", "")
            details = {
                "type": prop.get("type", ""),
                "reference": ref.rsplit("/", 1)[-1] if ref else "",
                "enum": prop.get("enum", []),
                "pattern": prop.get("pattern", ""),
            }
            content = (
                f"FHIR R4 {name}.{prop_name}\n"
                f"Required: {prop_name in required}\n"
                f"Rules: {dump_json(details)}\n"
                f"Description: {prop.get('description', '')}"
            )
            documents.append(KnowledgeDocument(content, {
                "source": str(settings.fhir_schema_path),
                "definition": name,
                "property": prop_name,
                "kind": "property",
            }))
    return documents


def database_schema_documents(settings: Settings, engine: Engine | None = None) -> list[KnowledgeDocument]:
    engine = engine or build_engine(settings)
    inspector = inspect(engine)
    documents: list[KnowledgeDocument] = []
    for schema in settings.allowed_schemas:
        available = set(inspector.get_table_names(schema=schema))
        for table_name in settings.allowed_tables:
            if table_name not in available:
                continue
            columns = inspector.get_columns(table_name, schema=schema)
            pk = set((inspector.get_pk_constraint(table_name, schema=schema) or {}).get("constrained_columns") or [])
            foreign_keys = inspector.get_foreign_keys(table_name, schema=schema)
            column_text = ", ".join(
                f"{column['name']} {column['type']}"
                f"{' PRIMARY KEY' if column['name'] in pk else ''}"
                for column in columns
            )
            fk_text = "; ".join(
                f"{','.join(foreign_key.get('constrained_columns') or [])} -> "
                f"{foreign_key.get('referred_schema') or schema}."
                f"{foreign_key.get('referred_table')}"
                f"({','.join(foreign_key.get('referred_columns') or [])})"
                for foreign_key in foreign_keys
            )
            content = (
                f"PostgreSQL table {schema}.{table_name}\n"
                f"Columns: {column_text}\n"
                f"Foreign keys: {fk_text or 'none'}"
            )
            documents.append(KnowledgeDocument(content, {
                "source": "postgresql",
                "schema": schema,
                "table": table_name,
                "kind": "table_schema",
            }))
    return documents


def mapping_profile_documents(settings: Settings) -> list[KnowledgeDocument]:
    mapping = MimicMappingConfig.load(settings.mimic_mapping_config_path)
    source_columns = {
        "patient": mapping.patient_columns.values,
        "admission": mapping.admission_columns.values,
        "lab_event": mapping.lab_columns.values,
        "diagnosis": mapping.diagnosis_columns.values,
        "procedure": mapping.procedure_columns.values,
    }
    encounter_rules = {
        "class_system": mapping.encounter_class_system,
        "default_class": mapping.encounter_default_class,
        "class_by_admission_type": mapping.encounter_class_by_type,
        "status_when_ended": mapping.encounter_status_when_ended,
        "status_when_open": mapping.encounter_status_when_open,
    }
    identifiers = {
        "patient": mapping.patient_identifier_system,
        "encounter": mapping.encounter_identifier_system,
        "lab_item": mapping.lab_item_system,
        "lab_event": mapping.lab_event_identifier_system,
        "condition": mapping.condition_identifier_system,
        "procedure": mapping.procedure_identifier_system,
        "resource_id_rules": mapping.resource_id_rules,
        "patient_gender_map": mapping.patient_gender_map,
        "default_timezone": settings.fhir_default_timezone,
    }
    terminology = {
        "diagnosis_icd_systems": mapping.diagnosis_icd_systems,
        "procedure_icd_systems": mapping.procedure_icd_systems,
        "diagnosis_decimal_positions": mapping.diagnosis_decimal_positions,
        "procedure_decimal_positions": mapping.procedure_decimal_positions,
        "diagnosis_prefix_decimal_positions": mapping.diagnosis_prefix_decimal_positions,
        "procedure_prefix_decimal_positions": mapping.procedure_prefix_decimal_positions,
        "fhir_defaults": mapping.fhir_defaults,
    }
    common = {"source": str(mapping.source_path), "kind": "mimic_mapping_profile"}
    return [
        KnowledgeDocument(
            "MIMIC logical-to-physical source columns:\n" + dump_json(source_columns),
            {**common, "section": "source_columns"},
        ),
        KnowledgeDocument(
            "MIMIC admission_type to FHIR Encounter mapping:\n"
            + dump_json(encounter_rules),
            {**common, "section": "encounter"},
        ),
        KnowledgeDocument(
            "MIMIC FHIR identifier and timezone policy:\n" + dump_json(identifiers),
            {**common, "section": "identifiers_and_timezone"},
        ),
        KnowledgeDocument(
            "MIMIC diagnosis and procedure ICD terminology policy:\n" + dump_json(terminology),
            {**common, "section": "icd_terminology"},
        ),
    ]


def build_knowledge_bases(settings: Settings) -> dict[str, Any]:
    store = PgVectorKnowledgeStore(settings)
    mapping_docs = mapping_profile_documents(settings)
    fhir_docs = [*fhir_schema_documents(settings), *mapping_docs]
    sql_docs = database_schema_documents(settings, store.engine)
    sql_profile_sections = {"source_columns", "icd_terminology"}
    sql_docs.extend(
        doc
        for doc in mapping_docs
        if doc.metadata.get("section") in sql_profile_sections
    )
    fhir_stats = store.upsert(settings.fhir_vector_table, fhir_docs)
    sql_stats = store.upsert(settings.sql_vector_table, sql_docs)
    pruned = {
        "fhir": store.prune_generated_documents(settings.fhir_vector_table, fhir_docs),
        "sql": store.prune_generated_documents(settings.sql_vector_table, sql_docs),
    }
    return {
        "fhir_documents": fhir_stats,
        "sql_documents": sql_stats,
        "pruned_documents": pruned,
        "tables": store.inspect_all(),
    }
