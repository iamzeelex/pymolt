"""Curated framework-succession catalog.

Hand-authored ``SuccessionEdge``s for dead-framework eras, keyed by the framework a
project is *coming from*. Like ``rule_catalog``, this is curated knowledge — not
derived from PyPI diffs — because "where should this code go" is a design judgement,
not a version diff.

Seeded for the canonical dead project (matterport/Mask_RCNN: TF1.x + standalone Keras
+ py3.6):
  - keras -> tensorflow.keras            IN_PLACE  (Keras absorbed into TF2)
  - tensorflow<2 -> tensorflow>=2        IN_PLACE  (compat.v1 shims for the graph API)
  - keras -> torch/torchvision           TRANSPLANT (Mask R-CNN is built into torchvision)

Pure data: the server never executes anything. pymolt applies IN_PLACE bundles via its
LibCST pipeline (re-verifying each rule locally) and renders TRANSPLANT as a plan.
"""

from __future__ import annotations

from collections.abc import Iterable

from axiom_graph.core.models import CodemodPattern
from axiom_graph.core.succession import (
    AbstractionMapping,
    SuccessionEdge,
    SuccessionStrategy,
    WeightConversion,
)


def _import_move(old: str, new: str) -> CodemodPattern:
    """A single-symbol import move (`from OLD import LEAF` → `from NEW import LEAF`)."""
    return CodemodPattern(
        old_qualname=old, new_qualname=new, kind="rewrite-import", confidence="high",
        evidence=[f"curated framework succession: {old} -> {new}"],
    )


def _module_move(old: str, new: str) -> CodemodPattern:
    """A whole-namespace move (`OLD.*` → `NEW.*`, any symbol) — pymolt's rewrite-module."""
    return CodemodPattern(
        old_qualname=old, new_qualname=new, kind="rewrite-module", confidence="high",
        evidence=[f"curated framework succession: {old}.* -> {new}.*"],
    )


def _attr_move(old: str, new: str) -> CodemodPattern:
    """An attribute-path move (`m.OLD_TAIL` → `m.NEW_TAIL`) — pymolt's rewrite-attr.
    Covers the dominant `tf.Session()` form (vs _import_move's `from tf import Session`)."""
    return CodemodPattern(
        old_qualname=old, new_qualname=new, kind="rewrite-attr", confidence="high",
        evidence=[f"curated framework succession: {old} -> {new}"],
    )


# TF1 graph-API symbols and their TF2 homes. Each gets BOTH a rewrite-attr pattern
# (the dominant `tf.Session()` attribute form) and a rewrite-import pattern (the
# `from tensorflow import Session` form) so both call shapes migrate.
_TF1_MOVES = [
    ("tensorflow.Session", "tensorflow.compat.v1.Session"),
    ("tensorflow.placeholder", "tensorflow.compat.v1.placeholder"),
    ("tensorflow.get_variable", "tensorflow.compat.v1.get_variable"),
    ("tensorflow.global_variables_initializer",
     "tensorflow.compat.v1.global_variables_initializer"),
    ("tensorflow.variable_scope", "tensorflow.compat.v1.variable_scope"),
    ("tensorflow.assign", "tensorflow.compat.v1.assign"),
    ("tensorflow.log", "tensorflow.math.log"),
]


# keras (standalone) -> tensorflow.keras: the modules Mask_RCNN-era code imports.
_KERAS_TO_TFKERAS = SuccessionEdge(
    from_framework="keras",
    to_framework="tensorflow.keras",
    strategy=SuccessionStrategy.IN_PLACE,
    summary="Standalone Keras was absorbed into TensorFlow 2 as tf.keras; re-point imports.",
    patterns=[
        # One namespace move covers keras, keras.layers, keras.models, keras.backend, … —
        # `from keras.X import Y` -> `from tensorflow.keras.X import Y`, `import keras` ->
        # `import tensorflow.keras as keras`.
        _module_move("keras", "tensorflow.keras"),
    ],
    confidence="curated",
    evidence=["Keras 2.3 was the last multi-backend release; TF2 ships tf.keras"],
    doc_link="https://keras.io/getting_started/faq/#whats-the-difference-between-keras-and-tfkeras",
    notes=[
        "keras.engine.* internals (e.g. keras.engine.Layer) have no clean tf.keras twin — "
        "those sites land in the manual zone, not an auto-shim.",
    ],
)

