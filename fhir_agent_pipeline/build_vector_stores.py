from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.documents import Document

if TYPE_CHECKING:
    from .config import Settings


FHIR_SCHEMA_DEFAULT_EMBEDDING_MODEL = "qwen3-embedding"
MEDICAL_KNOWLEDGE_DEFAULT_EMBEDDING_MODEL = "qwen3-embedding"
STORE_METADATA_FILE = ".builder_meta.json"


def _resolve_text_splitter_class():
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        return RecursiveCharacterTextSplitter
    except Exception:
        from langchain.text_splitter import RecursiveCharacterTextSplitter

        return RecursiveCharacterTextSplitter


def _effective_embedding_model_name(
    provider: str,
    embedding_model_name: str,
    ollama_model_name: str | None,
) -> str:
    if provider == "ollama":
        return ollama_model_name or embedding_model_name
    return embedding_model_name


def _resolve_embedding_runtime(
    provider: str,
    embedding_model_name: str,
    ollama_base_url: str | None = None,
    ollama_model_name: str | None = None,
):
    normalized_provider = provider.strip().lower() if provider else "huggingface"
    if normalized_provider == "ollama":
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(
            base_url=ollama_base_url or "http://localhost:11434",
            model=_effective_embedding_model_name(
                normalized_provider,
                embedding_model_name,
                ollama_model_name,
            ),
        )

    return _resolve_huggingface_embeddings(embedding_model_name)


def _store_metadata_path(persist_directory: str) -> Path:
    return Path(persist_directory) / STORE_METADATA_FILE


def _read_store_metadata(persist_directory: str) -> dict[str, Any] | None:
    metadata_path = _store_metadata_path(persist_directory)
    if not metadata_path.exists():
        return None

    with metadata_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_store_metadata(persist_directory: str, metadata: dict[str, Any]) -> None:
    metadata_path = _store_metadata_path(persist_directory)
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)


def _expected_store_metadata(
    store_kind: str,
    provider: str,
    embedding_model_name: str,
    ollama_model_name: str | None,
) -> dict[str, Any]:
    normalized_provider = provider.strip().lower() if provider else "huggingface"
    return {
        "store_kind": store_kind,
        "embedding_provider": normalized_provider,
        "embedding_model": _effective_embedding_model_name(
            normalized_provider,
            embedding_model_name,
            ollama_model_name,
        ),
    }


def _directory_has_contents(persist_directory: str) -> bool:
    path = Path(persist_directory)
    return path.exists() and any(path.iterdir())


def _should_rebuild_store(
    persist_directory: str,
    expected_metadata: dict[str, Any],
) -> tuple[bool, str | None]:
    if not _directory_has_contents(persist_directory):
        return True, None

    existing_metadata = _read_store_metadata(persist_directory)
    if existing_metadata is None:
        return True, "Missing builder metadata for existing vector store."
    if existing_metadata != expected_metadata:
        return True, "Vector store embedding configuration changed."
    return False, None


def _prepare_persist_directory(
    persist_directory: str,
    force_rebuild: bool,
    expected_metadata: dict[str, Any],
) -> bool:
    should_rebuild, rebuild_reason = _should_rebuild_store(
        persist_directory,
        expected_metadata,
    )
    needs_rebuild = force_rebuild or should_rebuild
    path = Path(persist_directory)

    if needs_rebuild and path.exists():
        if rebuild_reason:
            print(f"[!] Rebuilding vector store at {persist_directory}: {rebuild_reason}")
        shutil.rmtree(path)

    path.mkdir(parents=True, exist_ok=True)
    return not needs_rebuild


