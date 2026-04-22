from __future__ import annotations

import json
import shutil
from pathlib import Path

from langchain_core.documents import Document

from .config import Settings, load_settings


def _resolve_embeddings(settings: Settings):
    if settings.rag_embedding_provider == "ollama":
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(
            base_url=settings.ollama_base_url,
            model=settings.ollama_embed_model or settings.ollama_model,
        )

    try:
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name=settings.rag_embedding_model)
    except Exception:
        from langchain_community.embeddings import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name=settings.rag_embedding_model)


def _load_schema_documents(schema_path: Path) -> list[Document]:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)

    definitions = schema.get("definitions", {})
    documents: list[Document] = []
    for resource_type, definition in definitions.items():
        documents.append(
            Document(
                page_content=json.dumps(definition, ensure_ascii=False, indent=2),
                metadata={
                    "resource_type": resource_type,
                    "source": f"fhir.schema.json#{resource_type}",
                },
            )
        )
    return documents


def build_fhir_schema_vector_store(settings: Settings | None = None) -> str:
    runtime_settings = settings or load_settings()
    schema_path = Path(runtime_settings.fhir_schema_json_path)
    persist_directory = Path(runtime_settings.fhir_schema_db_dir)
    documents = _load_schema_documents(schema_path)

    if persist_directory.exists():
        shutil.rmtree(persist_directory)
    persist_directory.mkdir(parents=True, exist_ok=True)

    from langchain_chroma import Chroma

    Chroma.from_documents(
        documents=documents,
        embedding=_resolve_embeddings(runtime_settings),
        persist_directory=str(persist_directory),
    )
    return str(persist_directory)


if __name__ == "__main__":
    built_path = build_fhir_schema_vector_store()
    print(f"Built FHIR schema vector store at: {built_path}")