# tensorflow 1.x graph API -> tensorflow 2.x: the compat.v1 shims tf_upgrade_v2 emits.
_TF1_TO_TF2 = SuccessionEdge(
    from_framework="tensorflow<2",
    to_framework="tensorflow>=2",
    strategy=SuccessionStrategy.IN_PLACE,
    summary="TF2 removed the TF1 graph/session API; route the calls through tf.compat.v1.",
    patterns=[
        p for old, new in _TF1_MOVES for p in (_attr_move(old, new), _import_move(old, new))
    ],
    confidence="curated",
    evidence=["Google's tf_upgrade_v2 rewrites these symbols to tf.compat.v1"],
    doc_link="https://www.tensorflow.org/guide/migrate",
    notes=[
        "compat.v1 keeps TF1 code RUNNING on TF2 — it is a life-support shim, not a real "
        "port to eager/Keras. It is the cheapest path, with the lowest ceiling.",
        "Both call shapes migrate: `tf.Session()` (rewrite-attr) and `from tensorflow import "
        "Session` (rewrite-import). This is a curated subset of the graph API — for the full "
        "sweep, Google's tf_upgrade_v2 remains the exhaustive tool.",
    ],
)

# keras -> torch/torchvision: the transplant. Mask R-CNN is a built-in torchvision model.
_KERAS_TO_TORCH = SuccessionEdge(
    from_framework="keras",
    to_framework="torch+torchvision",
    strategy=SuccessionStrategy.TRANSPLANT,
    summary="Re-init from torchvision's Mask R-CNN; port the weights, not the framework.",
    abstraction_mappings=[
        AbstractionMapping(from_symbol="keras.Model", to_symbol="torch.nn.Module",
                           mechanical=False, notes="subclass nn.Module; define forward()"),
        AbstractionMapping(from_symbol="keras.layers.Conv2D", to_symbol="torch.nn.Conv2d",
                           mechanical=True, notes="NHWC->NCHW; kernel/stride args reorder"),
        AbstractionMapping(from_symbol="keras.layers.BatchNormalization",
                           to_symbol="torch.nn.BatchNorm2d", mechanical=True),
        AbstractionMapping(from_symbol="keras.layers.Dense", to_symbol="torch.nn.Linear",
                           mechanical=True),
        AbstractionMapping(from_symbol="keras.layers.MaxPooling2D",
                           to_symbol="torch.nn.MaxPool2d", mechanical=True),
        AbstractionMapping(from_symbol="model.fit", to_symbol="a manual training loop",
                           mechanical=False, notes="torch has no .fit(); write the loop"),
    ],
    scaffold_hint=(
        "from torchvision.models.detection import maskrcnn_resnet50_fpn\n"
        "model = maskrcnn_resnet50_fpn(weights=None, num_classes=<your N>)\n"
        "# keep matterport's Config/Dataset; drop mrcnn.model — torchvision IS the model."
    ),
    weight_conversion=WeightConversion(
        from_format=".h5",
        to_format=".pth",
        approach="Read Keras layer weights with h5py, map each conv/bn/linear tensor into the "
                 "torchvision state_dict by name+shape, transposing conv kernels (KHWC->OIHW).",
        notes="Backbone (ResNet) maps cleanly; the RPN/heads differ — expect manual alignment.",
    ),
    confidence="curated",
    evidence=["torchvision.models.detection.maskrcnn_resnet50_fpn is the maintained equivalent"],
    doc_link="https://pytorch.org/vision/stable/models/mask_rcnn.html",
    notes=[
        "Highest durability, highest effort. pymolt does NOT auto-rewrite this — it renders the "
        "plan and verifies behavioural equivalence via `pymolt contract` (old .h5 vs new .pth "
        "must agree on the same inputs).",
    ],
)

_CATALOG: dict[str, list[SuccessionEdge]] = {
    "keras": [_KERAS_TO_TFKERAS, _KERAS_TO_TORCH],
    "tensorflow": [_TF1_TO_TF2],
    "tensorflow<2": [_TF1_TO_TF2],
}


def succession_for(frameworks: Iterable[str]) -> list[SuccessionEdge]:
    """Return curated succession edges for the given detected frameworks.

    Matching is case-insensitive on the base package name (``keras``, ``tensorflow``),
    so a detected ``tensorflow==1.15`` still hits the ``tensorflow`` key. De-duplicates
    while preserving first-seen order.
    """
    seen: set[int] = set()
    out: list[SuccessionEdge] = []
    for fw in frameworks:
        base = str(fw).strip().lower().split("<")[0].split(">")[0].split("=")[0].split("~")[0]
        for edge in _CATALOG.get(base, []):
            if id(edge) not in seen:
                seen.add(id(edge))
                out.append(edge)
    return out
