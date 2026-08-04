"""pymolt-side framework-succession models.

Mirror of Axiom Graph's ``SuccessionEdge`` (pymolt never imports the server). The
IN_PLACE bundle reuses pymolt's own ``CodemodPattern`` / ``CodemodRule`` — the exact
types the LibCST apply pipeline already consumes — so an in-place shim edge flows
straight into ``apply_to_repo`` / ``apply_rules_to_repo`` with no translation. The
TRANSPLANT fields are guidance pymolt renders and verifies via contract (S3), never
auto-applies.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from pymolt.codemods.models import CodemodPattern
from pymolt.codemods.rules import CodemodRule


class AbstractionMapping(BaseModel):
    from_symbol: str
    to_symbol: str
    mechanical: bool = False
    notes: str | None = None


class WeightConversion(BaseModel):
    from_format: str
    to_format: str
    approach: str
    notes: str | None = None


class SuccessionEdge(BaseModel):
    """A curated cross-framework migration path returned by /succession."""

    from_framework: str
    to_framework: str
    strategy: str  # "in_place" | "transplant"
    summary: str = ""

    # IN_PLACE: executable shims, in pymolt's own codemod types.
    patterns: list[CodemodPattern] = Field(default_factory=list)
    rules: list[CodemodRule] = Field(default_factory=list)

    # TRANSPLANT: a plan (rendered + contract-verified, never auto-applied).
    abstraction_mappings: list[AbstractionMapping] = Field(default_factory=list)
    scaffold_hint: str | None = None
    weight_conversion: WeightConversion | None = None

    confidence: str = "curated"
    evidence: list[str] = Field(default_factory=list)
    doc_link: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def is_in_place(self) -> bool:
        return self.strategy == "in_place"

    @property
    def is_transplant(self) -> bool:
        return self.strategy == "transplant"
