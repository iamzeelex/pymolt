"""Legacy/baseline environment HINT emitter — pymolt does not build environments.

When ``assess`` cannot resolve the *baseline* (legacy) dependency graph because the
project's Python (e.g. 3.6) isn't installed — and installing an EOL interpreter on
the host is often impractical — pymolt refuses to guess. Fabricating a baseline by
resolving unpinned ``>=`` constraints to their latest releases would be a lie (it
reports the newest versions, not what the code was written against). Instead it
emits this hint: how to stand up the legacy interpreter (usually a throwaway
container) and how to point pymolt back at it.

Two audiences, one hint: a human gets copy-paste Docker commands; an LLM agent gets
``agent_brief`` — a self-contained task it can execute to build the env and hand
control back. Pure/read-only: builds strings, writes and runs nothing.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

_DOCKERFILE_TEMPLATE = """\
# Throwaway legacy env for pymolt — used ONLY to resolve the OLD dependency graph.
# pymolt runs pip-compile inside it; nothing is installed on your host, and the
# project's own dependencies are never upgraded or edited.
FROM python:{base}-slim
WORKDIR /src
RUN pip install --no-cache-dir pip-tools
CMD ["sleep", "infinity"]
"""


class BaselineHint(BaseModel):
    """How to establish the legacy baseline environment pymolt could not resolve."""

    base_python: str
    reason: str
    suggested_dockerfile: str
    suggested_commands: list[str] = Field(default_factory=list)
    rerun_command: str
    agent_brief: str
    notes: list[str] = Field(default_factory=list)


def build_baseline_hint(
    project_dir: str | Path, base_python: str, *, reason: str | None = None
) -> BaselineHint:
    """Build the legacy-baseline hint for ``base_python`` (e.g. "3.6")."""
    proj = Path(project_dir).resolve()
    tag = f"pymolt-legacy-py{base_python}"
    dockerfile = _DOCKERFILE_TEMPLATE.format(base=base_python)
    rerun = f"pymolt assess {proj} --current-python {base_python} --container {tag}"
    commands = [
        f"# 1) save the Dockerfile below as Dockerfile.pymolt-legacy in {proj}",
        f"docker build -f Dockerfile.pymolt-legacy -t {tag} {proj}",
        f'docker run -d --name {tag} -v "{proj}":/src {tag}',
        "# 2) re-run assess pointed at the running container:",
        rerun,
    ]
    agent_brief = (
        f"Stand up a Python {base_python} environment so pymolt can resolve the LEGACY "
        f"dependency graph of the project at {proj}. Preferred path: a devcontainer / Docker "
        f"image based on `python:{base_python}-slim` with `pip-tools` installed, left running "
        f"with the project mounted at /src (name it `{tag}`). Then hand control back so "
        f"`{rerun}` can resolve the baseline inside it. Hard constraints: do NOT upgrade, pin, "
        f"or edit the project's dependencies, and do NOT substitute a newer Python — the goal "
        f"is to capture the OLD graph faithfully, not to make it modern."
    )
    notes = [
        f"Python {base_python} is likely EOL and hard to install on a modern host — a container "
        "is usually the least-painful path.",
        "pymolt provisions nothing: it only resolves inside the environment you point it at "
        "(--container), never one it built itself.",
    ]
    return BaselineHint(
        base_python=base_python,
        reason=reason or f"no Python {base_python} interpreter available to resolve the baseline",
        suggested_dockerfile=dockerfile,
        suggested_commands=commands,
        rerun_command=rerun,
        agent_brief=agent_brief,
        notes=notes,
    )