def _schema_builder_config_from_env() -> dict[str, str | None]:
    return {
        "persist_directory": os.getenv("FHIR_SCHEMA_DB_DIR", "./fhir_schema_vectordb").strip()
        or "./fhir_schema_vectordb",
        "schema_file_path": os.getenv("FHIR_SCHEMA_JSON_PATH", "./db/fhir.schema.json").strip()
        or "./db/fhir.schema.json",
        "embedding_provider": os.getenv("RAG_EMBEDDING_PROVIDER", "ollama").strip().lower()
        or "huggingface",
        "embedding_model_name": os.getenv("RAG_EMBEDDING_MODEL", FHIR_SCHEMA_DEFAULT_EMBEDDING_MODEL).strip()
        or FHIR_SCHEMA_DEFAULT_EMBEDDING_MODEL,
        "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
        or "http://localhost:11434",
        "ollama_model_name": os.getenv("OLLAMA_EMBED_MODEL", "").strip()
        or None,
    }


def _resolve_huggingface_embeddings(model_name: str):
    try:
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name=model_name)
    except Exception:
        from langchain_community.embeddings import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name=model_name)


def _normalize_documents(documents: list[Document], chunk_size: int, chunk_overlap: int) -> list[Document]:
    splitter_class = _resolve_text_splitter_class()
    splitter = splitter_class(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_documents(documents)


def _parse_fhir_schema_documents(json_data: dict[str, Any]) -> list[Document]:
    documents: list[Document] = []
    definitions = json_data.get("definitions", {})
    for definition_name, definition_data in definitions.items():
        if not isinstance(definition_data, dict):
            continue

        description = str(
            definition_data.get("description", "No description available.")
        )
        overview_text = (
            f"Resource/DataType: {definition_name}\n"
            f"Description: {description}"
        )
        documents.append(
            Document(
                page_content=overview_text,
                metadata={"category": "overview", "definition": definition_name},
            )
        )

        properties = definition_data.get("properties", {})
        if not isinstance(properties, dict):
            continue
        for property_name, property_data in properties.items():
            if not isinstance(property_data, dict):
                continue

            property_description = str(property_data.get("description", ""))
            type_info = str(property_data.get("type", ""))

            if "$ref" in property_data:
                type_info += (
                    f" (Ref: {str(property_data['$ref']).split('/')[-1]})"
                )
            elif isinstance(property_data.get("items"), dict) and "$ref" in property_data["items"]:
                type_info += (
                    f" Array of {str(property_data['items']['$ref']).split('/')[-1]}"
                )

            enum_info = ""
            enum_values = property_data.get("enum")
            if isinstance(enum_values, list) and enum_values:
                enum_info = "\nEnum: " + ", ".join(str(value) for value in enum_values)

            pattern_info = ""
            pattern = property_data.get("pattern")
            if pattern is not None:
                pattern_info = f"\nPattern: {pattern}"

            property_text = (
                f"Resource: {definition_name}\n"
                f"Property: {property_name}\n"
                f"Type: {type_info}\n"
                f"Description: {property_description}{enum_info}{pattern_info}"
            )
            documents.append(
                Document(
                    page_content=property_text,
                    metadata={
                        "category": "property",
                        "definition": definition_name,
                        "property": property_name,
                    },
                )
            )

    return documents


def _load_schema_documents(schema_path: Path) -> list[Document]:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)

    return _parse_fhir_schema_documents(schema)


def _policy_documents() -> list[Document]:
    policies = [
        "Policy [Hallucination Prevention]: MIMIC-IV data does not contain real names, addresses, or telecom details. NEVER invent this data (e.g., do not use 'John Smith' or '123 Main St'). Omit these fields or use the FHIR 'data-absent-reason' extension.",
        "Policy [Identifiers]: For the FHIR resource 'id', strictly use the MIMIC 'subject_id' or 'hadm_id' exactly as provided. Do not generate random IDs like 'patient-123'.",
        "Policy [Date Shift]: Dates in MIMIC are shifted into the future for de-identification (e.g., 2180-01-01). Keep the provided dates exactly as they are in 'YYYY-MM-DD' format. Do not calculate or adjust to the current year.",
        "Policy [Local Coding]: Do NOT attempt to map MIMIC 'itemid' to external standards like LOINC or SNOMED-CT. Preserve the local code. Set 'system' to 'http://mimic.mit.edu/fhir/CodeSystem/local-code', 'code' to the 'itemid', and 'display' to the 'label'.",
        "Value Set Mapping [Gender]: Map MIMIC gender 'M' to FHIR 'male' and 'F' to FHIR 'female'.",
        "Value Set Mapping [Encounter Class]: Map MIMIC admission_type 'EMERGENCY' to FHIR 'EMER', 'ELECTIVE' to 'AMB', 'URGENT' to 'URG', and 'OBSERVATION ADMIT' to 'OBSENC'.",
    ]
    return [
        Document(page_content=policy, metadata={"category": "policy"})
        for policy in policies
    ]


