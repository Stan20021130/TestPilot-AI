"""Typed contracts for the verify phase.

Nothing here knows about ``AgentState``, LangGraph or the legacy agent nodes.
The request is what the verify phase needs; the report is what it promises.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Verdict(StrEnum):
    GREEN = "green"
    RED = "red"
    INCONCLUSIVE = "inconclusive"


class ExitReason(StrEnum):
    GREEN = "green"
    NO_TESTS = "no_tests"
    FAILURES_REPORTED = "failures_reported"
    MUTATION_SURVIVED = "mutation_survived"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EXECUTION_ERROR = "execution_error"
    NO_PROGRESS = "no_progress"
    SOURCE_PATCHES_PROPOSED = "source_patches_proposed"
    NO_APPLICABLE_ACTION = "no_applicable_action"


class TestCase(BaseModel):
    """A generated test, addressable by id."""

    id: str
    path: str
    content: str | None = None


class TestBundle(BaseModel):
    """Tests the verify phase may run."""

    generated: list[TestCase] = Field(default_factory=list)
    existing: list[str] = Field(default_factory=list)

    def has_tests(self) -> bool:
        return bool(self.generated or self.existing)

    def paths(self) -> list[str]:
        return [test.path for test in self.generated] + list(self.existing)


class FailureFact(BaseModel):
    """One failing test, with the attribution the controller routes on."""

    test_id: str
    message: str = ""
    origin: Literal["test_defect", "product_defect", "environment", "unknown"] = "unknown"


class RunResult(BaseModel):
    """What one execution of the suite produced."""

    status: Literal["passed", "failed", "error"]
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    failures: list[FailureFact] = Field(default_factory=list)

    def signature(self) -> str:
        if not self.failures:
            return ""
        joined = "|".join(sorted(f"{f.test_id}::{f.message[:120]}" for f in self.failures))
        import hashlib

        return hashlib.sha1(joined.encode()).hexdigest()[:16]


class TestPatch(BaseModel):
    """A change to a generated test; source patches are never applied here."""

    test_id: str
    replacement: str
    rationale: str = ""


class PatchProposal(BaseModel):
    """A source change proposed for review, not applied by the engine."""

    file: str
    diff: str
    rationale: str = ""


class MutationReport(BaseModel):
    total: int = 0
    killed: int = 0
    survived: list[str] = Field(default_factory=list)

    @property
    def score(self) -> float:
        return (self.killed / self.total) if self.total else 1.0


class VerifyBudget(BaseModel):
    """Limits are enforced by the controller, not by the decider."""

    max_rounds: int = 3
    max_repairs: int = 2
    max_mutations: int = 1
    max_steps: int = 6


class VerifyRequest(BaseModel):
    """Everything the verify phase is allowed to touch."""

    workspace: str
    bundle: TestBundle
    runner: str = "pytest"
    budget: VerifyBudget = Field(default_factory=VerifyBudget)


class ToolCall(BaseModel):
    """One ledger entry: what was chosen, what it did."""

    tool: str
    args_digest: str
    status: Literal["ok", "error", "rejected"] = "ok"
    summary: str = ""


class VerifyReport(BaseModel):
    """The only output of the verify phase."""

    verdict: Verdict
    exit_reason: ExitReason
    rounds: int
    runs: list[RunResult] = Field(default_factory=list)
    failures: list[FailureFact] = Field(default_factory=list)
    proposals: list[PatchProposal] = Field(default_factory=list)
    mutations: MutationReport = Field(default_factory=MutationReport)
    ledger: list[ToolCall] = Field(default_factory=list)
