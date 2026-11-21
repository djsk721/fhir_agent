from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ProfileGenerationError(ValueError):
    """검증 실패한 모델 초안을 다음 보정 요청에 전달합니다."""

    def __init__(self, message: str, draft: dict[str, Any]) -> None:
        super().__init__(message)
        self.draft = draft


@dataclass(frozen=True)
class SourceBatch:
    """Data Extractor가 Renderer에 전달하는 데이터셋 독립 계약."""

    dataset: str
    dataset_version: str
    source_resource: str
    schema_version: str
    records: tuple[dict[str, Any], ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    extraction: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "source_resource": self.source_resource,
            "schema_version": self.schema_version,
            "records": [dict(row) for row in self.records],
            "metadata": self.metadata,
            "extraction": {
                **self.extraction,
                "record_count": len(self.records),
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceBatch":
        records = payload.get("records", [])
        if not isinstance(records, list) or any(
            not isinstance(row, dict) for row in records
        ):
            raise ValueError("Source records는 객체 배열이어야 합니다.")
        return cls(
            dataset=str(payload["dataset"]),
            dataset_version=str(payload["dataset_version"]),
            source_resource=str(payload["source_resource"]),
            schema_version=str(payload["schema_version"]),
            records=tuple(dict(row) for row in records),
            metadata=dict(payload.get("metadata") or {}),
            extraction=dict(payload.get("extraction") or {}),
        )