def _fetch_dictionary_rows(settings: "Settings", sql: str) -> list[dict[str, Any]]:
    import pandas as pd
    from sqlalchemy import text

    try:
        from .db import _normalize_scalar, build_engine
    except ImportError:
        from db import _normalize_scalar, build_engine

    engine = build_engine(settings)
    df = pd.read_sql_query(text(sql), con=engine)
    return [
        {str(key): _normalize_scalar(value) for key, value in record.items()}
        for record in df.to_dict(orient="records")
    ]


def _dictionary_documents(settings: "Settings") -> list[Document]:
    documents: list[Document] = []
    schema = settings.mimic_schema

    dictionary_queries = [
        (
            f"SELECT itemid, label, fluid, category FROM {schema}.{settings.mimic_d_labitems_table}",
            "d_labitems",
        ),
        (
            f"SELECT itemid, label, category, param_type FROM {schema}.{settings.mimic_d_items_table}",
            "d_items",
        ),
        (
            f"SELECT icd_code, icd_version, long_title FROM {schema}.{settings.mimic_d_icd_diagnoses_table}",
            "d_icd_diagnoses",
        ),
    ]

    failures: list[str] = []

    for query, source in dictionary_queries:
        try:
            rows = _fetch_dictionary_rows(settings, query)
        except Exception as exc:
            failures.append(f"{source}: {exc}")
            if not settings.sql_fail_open:
                raise RuntimeError(
                    f"Failed to build medical knowledge dictionary from {source}."
                ) from exc
            continue

        for row in rows:
            if source == "d_labitems":
                content = (
                    f"MIMIC Local Dictionary [Lab Test]: ItemID '{row.get('itemid')}' corresponds to the label '{row.get('label')}'. "
                    f"Category: '{row.get('category', 'N/A')}', Fluid: '{row.get('fluid', 'N/A')}'."
                )
                code = row.get("itemid")
            elif source == "d_items":
                content = (
                    f"MIMIC Local Dictionary [Chart Item]: ItemID '{row.get('itemid')}' corresponds to the label '{row.get('label')}'. "
                    f"Category: '{row.get('category', 'N/A')}', Type: '{row.get('param_type', 'N/A')}'."
                )
                code = row.get("itemid")
            else:
                icd_code = str(row.get("icd_code", "")).strip()
                content = (
                    f"MIMIC Local Dictionary [Diagnosis]: ICD-{row.get('icd_version')} code '{icd_code}' "
                    f"represents the diagnosis '{row.get('long_title')}'."
                )
                code = icd_code

            documents.append(
                Document(
                    page_content=content,
                    metadata={
                        "category": "dictionary",
                        "source": source,
                        "code": code,
                    },
                )
            )

    if failures:
        print(
            "[!] Warning: Some medical dictionary sources could not be loaded: "
            + "; ".join(failures)
        )

    return documents


