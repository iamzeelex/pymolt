from typing import Optional
from pydantic import BaseModel, Field


class IngestionReport(BaseModel):
    resolution_quality: str
    source_fixation: str
    manual_zone: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    detected_python: Optional[str] = None
