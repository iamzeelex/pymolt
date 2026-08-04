from pydantic import BaseModel, Field

from pymolt.core.enums import RiskTier


class CveFinding(BaseModel):
    id: str
    severity: str | None = None
    fixed_version: str | None = None
    summary: str | None = None


class PackageRisk(BaseModel):
    name: str
    baseline_version: str | None = None
    target_version: str | None = None
    # CVEs that still affect the version you would run after migrating.
    open_cves: list[CveFinding] = Field(default_factory=list)
    # CVE ids that affect the baseline pin but not the target version.
    fixed_by_migration: list[str] = Field(default_factory=list)
    # pure-wheel | target-wheel | no-target-wheel | sdist-only | unknown
    wheel_status: str = "unknown"
    needs_compilation: bool = False
    last_release: str | None = None      # ISO date of the most recent release
    days_since_release: int | None = None
    abandoned: bool = False
    tier: RiskTier = RiskTier.LOW
    reasons: list[str] = Field(default_factory=list)


class RiskReport(BaseModel):
    packages: list[PackageRisk] = Field(default_factory=list)
    assessed: int = 0
    skipped: int = 0          # nodes without a pinned version (not assessable)
    errors: int = 0           # packages where a data source could not be reached
    target_python: str | None = None

    def by_tier(self, tier: RiskTier) -> list[PackageRisk]:
        return [p for p in self.packages if p.tier == tier]