class FHIRSchemaKnowledgeBuilder:
    """fhir.schema.json을 파싱하여 구조적 지식 벡터 DB를 구축합니다."""

    def __init__(
        self,
        persist_directory: str = "./fhir_schema_vectordb",
        schema_file_path: str = "./db/fhir.schema.json",
        embedding_provider: str = "ollama",
        embedding_model_name: str = FHIR_SCHEMA_DEFAULT_EMBEDDING_MODEL,
        ollama_base_url: str = "http://192.168.0.7:11434",
        ollama_model_name: str | None = None,
    ) -> None:
        self.persist_directory = persist_directory
        self.schema_file_path = schema_file_path
        self.embedding_provider = embedding_provider
        self.embedding_model_name = embedding_model_name
        self.ollama_base_url = ollama_base_url
        self.ollama_model_name = ollama_model_name
        self.embedding_model = _resolve_embedding_runtime(
            provider=embedding_provider,
            embedding_model_name=embedding_model_name,
            ollama_base_url=ollama_base_url,
            ollama_model_name=ollama_model_name,
        )

    def _parse_fhir_schema(self, json_data: dict[str, Any]) -> list[Document]:
        return _parse_fhir_schema_documents(json_data)

    def get_or_build_db(self, force_rebuild: bool = False):
        expected_metadata = _expected_store_metadata(
            store_kind="fhir_schema",
            provider=self.embedding_provider,
            embedding_model_name=self.embedding_model_name,
            ollama_model_name=self.ollama_model_name,
        )
        can_reuse = _prepare_persist_directory(
            self.persist_directory,
            force_rebuild=force_rebuild,
            expected_metadata=expected_metadata,
        )
        if can_reuse:
            from langchain_chroma import Chroma

            print(f"[*] 기존 FHIR Schema Vector DB 로딩 중: {self.persist_directory}")
            return Chroma(
                persist_directory=self.persist_directory,
                embedding_function=self.embedding_model,
            )

        print(f"[*] FHIR Schema Vector DB 구축 중 ({self.schema_file_path})...")
        with open(self.schema_file_path, "r", encoding="utf-8") as handle:
            schema_data = json.load(handle)

        all_documents = self._parse_fhir_schema(schema_data)
        split_documents = _normalize_documents(
            all_documents,
            chunk_size=800,
            chunk_overlap=100,
        )

        from langchain_chroma import Chroma

        vector_db = Chroma.from_documents(
            documents=split_documents,
            embedding=self.embedding_model,
            persist_directory=self.persist_directory,
        )
        _write_store_metadata(self.persist_directory, expected_metadata)
        return vector_db


class MedicalKnowledgeRetriever:
    """MIMIC 정책 및 로컬 코드 기반 medical knowledge 벡터 DB를 구축/로드합니다."""

    def __init__(
        self,
        settings: "Settings",
        persist_directory: str = "./mimic_docs_vectordb",
        embedding_provider: str | None = None,
        embedding_model_name: str = MEDICAL_KNOWLEDGE_DEFAULT_EMBEDDING_MODEL,
        ollama_base_url: str | None = None,
        ollama_model_name: str | None = None,
    ) -> None:
        self.settings = settings
        self.persist_directory = persist_directory
        self.embedding_provider = embedding_provider or settings.rag_embedding_provider
        self.embedding_model_name = embedding_model_name
        self.ollama_base_url = ollama_base_url or settings.ollama_base_url
        self.ollama_model_name = ollama_model_name or settings.ollama_embed_model
        self.embedding_model = _resolve_embedding_runtime(
            provider=self.embedding_provider,
            embedding_model_name=embedding_model_name,
            ollama_base_url=self.ollama_base_url,
            ollama_model_name=self.ollama_model_name,
        )
        self.vector_db = None

    def _get_policy_documents(self) -> list[Document]:
        return _policy_documents()

    def _get_dictionary_documents(self) -> list[Document]:
        return _dictionary_documents(self.settings)

    def get_or_build_db(self, force_rebuild: bool = False):
        expected_metadata = _expected_store_metadata(
            store_kind="medical_knowledge",
            provider=self.embedding_provider,
            embedding_model_name=self.embedding_model_name,
            ollama_model_name=self.ollama_model_name,
        )
        can_reuse = _prepare_persist_directory(
            self.persist_directory,
            force_rebuild=force_rebuild,
            expected_metadata=expected_metadata,
        )
        if can_reuse:
            from langchain_chroma import Chroma

            print(f"[*] 기존 Medical Knowledge DB 로딩 중: {self.persist_directory}")
            self.vector_db = Chroma(
                persist_directory=self.persist_directory,
                embedding_function=self.embedding_model,
            )
            return self.vector_db

        print("[*] Medical Knowledge DB 구축 중 (MIMIC Policies & Local Codes)...")
        all_documents = self._get_policy_documents()
        dictionary_documents = self._get_dictionary_documents()
        if dictionary_documents:
            all_documents.extend(dictionary_documents)
        else:
            print("[!] Warning: 사전 데이터 없이 정책 가이드만 구축합니다.")

        split_documents = _normalize_documents(
            all_documents,
            chunk_size=600,
            chunk_overlap=50,
        )

        from langchain_chroma import Chroma

        self.vector_db = Chroma.from_documents(
            documents=split_documents,
            embedding=self.embedding_model,
            persist_directory=self.persist_directory,
        )
        _write_store_metadata(self.persist_directory, expected_metadata)
        print("[✅] Medical Knowledge DB 구축 완료")
        return self.vector_db

    def search_context(self, query: str, k: int = 10) -> str:
        if not self.vector_db:
            return "Knowledge base not initialized."

        scored_documents = self.vector_db.similarity_search_with_score(query, k=k)
        valid_contexts = [
            document.page_content
            for document, score in scored_documents
            if score < 1.8
        ]
        return (
            "\n\n".join(valid_contexts)
            if valid_contexts
            else "No specific clinical or policy evidence found."
        )


