"""Framework-succession model — cross-framework migration knowledge.

The version-delta layer (CodemodPattern / CodemodRule / FullDelta) answers "how did
one library change between two versions?". It cannot express the harder, higher-value
question behind a dead project like Mask_RCNN: the code is written against a dead
framework era (TensorFlow 1.x + standalone Keras) — where should it GO, and how? That
is not a version bump; it is a move to a DIFFERENT framework at a DIFFERENT abstraction
level.

A ``SuccessionEdge`` is that knowledge, CURATED (like rule_catalog), not derived from
PyPI diffs. Each edge carries one of two strategies:

  - IN_PLACE ("living corpse"): stay on the same lineage, shim the breaks. Carried as a
    ``CodemodBundle`` shape — ``CodemodPattern``s (import/call renames, e.g.
    ``keras.layers`` -> ``tensorflow.keras.layers``, ``tf.Session`` ->
    ``tf.compat.v1.Session``) plus optional compound ``CodemodRule``s — so pymolt's
    existing LibCST apply pipeline executes them unchanged. Mechanical, cheap, lower ceiling.

  - TRANSPLANT ("organ transplant"): move to a modern framework (keras -> torch /
    torchvision / Detectron2). NOT auto-rewritable — the abstraction shift is a
    human/agent design task. pymolt emits a PLAN (abstraction mappings + a scaffold hint
    for the modern model + a weight-conversion recipe, e.g. ``.h5`` -> ``.pth``), and
    ``pymolt contract`` verifies the ported model behaves like the original. Honesty-first:
    guide and verify, never silently rewrite and claim the math survived.

Pure data: the server never executes anything; pymolt is the sole executor/verifier
(``confidence`` here is only a claim pymolt re-derives locally).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from axiom_graph.core.models import CodemodPattern, CodemodRule


class SuccessionStrategy(StrEnum):
    """How a dead-framework project moves forward."""

    IN_PLACE = "in_place"  # living corpse: shim the breaks, stay on the lineage
    TRANSPLANT = "transplant"  # organ transplant: port to a modern framework


class AbstractionMapping(BaseModel):
    """One symbol/concept mapping across the framework boundary."""

    from_symbol: str  # "keras.models.Model", "keras.layers.Conv2D"
    to_symbol: str  # "tensorflow.keras.Model", "torch.nn.Conv2d"
    mechanical: bool = False  # True → a plain rename a codemod could do; False → design work
    notes: str | None = None


class WeightConversion(BaseModel):
    """How to carry trained weights across a transplant."""

    from_format: str  # ".h5" (Keras)
    to_format: str  # ".pth" (PyTorch state_dict)
    approach: str  # short recipe / tool pointer
    notes: str | None = None


class SuccessionEdge(BaseModel):
    """A curated cross-framework migration path for a dead-framework project."""

    from_framework: str  # "keras", "tensorflow<2"
    to_framework: str  # "tensorflow.keras", "torch+torchvision", "detectron2"
    strategy: SuccessionStrategy
    summary: str

    # IN_PLACE: executable shims, in the exact shape of a CodemodBundle (patterns +
    # rules), so pymolt's existing apply pipeline runs them with zero new machinery.
    patterns: list[CodemodPattern] = Field(default_factory=list)  # rename-import / rename-call
    rules: list[CodemodRule] = Field(default_factory=list)  # compound match/rewrite templates

    # TRANSPLANT: guidance pymolt renders as a plan and `pymolt contract` verifies —
    # never auto-applied.
    abstraction_mappings: list[AbstractionMapping] = Field(default_factory=list)
    scaffold_hint: str | None = None
    weight_conversion: WeightConversion | None = None

    confidence: str = "curated"  # "curated" | "heuristic"
    evidence: list[str] = Field(default_factory=list)
    doc_link: str | None = None
    notes: list[str] = Field(default_factory=list)
