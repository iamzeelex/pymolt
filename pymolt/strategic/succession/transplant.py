"""Transplant plan builder — the "organ transplant" succession path.

A TRANSPLANT edge (e.g. keras → torch/torchvision) is NOT auto-rewritten: the
abstraction shift is a design task. pymolt turns the edge into an ordered, honest
PLAN — scaffold the modern model, map the layers, port the weights, then *prove*
behavioural equivalence with `pymolt contract`. We guide and verify; we never
silently rewrite and claim the math survived.

Pure/read-only: builds a structured plan from the edge, writes and runs nothing.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.strategic.succession.models import SuccessionEdge


class TransplantStep(BaseModel):
    n: int
    title: str
    detail: str = ""
    code: str | None = None  # a code block (scaffold snippet, etc.)
    commands: list[str] = Field(default_factory=list)  # shell commands to run


class TransplantPlan(BaseModel):
    from_framework: str
    to_framework: str
    summary: str = ""
    steps: list[TransplantStep] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def build_transplant_plan(edge: SuccessionEdge, project_dir: str | Path = ".") -> TransplantPlan:
    """Turn a TRANSPLANT SuccessionEdge into an ordered plan ending in contract verification."""
    steps: list[TransplantStep] = []

    steps.append(TransplantStep(
        n=1, title="Scaffold the modern model",
        detail="Initialize the maintained equivalent; keep your Config/Dataset and logic, drop "
               "the dead framework's model code.",
        code=edge.scaffold_hint,
    ))

    if edge.abstraction_mappings:
        lines = []
        for m in edge.abstraction_mappings:
            tag = "auto  " if m.mechanical else "MANUAL"
            note = f"  — {m.notes}" if m.notes else ""
            lines.append(f"{tag}  {m.from_symbol} → {m.to_symbol}{note}")
        steps.append(TransplantStep(
            n=2, title="Map the abstraction layer",
            detail="Translate each layer/concept. 'auto' are mechanical renames; 'MANUAL' need "
                   "real design work:\n" + "\n".join(lines),
        ))

    if edge.weight_conversion:
        wc = edge.weight_conversion
        detail = wc.approach + (f"\n{wc.notes}" if wc.notes else "")
        steps.append(TransplantStep(
            n=3, title=f"Port the trained weights ({wc.from_format} → {wc.to_format})",
            detail=detail,
        ))

    steps.append(TransplantStep(
        n=len(steps) + 1, title="Verify behavioural equivalence with pymolt contract",
        detail="The honest gate for a transplant: the ported model must match the original on "
               "the same inputs. Capture the original's behaviour BEFORE you port, the ported "
               "model's after, then diff — replace the placeholder inference commands with yours.",
        commands=[
            "# 1) BEFORE porting — capture the original model on a fixture input:",
            "pymolt contract capture --when baseline --mode command -- "
            "python infer_original.py --input fixture",
            "# 2) after steps 1–3, run the PORTED model on the SAME input:",
            "pymolt contract capture --when post-migration --mode command -- "
            "python infer_ported.py --input fixture",
            "# 3) diff — the outputs must agree:",
            "pymolt contract report",
        ],
    ))

    return TransplantPlan(
        from_framework=edge.from_framework,
        to_framework=edge.to_framework,
        summary=edge.summary,
        steps=steps,
        notes=list(edge.notes),
    )
