from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_SQL_RESPONSE_FORMAT = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
    },
    "required": ["sql"],
    "additionalProperties": False,
}


class OllamaClient:
    """추가 프레임워크 없이 Ollama의 chat/embed API를 호출합니다."""

    def __init__(
        self,
        base_url: str,
        chat_model: str,
        embedding_model: str = "",
        timeout: int = 300,
        context_size: int = 8192,
        max_output_tokens: int = 1024,
        max_retries: int = 0,
        retry_delay_seconds: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self.timeout = timeout
        self.context_size = context_size
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds

    def _post(self, endpoint: str, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=timeout or self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")[:1000]
                retryable = 500 <= exc.code < 600 and attempt < self.max_retries
                if not retryable:
                    raise RuntimeError(
                        f"Ollama 호출 실패({self.base_url}, HTTP {exc.code}): {details}"
                    ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(
                        f"Ollama 호출 실패({self.base_url}): {exc}"
                    ) from exc
            if self.retry_delay_seconds:
                time.sleep(self.retry_delay_seconds)
        raise AssertionError("Ollama retry loop must return or raise")

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        result = self._post("/api/chat", {
            "model": self.chat_model,
            "stream": False,
            "think": False,
            "format": _SQL_RESPONSE_FORMAT,
            "options": {
                "temperature": 0,
                "num_ctx": self.context_size,
                "num_predict": self.max_output_tokens,
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        })
        return str(result.get("message", {}).get("content", ""))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        result = self._post("/api/embed", {"model": self.embedding_model, "input": texts})
        embeddings = result.get("embeddings")
        if not isinstance(embeddings, list):
            raise RuntimeError("Ollama embed 응답에 embeddings가 없습니다.")
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        if not vectors:
            raise RuntimeError("Ollama가 빈 임베딩 결과를 반환했습니다.")
        return vectors[0]
