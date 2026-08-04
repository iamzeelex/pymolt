from pydantic import BaseModel, Field

from pymolt.core.enums import Confidence


class VersionEvidence(BaseModel):
    """One (source -> Python version) hint, with provenance and confidence.

    ``kind`` distinguishes a concrete intended *runtime* (Dockerfile FROM,
    .python-version, Pipfile) from a *floor* constraint (requires-python,
    poetry python) — only runtimes can genuinely *diverge*; a floor is a lower
    bound, presented as such, and can only be *violated* by a runtime below it.
    """
    source: str           # e.g. "Dockerfile:FROM", "pyproject:requires-python"
    source_file: str
    version: str | None    # "3.6", "3.6.15", or None when undeterminable
    confidence: Confidence = Confidence.UNKNOWN
    note: str | None = None
    kind: str = "runtime"          # runtime | floor
    constraint: str | None = None  # raw spec for floor kind, e.g. ">=3.12"


class DockerStage(BaseModel):
    index: int
    name: str | None = None
    base_image: str = ""          # resolved base, e.g. "python:3.6-slim" or a prior stage name
    python_version: str | None = None
    confidence: Confidence = Confidence.UNKNOWN
    os_hint: str | None = None     # slim | alpine | bullseye | bookworm | buster | ...
    is_runtime: bool = False
    copies_venv_from: list[str] = Field(default_factory=list)  # stage names a venv is COPY --from'd
    note: str | None = None


class DockerfileSurface(BaseModel):
    path: str
    stages: list[DockerStage] = Field(default_factory=list)
    runtime_python: str | None = None
    runtime_confidence: Confidence = Confidence.UNKNOWN
    os_hint: str | None = None
    install_tools: list[str] = Field(default_factory=list)  # pip | poetry | pipenv | conda | uv | pdm
    notes: list[str] = Field(default_factory=list)


class ToxNoxSurface(BaseModel):
    path: str
    kind: str                      # "tox" | "nox"
    declared_versions: list[str] = Field(default_factory=list)
    extra_deps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProjectRoot(BaseModel):
    path: str                      # relative to the scanned repo root ("." for the top)
    manifests: list[str] = Field(default_factory=list)
    dockerfiles: list[DockerfileSurface] = Field(default_factory=list)
    tox_nox: list[ToxNoxSurface] = Field(default_factory=list)
    version_evidence: list[VersionEvidence] = Field(default_factory=list)
    is_workspace_member: bool = False

    def distinct_versions(self) -> list[str]:
        """Distinct concrete (major.minor) versions across the intended-runtime evidence.

        Only authoritative single-runtime sources (Dockerfile/.python-version/
        requires-python/Pipfile) live in ``version_evidence``; the tox/nox test
        matrix is intentionally excluded (see :meth:`tested_versions`), so this
        reflects genuine disagreement about the runtime, not a wide CI matrix.
        """
        seen = []
        for ev in self.version_evidence:
            if ev.version:
                mm = ".".join(ev.version.split(".")[:2])
                if mm not in seen:
                    seen.append(mm)
        return seen

    def runtime_versions(self) -> list[str]:
        """Distinct concrete (major.minor) versions among intended-runtime sources."""
        seen: list[str] = []
        for ev in self.version_evidence:
            if ev.kind == "runtime" and ev.version:
                mm = ".".join(ev.version.split(".")[:2])
                if mm not in seen:
                    seen.append(mm)
        return seen

    @property
    def is_divergent(self) -> bool:
        """True when concrete runtime sources disagree (a floor alone never diverges)."""
        return len(self.runtime_versions()) > 1

    def floor_violations(self) -> list[tuple[str, str]]:
        """Runtimes that fall below a declared floor: (runtime_version, floor_constraint)."""
        def _mm(v: str) -> tuple[int, ...]:
            try:
                return tuple(int(p) for p in v.split(".")[:2])
            except ValueError:
                return ()

        floors = [
            (ev.constraint or f">={ev.version}", _mm(ev.version))
            for ev in self.version_evidence
            if ev.kind == "floor" and ev.version
        ]
        out: list[tuple[str, str]] = []
        for rv in self.runtime_versions():
            rmm = _mm(rv)
            for constraint, fmm in floors:
                if rmm and fmm and rmm < fmm:
                    out.append((rv, constraint))
        return out

    def tested_versions(self) -> list[str]:
        """The CPython support window from tox/nox, as sorted distinct major.minor."""
        seen: set[str] = set()
        for tn in self.tox_nox:
            for v in tn.declared_versions:
                seen.add(".".join(v.split(".")[:2]))

        def _key(v: str) -> list[int]:
            try:
                return [int(p) for p in v.split(".")]
            except ValueError:
                return [0]

        return sorted(seen, key=_key)


class SurfaceMap(BaseModel):
    root_dir: str
    project_roots: list[ProjectRoot] = Field(default_factory=list)
    """Roots that are part of *this* project, primary first."""
    excluded_roots: list[str] = Field(default_factory=list)
    """"<path> — <reason>" for every manifest-bearing directory deliberately
    left out (git submodule, vendored tree, test fixture). Recorded rather than
    dropped: a root that vanishes with no explanation is indistinguishable from
    a root the scanner failed to find."""
    notes: list[str] = Field(default_factory=list)
