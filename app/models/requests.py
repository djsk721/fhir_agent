from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class MimicPayload(BaseModel):
    patient: dict[str, Any] = Field(default_factory=dict)
    admission: dict[str, Any] | None = None
    admissions: list[dict[str, Any]] | None = None
    lab_event: dict[str, Any] | None = None

    @model_validator(mode="after")
    def normalize_admissions(self) -> "MimicPayload":
        admissions = list(self.admissions or [])

        if self.admission is not None and not admissions:
            admissions = [self.admission]

        if self.admission is None and admissions:
            self.admission = admissions[0]

        self.admissions = admissions or None
        return self


class AnalyzeRequest(BaseModel):
    payload: MimicPayload
    resource_types: list[str] | None = None
    enable_cross_validation: bool = True


class MappingRequest(BaseModel):
    itemid: int | str | None = None
    payload: MimicPayload | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_input(self) -> "MappingRequest":
        if self.itemid is None and self.payload is None:
            raise ValueError("Either 'itemid' or 'payload' must be provided.")
        return self
