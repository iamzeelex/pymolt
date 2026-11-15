"""pymolt.delta — local, scoped API-change facts (replaces the Axiom Graph service).

``compute_deltas(project_dir, migrations, *, symbols=None, online=False, use_cache=True)``
returns one :class:`DeltaReport` (schema ``pymolt.delta/1``) per migration: what changed
between the old and the new version for the symbols the project touches. See
``engine`` for the flow, ``models`` for the schema, ``provider`` for the codemod bundle
``pymolt plan`` consumes. Nothing leaves the machine unless ``online=True`` (then only
package names and versions).
"""

from pymolt.delta.engine import compute_deltas, run_stats
from pymolt.delta.models import SCHEMA, DeltaFact, DeltaReport, RecordedWarning, TouchedSymbol

__all__ = [
    "SCHEMA",
    "DeltaFact",
    "DeltaReport",
    "RecordedWarning",
    "TouchedSymbol",
    "compute_deltas",
    "run_stats",
]