def build_fhir_schema_vector_store(
    settings: "Settings" | None = None,
    force_rebuild: bool = False,
) -> str:
    if settings is None:
        builder = FHIRSchemaKnowledgeBuilder(**_schema_builder_config_from_env())
        builder.get_or_build_db(force_rebuild=force_rebuild)
        return str(Path(builder.persist_directory))
    FHIRSchemaKnowledgeBuilder(
        persist_directory=settings.fhir_schema_db_dir,
        schema_file_path=settings.fhir_schema_json_path,
        embedding_provider=settings.rag_embedding_provider,
        embedding_model_name=settings.rag_embedding_model,
        ollama_base_url=settings.ollama_base_url,
        ollama_model_name=settings.ollama_embed_model,
    ).get_or_build_db(force_rebuild=force_rebuild)
    return str(Path(settings.fhir_schema_db_dir))


def build_medical_knowledge_vector_store(
    settings: "Settings" | None = None,
    force_rebuild: bool = False,
) -> str:
    runtime_settings = settings or _load_settings()
    MedicalKnowledgeRetriever(
        settings=runtime_settings,
        persist_directory=runtime_settings.mimic_knowledge_db_dir,
        embedding_provider=runtime_settings.rag_embedding_provider,
        embedding_model_name=runtime_settings.rag_embedding_model,
        ollama_base_url=runtime_settings.ollama_base_url,
        ollama_model_name=runtime_settings.ollama_embed_model,
    ).get_or_build_db(force_rebuild=force_rebuild)
    return str(Path(runtime_settings.mimic_knowledge_db_dir))


def build_all_vector_stores(
    settings: "Settings" | None = None,
    force_rebuild: bool = False,
) -> dict[str, str]:
    runtime_settings = settings or _load_settings()
    return {
        "fhir_schema": build_fhir_schema_vector_store(
            runtime_settings,
            force_rebuild=force_rebuild,
        ),
        "medical_knowledge": build_medical_knowledge_vector_store(
            runtime_settings,
            force_rebuild=force_rebuild,
        ),
    }


def _load_settings() -> "Settings":
    try:
        from .config import load_settings
    except ImportError:
        from config import load_settings

    return load_settings()


__all__ = [
    "FHIRSchemaKnowledgeBuilder",
    "MedicalKnowledgeRetriever",
    "build_all_vector_stores",
    "build_fhir_schema_vector_store",
    "build_medical_knowledge_vector_store",
    "_parse_fhir_schema_documents",
    "_policy_documents",
]


if __name__ == "__main__":
    built_paths = build_all_vector_stores()
    print(
        "Built vector stores:\n"
        f"- FHIR schema: {built_paths['fhir_schema']}\n"
        f"- Medical knowledge: {built_paths['medical_knowledge']}"
    )
