from typing import Dict, Any, Optional
from pydantic import BaseModel, Field


class BaseLayer(BaseModel):
    layer_name: str
    data: Dict[str, Any] = Field(default_factory=dict)


class IngestionReport(BaseModel):
    resolution_quality: str
    source_fixation: str
    manual_zone: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    detected_python: Optional[str] = None
