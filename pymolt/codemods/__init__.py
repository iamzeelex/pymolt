"""
pymolt.codemods — the LOCAL side of the codemod loop.

Axiom Graph (the service) produces codemod *patterns* from public package
sources; pymolt fetches them and applies them to the user's repository here.
User code never leaves the machine — only dependency names/versions are sent.

  - models.CodemodPattern  : the pattern as received from the Axiom Graph API
  - client.AxiomGraphClient : fetch patterns (versions in, patterns out)
  - apply.apply_to_repo     : rewrite local source via LibCST (format-preserving)
"""

from pymolt.codemods.models import CodemodPattern

__all__ = ["CodemodPattern"]
