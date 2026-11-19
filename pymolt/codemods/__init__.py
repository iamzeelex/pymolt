"""
pymolt.codemods — the LOCAL side of the codemod loop.

The local delta engine (``pymolt.delta``) derives codemod *patterns* and picks curated
*rules*; this package applies them to the user's repository. Everything runs on this
machine.

  - models.CodemodPattern  : one old → new symbol replacement
  - service.build_migration_plan : the plan's previews (nothing is written)
  - apply.apply_to_repo     : rewrite local source via LibCST (format-preserving)
"""

from pymolt.codemods.models import CodemodPattern

__all__ = ["CodemodPattern"]
