"""Read-only preflight for the migration and verification workflow."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, computed_field

from pymolt.ingestion.config import EnvConfig
from pymolt.ingestion.detect import detect_sources
from pymolt.migration_plan import MigrationPlan, inspect_migration_plan
from pymolt.migration_state import MigrationReceipt
from pymolt.verify.models import CaptureValidity
from pymolt.verify.service import (
    container_is_running,
    inspect_trace_artifact,
    load_contract_state,
)


class DoctorCheck(BaseModel):
    name: str
    state: str  # ok | warning | error
    detail: str
    hint: str | None = None


class DoctorReport(BaseModel):
    project_dir: str
    checks: list[DoctorCheck] = Field(default_factory=list)
    workload_command: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def ready(self) -> bool:
        return not any(check.state == "error" for check in self.checks)


def detect_workload_command(project_dir: str | Path) -> list[str]:
    """Return the most likely repeatable workload, without executing anything."""
    root = Path(project_dir)
    pyproject = root / "pyproject.toml"
    has_pytest_config = any(
        (root / name).is_file()
        for name in ("pytest.ini", "setup.cfg")
    )
    if pyproject.is_file():
        try:
            has_pytest_config = has_pytest_config or "[tool.pytest" in pyproject.read_text(
                encoding="utf-8"
            )
        except OSError:
            pass
    if has_pytest_config or (root / "tests").is_dir():
        return ["python", "-m", "pytest"]
    if (root / "tox.ini").is_file():
        return ["tox"]
    if (root / "noxfile.py").is_file():
        return ["nox"]
    return []


def _capture_check(name: str, slot) -> DoctorCheck:
    if slot is None:
        return DoctorCheck(
            name=name, state="warning", detail="not captured",
            hint=f"Capture {name} before relying on behavioral verification.",
        )
    quality = inspect_trace_artifact(slot.trace_path)
    validity = getattr(slot, "validity", CaptureValidity.UNKNOWN)
    if validity is not CaptureValidity.VALID or quality.validity is not CaptureValidity.VALID:
        reason = slot.validity_reason or quality.reason or validity.value
        return DoctorCheck(name=name, state="error", detail=reason)
    return DoctorCheck(
        name=name,
        state="ok",
        detail=f"{slot.events} events · {slot.privacy_profile} · sample {slot.sample_rate:g}",
    )


def run_doctor(project_dir: str | Path) -> DoctorReport:
    """Inspect local prerequisites and persisted evidence; never write or execute."""
    root = Path(project_dir).resolve()
    report = DoctorReport(project_dir=str(root))

    try:
        sources = detect_sources(root)
    except OSError as exc:
        sources = []
        report.checks.append(DoctorCheck(
            name="manifest", state="error", detail=f"manifest scan failed: {exc}",
        ))
    if sources:
        report.checks.append(DoctorCheck(
            name="manifest", state="ok",
            detail=", ".join(source.path.name for source in sources[:5]),
        ))
    elif not any(check.name == "manifest" for check in report.checks):
        report.checks.append(DoctorCheck(
            name="manifest", state="error", detail="no dependency manifest found",
            hint="Run doctor from the project root.",
        ))

    config = EnvConfig.load(root / ".pymolt" / "env_config.json")
    if config is None:
        report.checks.append(DoctorCheck(
            name="configuration", state="error", detail="pymolt is not configured",
            hint="Run `pymolt setup .`.",
        ))
    else:
        report.checks.append(DoctorCheck(
            name="configuration", state="ok",
            detail=f"Python {config.base_python or '?'} → {config.target_python or '?'}",
        ))
        if config.selected_tool and config.selected_tool.value == "container":
            running = bool(config.container_id and container_is_running(config.container_id))
            report.checks.append(DoctorCheck(
                name="baseline environment",
                state="ok" if running else "error",
                detail=(
                    f"container {config.container_id[:12]} is running"
                    if running else f"container {config.container_id or '?'} is not running"
                ),
                hint=None if running else "Start the container or re-run setup.",
            ))
        else:
            report.checks.append(DoctorCheck(
                name="baseline environment", state="warning",
                detail=f"local Python {config.base_python or '?'}; interpreter not pinned",
            ))

        if config.target_env_path:
            target = Path(config.target_env_path)
            candidates = (target / "bin" / "python", target / "Scripts" / "python.exe")
            interpreter = next((path for path in candidates if path.is_file()), None)
            report.checks.append(DoctorCheck(
                name="target environment",
                state="ok" if interpreter else "error",
                detail=str(interpreter or target),
                hint=None if interpreter else "Build the configured target environment.",
            ))
        else:
            report.checks.append(DoctorCheck(
                name="target environment", state="warning",
                detail="no target virtualenv path configured",
                hint="Set target_env_path in setup before post-migration capture.",
            ))

    workload = detect_workload_command(root)
    report.workload_command = workload
    report.checks.append(DoctorCheck(
        name="workload",
        state="ok" if workload else "warning",
        detail=" ".join(workload) if workload else "no pytest/tox/nox workload detected",
        hint=None if workload else "Choose a repeatable smoke or application command.",
    ))

    contract = load_contract_state(root)
    report.checks.append(_capture_check("baseline capture", contract.baseline))
    report.checks.append(_capture_check("post-migration capture", contract.post_migration))

    plan = MigrationPlan.load(root)
    if plan is None:
        report.checks.append(DoctorCheck(
            name="migration plan", state="warning", detail="no exact plan saved",
            hint="Run `pymolt plan .` after assess and baseline capture.",
        ))
    else:
        inspection = inspect_migration_plan(root, plan)
        report.checks.append(DoctorCheck(
            name="migration plan",
            state="ok" if inspection.valid else "error",
            detail=(
                f"plan {plan.plan_id[:8]} · {len(plan.changes)} file(s)"
                if inspection.valid else "; ".join(inspection.reasons)
            ),
            hint=None if inspection.valid else "Regenerate it with `pymolt plan .`.",
        ))

    receipt = MigrationReceipt.load(root)
    receipt_applied = receipt is not None and receipt.status == "applied"
    report.checks.append(DoctorCheck(
        name="migration receipt",
        state="ok" if receipt_applied else "warning",
        detail=(
            f"run {receipt.run_id[:8]} · {len(receipt.files_changed)} file(s)"
            if receipt_applied else (
                f"run {receipt.run_id[:8]} was rolled back"
                if receipt else "no applied migration recorded"
            )
        ),
    ))
    return report
