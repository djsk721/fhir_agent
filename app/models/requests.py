from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class MimicPayload(BaseModel):
    patient: dict[str, Any] = Field(default_factory=dict)
    admission: dict[str, Any] | None = None
    lab_event: dict[str, Any] | None = None


class AnalyzeRequest(BaseModel):
    payload: MimicPayload
    resource_types: list[str] | None = None


class MappingRequest(BaseModel):
    itemid: int | str | None = None
    payload: MimicPayload | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_input(self) -> "MappingRequest":
        if self.itemid is None and self.payload is None:
            raise ValueError("Either 'itemid' or 'payload' must be provided.")
        return self
