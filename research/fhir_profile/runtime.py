"""Local Ollama 계측과 FHIR 전용 벡터 검색. 원천 SQL을 생성하지 않습니다."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from urllib.request import urlopen
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from fhir_agent_pipeline.fhir_schema import get_fhir_schema
from fhir_agent_pipeline.ollama import OllamaClient

from .artifacts import ROOT, fingerprint, read_json
from .contracts import ProfileGenerationError


class ResearchOllamaClient(OllamaClient):
    def __init__(self, config: dict[str, Any]) -> None:
        model = config["model"]
        super().__init__(
            model["base_url"], model["name"], config["rag"].get("embedding_model", ""),
            timeout=int(model.get("timeout_seconds", 300)),
            context_size=int(model.get("context_size", 8192)),
            max_output_tokens=int(model.get("max_output_tokens", 4096)),
            max_retries=0,
        )
        self.options = {
            "temperature": float(model.get("temperature", 0)),
            "seed": int(model.get("seed", 42)),
            "num_ctx": self.context_size, "num_predict": self.max_output_tokens,
        }
        if "num_gpu" in model:
            self.options["num_gpu"] = model["num_gpu"]
        self.embedding_options = {"num_gpu": config["rag"]["num_gpu"]} if "num_gpu" in config["rag"] else {}
        self.calls: list[dict[str, Any]] = []
        self.record_payloads = bool(config.get("logging", {}).get("record_payloads", False))
        self.schema_path = ROOT / config["validation"]["schema_path"]
        self.thinking = model.get("think", False)

    def identity(self, model: str | None = None) -> dict[str, str]:
        name = model or self.chat_model
        with urlopen(f"{self.base_url}/api/tags", timeout=self.timeout) as response:
            models = json.load(response).get("models", [])
        matches = [item for item in models if item.get("name") in {name, name + ":latest"} or item.get("model") == name]
        if len(matches) != 1 or not matches[0].get("digest"):
            raise ValueError(f"Ollama에서 모델 digest를 확인할 수 없습니다: {name}")
        return {"name": str(matches[0]["name"]), "digest": str(matches[0]["digest"])}

    def chat_profile(self, system_prompt: str, user_prompt: str) -> str:
        from .profiles.output_schema import profile_output_schema, profile_generation_schema, unpack_target_rules
        from .profiles.schema import bind_profile_identity

        request = json.loads(user_prompt)
        target = request.get("target_resource") or request.get("current_profile", {}).get("target_resource")
        metadata = request.get("source_metadata")
        managed_identity = bool((metadata or {}).get("identity_contract"))
        schema = profile_output_schema(target, metadata, self.schema_path)
        generation_schema = profile_generation_schema(target, metadata, self.schema_path, wire=True)
        if target:
            request["decoder_output_contract"] = {
                "mapping_shape": {"id": "string", "target_rule": "operation:target_path"},
                "rules": [
                    "Output one target_rule string per mapping, as operation:target_path. Do not output separate operation or target_path keys in mappings.",
                    "The decoder permits only compatible FHIR paths and operations. Select the exact tagged pair using the supplied FHIR context.",
                    "An input draft or current_profile may use the separate canonical keys. Convert those keys into target_rule when returning a revision.",
                    *(
                        ["Omit the root identity from the response; the renderer applies the verified source identity."]
                        if managed_identity else []
                    ),
                ],
            }
            user_prompt = json.dumps(request, ensure_ascii=False)

        def normalize(payload):
            canonical = unpack_target_rules(payload)
            return bind_profile_identity(canonical, metadata) if managed_identity else canonical

        return self.chat(system_prompt, user_prompt, response_schema=schema, generation_schema=generation_schema,
                         response_adapter=normalize)

    def chat(self, system_prompt: str, user_prompt: str, *, response_schema: dict[str, Any] | None = None,
             generation_schema: dict[str, Any] | None = None,
             response_adapter: Callable[[Any], Any] | None = None) -> str:
        started = perf_counter()
        event: dict[str, Any] = {
            "kind": "chat", "model": self.chat_model,
            "prompt_sha256": fingerprint([system_prompt, user_prompt]),
            "options": dict(self.options),
            "think": self.thinking,
            "response_schema_sha256": fingerprint(response_schema) if response_schema else None,
            "generation_schema_sha256": hashlib.sha256(json.dumps(generation_schema, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() if generation_schema else None,
        }
        if self.record_payloads:
            event["request"] = {"system_prompt": system_prompt, "user_prompt": user_prompt}
        try:
            result = self._post("/api/chat", {
                "model": self.chat_model, "stream": False, "think": self.thinking, "format": generation_schema or response_schema or "json",
                "options": self.options,
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            })
            if self.record_payloads:
                event["response"] = result
            answer = result.get("message", {}).get("content")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Ollama chat에 실제 content 응답이 없습니다.")
            if result.get("done") is False:
                raise ValueError("Ollama non-stream 응답이 완료되지 않았습니다.")
            event.update({
                "success": True, "prompt_tokens": result.get("prompt_eval_count"),
                "output_tokens": result.get("eval_count"), "response_sha256": fingerprint(answer),
                "done_reason": result.get("done_reason"),
                "total_duration_ns": result.get("total_duration"),
                "load_duration_ns": result.get("load_duration"),
                "prompt_eval_duration_ns": result.get("prompt_eval_duration"),
                "eval_duration_ns": result.get("eval_duration"),
            })
            if result.get("done_reason") == "length":
                raise ValueError("모델 출력이 max_output_tokens에서 절단되었습니다.")
            if response_schema is not None:
                payload = json.loads(answer)
                if response_adapter is not None:
                    try:
                        payload = response_adapter(payload)
                    except (TypeError, ValueError) as exc:
                        raise ProfileGenerationError(f"Profile output contract violation (wire): {exc}", payload) from exc
                error = best_match(Draft202012Validator(response_schema).iter_errors(payload))
                if error is not None:
                    path = ".".join(str(part) for part in error.absolute_path)
                    raise ProfileGenerationError(f"Profile output contract violation ({path}): {error.message}", payload)
                if response_adapter is not None:
                    answer = json.dumps(payload, ensure_ascii=False)
            return answer
        except Exception as exc:
            event.update({"success": False, "error_type": type(exc).__name__})
            raise
        finally:
            event["elapsed_seconds"] = perf_counter() - started
            self.calls.append(event)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        started = perf_counter()
        event: dict[str, Any] = {
            "kind": "embedding", "model": self.embedding_model, "document_count": len(texts),
            "input_sha256": fingerprint(texts),
            "options": dict(self.embedding_options),
        }
        try:
            if self.embedding_options:
                response = self._post("/api/embed", {
                    "model": self.embedding_model, "input": texts, "options": self.embedding_options,
                })
                vectors = response.get("embeddings")
            else:
                vectors = super().embed_documents(texts)
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise ValueError("유효하지 않은 임베딩 응답입니다.")
            _validate_vectors(vectors)
            event["success"] = True
            event["dimension"] = len(vectors[0])
            return vectors
        except Exception as exc:
            event.update({"success": False, "error_type": type(exc).__name__})
            raise
        finally:
            event["elapsed_seconds"] = perf_counter() - started
            self.calls.append(event)


def schema_documents(schema_path: Path, *, max_document_chars: int = 2000) -> list[dict[str, str]]:
    if isinstance(max_document_chars, bool) or not isinstance(max_document_chars, int) or max_document_chars < 256:
        raise ValueError("max_document_chars는 256 이상의 정수여야 합니다.")
    definitions = get_fhir_schema(schema_path).definitions
    documents: list[dict[str, str]] = []

    def add(name: str, kind: str, path: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # Large individual element descriptions are split with an explicit path.
        # Overview plus per-element chunks avoids embedding a whole huge resource.
        header = f"FHIR R4 {name} {path} [{kind}]\n"
        available = max_document_chars - len(header) - 32
        if available < 1:
            raise ValueError("FHIR 정의 이름이 문서 크기 제한을 초과합니다.")
        chunks = [body[start:start + available] for start in range(0, len(body), available)]
        for number, chunk in enumerate(chunks, 1):
            documents.append({
                "definition": name, "kind": kind, "path": path,
                "content": f"{header}part {number}/{len(chunks)}\n{chunk}",
            })

    for name, definition in definitions.items():
        props = definition.get("properties", {})
        add(name, "overview", name, {
            "definition": name, "description": definition.get("description", "")[:400],
            "required": definition.get("required", []), "properties": list(props),
            "type": definition.get("type"), "pattern": definition.get("pattern"),
        })
        for property_name, property_schema in props.items():
            add(name, "property", f"{name}.{property_name}", {
                "definition": name, "path": f"{name}.{property_name}",
                "required": property_name in definition.get("required", []),
                "schema": property_schema,
            })
    return documents


def _validate_vectors(vectors: Any) -> int:
    if not isinstance(vectors, list) or not vectors:
        raise ValueError("Vector index의 임베딩 배열이 비어 있습니다.")
    dimension = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("임베딩 vector는 비어 있지 않은 숫자 배열이어야 합니다.")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension:
            raise ValueError("임베딩 vector의 차원이 일정하지 않습니다.")
        if any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in vector):
            raise ValueError("임베딩 vector에는 유한한 숫자만 허용합니다.")
        if not any(x != 0 for x in vector):
            raise ValueError("영벡터는 유효한 임베딩이 아닙니다.")
        if not math.isfinite(math.hypot(*vector)):
            raise ValueError("임베딩 vector의 norm이 유한하지 않습니다.")
    return int(dimension)


def build_fhir_index(config: dict[str, Any], client: ResearchOllamaClient) -> dict[str, Any]:
    path = ROOT / config["validation"]["schema_path"]
    documents = schema_documents(path, max_document_chars=int(config["rag"].get("max_document_chars", 2000)))
    # FHIR 정의만 저장하며 환자 데이터와 정답 매핑은 적재하지 않습니다.
    batch_size = int(config["rag"].get("embedding_batch_size", 16))
    if batch_size < 1:
        raise ValueError("embedding_batch_size는 1 이상이어야 합니다.")
    vectors = []
    for start in range(0, len(documents), batch_size):
        vectors.extend(client.embed_documents([item["content"] for item in documents[start:start + batch_size]]))
    dimension = _validate_vectors(vectors)
    payload = {
        "format_version": 1, "kind": "fhir_schema_only",
        "schema_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "embedding_identity": client.identity(client.embedding_model),
        "chunking": {"method": "overview-and-property-v1", "max_document_chars": int(config["rag"].get("max_document_chars", 2000))},
        "dimension": dimension, "documents": documents, "vectors": vectors,
    }
    return {**payload, "index_sha256": fingerprint(payload)}


class FhirRetriever:
    def __init__(self, index_path: str | Path, client: ResearchOllamaClient, *, top_k: int = 6) -> None:
        index = read_json(index_path)
        unsigned = {key: value for key, value in index.items() if key != "index_sha256"}
        if index.get("kind") != "fhir_schema_only" or fingerprint(unsigned) != index.get("index_sha256"):
            raise ValueError("FHIR 전용 Vector index의 hash/kind가 올바르지 않습니다.")
        self.index = index
        self.client = client
        self.top_k = top_k
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1 or top_k > 64:
            raise ValueError("top_k는 1~64 정수여야 합니다.")
        documents, vectors = index.get("documents"), index.get("vectors")
        if not isinstance(documents, list) or not isinstance(vectors, list) or len(documents) != len(vectors):
            raise ValueError("Vector index의 문서/임베딩이 유효하지 않습니다.")
        self.dimension = _validate_vectors(vectors)
        if index.get("dimension", self.dimension) != self.dimension:
            raise ValueError("Vector index의 dimension metadata가 실제 vector와 다릅니다.")
        if not index.get("schema_sha256") or not index.get("embedding_identity", {}).get("digest"):
            raise ValueError("Vector index의 스키마/모델 metadata가 없습니다.")
        if any(not isinstance(doc, dict) or not isinstance(doc.get("definition"), str)
               or not isinstance(doc.get("content"), str) or not doc["content"].strip() for doc in documents):
            raise ValueError("Vector index의 FHIR 문서 metadata가 올바르지 않습니다.")

    def context(self, resource_type: str) -> str:
        vector = self.client.embed_query(f"FHIR R4 {resource_type} required properties datatypes references choice fields")
        if _validate_vectors([vector]) != self.dimension:
            raise ValueError("검색 query와 index의 임베딩 차원이 다릅니다.")
        def score(other: list[float]) -> float:
            if len(vector) != len(other):
                raise ValueError("임베딩 차원이 다릅니다.")
            query_norm, other_norm = math.hypot(*vector), math.hypot(*other)
            return sum((x / query_norm) * (y / other_norm) for x, y in zip(vector, other))
        ranked = sorted(range(len(self.index["vectors"])), key=lambda i: score(self.index["vectors"][i]), reverse=True)
        selected = ranked[:self.top_k]
        # 필수 대상 정의는 검색 점수와 무관하게 함께 제공한다.
        exact = next((i for i, doc in enumerate(self.index["documents"])
                      if doc["definition"] == resource_type and doc.get("kind", "overview") == "overview"), None)
        if exact is not None and exact not in selected:
            selected = [exact, *selected[:max(0, self.top_k - 1)]]
        return "\n\n".join(self.index["documents"][i]["content"] for i in selected)

    @property
    def identity(self) -> dict[str, Any]:
        return {key: self.index[key] for key in ("index_sha256", "schema_sha256", "embedding_identity", "kind")}
