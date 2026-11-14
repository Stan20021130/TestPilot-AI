"""The verify controller: observe → decide → act → settle.

No LangGraph state, no ambient ``AgentState``. The controller owns the run
ledger and the budget; tools only perform one capability and return a
:class:`ToolOutcome`. The decider can be the deterministic heuristic (default)
or a model-driven policy with the same interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from app.verify.contracts import (
    ExitReason,
    FailureFact,
    MutationReport,
    PatchProposal,
    RunResult,
    TestPatch,
    ToolCall,
    Verdict,
    VerifyReport,
    VerifyRequest,
)
from app.verify.tools import (
    MutationProbe,
    PatchService,
    RepairService,
    TestEditor,
    TestRunner,
    ToolOutcome,
    ToolRegistry,
    UnknownTool,
    build_default_registry,
)

FINISH = "finish"


@dataclass(frozen=True)
class ActionDecision:
    """A decision is a tool name plus validated-at-dispatch arguments."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class Decider(Protocol):
    def decide(
        self,
        run: VerifyRun,
        available: Sequence[str],
    ) -> ActionDecision: ...


@dataclass
class VerifyRun:
    """Mutable controller state; the only place run metadata is written."""

    request: VerifyRequest
    runs: list[RunResult] = field(default_factory=list)
    patches: list[TestPatch] = field(default_factory=list)
    proposals: list[PatchProposal] = field(default_factory=list)
    mutations: MutationReport | None = None
    ledger: list[ToolCall] = field(default_factory=list)
    steps: int = 0
    rounds: int = 0
    repairs: int = 0
    mutations_used: int = 0
    last_action: str = ""
    last_repair_applied: int = 0
    previous_signature: str = ""
    current_signature: str = ""

    @property
    def last_run(self) -> RunResult | None:
        return self.runs[-1] if self.runs else None

    @property
    def failures(self) -> list[FailureFact]:
        return self.last_run.failures if self.last_run else []


def _defect_count(failures: Sequence[FailureFact], origin: str) -> int:
    return sum(1 for failure in failures if failure.origin == origin)


class HeuristicDecider:
    """Deterministic policy; the default and the fallback for model failures."""

    def decide(self, run: VerifyRun, available: Sequence[str]) -> ActionDecision:
        budget = run.request.budget
        if run.steps >= budget.max_steps:
            return ActionDecision(FINISH, reason="step budget exhausted")

        last = run.last_run
        if last is None:
            if run.request.bundle.has_tests():
                return ActionDecision("execute", {"scope": "selected"}, "tests are available")
            return ActionDecision(FINISH, reason="no tests")

        if run.last_action == "propose_source_patch":
            return ActionDecision(FINISH, reason="source patch proposed for review")

        if last.status == "error":
            return ActionDecision(FINISH, reason="execution error")

        if last.failed:
            if run.last_action == "repair_tests":
                if run.last_repair_applied:
                    # The controller enforces the round budget; the decider
                    # only proposes the action.
                    return ActionDecision("execute", {"scope": "selected"}, "test patches applied")
                return ActionDecision(FINISH, reason="repair produced no test patch")
            if run.current_signature and run.current_signature == run.previous_signature:
                return ActionDecision(FINISH, reason="failure signature unchanged; no progress")
            failures = last.failures
            test_defects = _defect_count(failures, "test_defect")
            if test_defects > len(failures) / 2 and run.repairs < budget.max_repairs:
                return ActionDecision("repair_tests", reason="test defects dominate")
            products = _defect_count(failures, "product_defect")
            if products and not run.proposals and run.steps < budget.max_steps:
                return ActionDecision(
                    "propose_source_patch",
                    reason="product defect: propose, do not auto-apply",
                )
            return ActionDecision(FINISH, reason="failures need human review")

        # Green run.
        if (
            run.mutations is None
            and run.mutations_used < budget.max_mutations
            and run.request.bundle.has_tests()
        ):
            return ActionDecision("probe_mutations", reason="green run; probe assertions")
        return ActionDecision(FINISH, reason="green run")


class VerifyController:
    """Runs one verify request to a report."""

    def __init__(
        self,
        request: VerifyRequest,
        registry: ToolRegistry,
        decider: Decider | None = None,
    ) -> None:
        self.run = VerifyRun(request=request)
        self.registry = registry
        self.decider = decider or HeuristicDecider()
        self._forced_reason: str | None = None

    def run_to_report(self) -> VerifyReport:
        while True:
            budget = self.run.request.budget
            if self.run.steps >= budget.max_steps:
                self._forced_reason = ExitReason.BUDGET_EXHAUSTED.value
                break

            decision = self.decider.decide(self.run, self.registry.names())
            if decision.tool == FINISH:
                break

            if not self._budget_allows(decision.tool):
                self._forced_reason = ExitReason.BUDGET_EXHAUSTED.value
                self._record(decision.tool, "", "rejected", "budget exhausted")
                break

            try:
                spec = self.registry.get(decision.tool)
            except UnknownTool as exc:
                self._record(decision.tool, "", "rejected", str(exc))
                self._forced_reason = ExitReason.NO_APPLICABLE_ACTION.value
                break

            try:
                args = spec.args_model.model_validate(decision.args)
            except ValidationError as exc:
                self._record(decision.tool, "", "rejected", f"invalid args: {exc}")
                self._forced_reason = ExitReason.NO_APPLICABLE_ACTION.value
                break

            try:
                outcome = spec.run(self.run.request, self.run.failures, args)
            except Exception as exc:  # noqa: BLE001 - one tool failure ends the run
                self._record(decision.tool, spec.args_digest(args), "error", str(exc))
                self._forced_reason = ExitReason.EXECUTION_ERROR.value
                break

            self._apply(decision.tool, spec.args_digest(args), outcome)
            self.run.steps += 1
            self.run.last_action = decision.tool

        return self._settle()

    def _budget_allows(self, tool: str) -> bool:
        budget = self.run.request.budget
        if tool == "execute":
            return self.run.rounds < budget.max_rounds
        if tool == "repair_tests":
            return self.run.repairs < budget.max_repairs
        if tool == "probe_mutations":
            return self.run.mutations_used < budget.max_mutations
        return True

    def _apply(self, tool: str, digest: str, outcome: ToolOutcome) -> None:
        if outcome.run_result is not None:
            self.run.previous_signature = self.run.current_signature
            self.run.current_signature = outcome.run_result.signature()
            self.run.runs.append(outcome.run_result)
            self.run.rounds += 1
        if tool == "repair_tests":
            self.run.last_repair_applied = len(outcome.patches)
        if outcome.patches:
            self.run.patches.extend(outcome.patches)
            self.run.repairs += 1
        if outcome.proposals:
            self.run.proposals.extend(outcome.proposals)
        if outcome.mutation is not None:
            self.run.mutations = outcome.mutation
            self.run.mutations_used += 1
        self._record(tool, digest, "ok", outcome.summary)

    def _record(self, tool: str, digest: str, status: str, summary: str) -> None:
        self.run.ledger.append(
            ToolCall(tool=tool, args_digest=digest, status=status, summary=summary)
        )

    def _settle(self) -> VerifyReport:
        last = self.run.last_run
        forced = self._forced_reason

        if forced == ExitReason.NO_APPLICABLE_ACTION.value:
            reason = ExitReason.NO_APPLICABLE_ACTION
        elif (
            forced == ExitReason.EXECUTION_ERROR.value
            or last is not None
            and last.status == "error"
        ):
            reason = ExitReason.EXECUTION_ERROR
        elif last is None:
            reason = (
                ExitReason.NO_TESTS
                if not self.run.request.bundle.has_tests()
                else ExitReason.NO_APPLICABLE_ACTION
            )
        elif forced == ExitReason.BUDGET_EXHAUSTED.value:
            reason = ExitReason.BUDGET_EXHAUSTED
        elif last.failed:
            if self.run.current_signature and (
                self.run.current_signature == self.run.previous_signature
            ):
                reason = ExitReason.NO_PROGRESS
            elif self.run.proposals and not self.run.patches:
                reason = ExitReason.SOURCE_PATCHES_PROPOSED
            else:
                reason = ExitReason.FAILURES_REPORTED
        elif self.run.mutations is not None and self.run.mutations.survived:
            reason = ExitReason.MUTATION_SURVIVED
        else:
            reason = ExitReason.GREEN

        verdict = _verdict_for(reason)
        return VerifyReport(
            verdict=verdict,
            exit_reason=reason,
            rounds=self.run.rounds,
            runs=self.run.runs,
            failures=self.run.failures,
            proposals=self.run.proposals,
            mutations=self.run.mutations or MutationReport(),
            ledger=self.run.ledger,
        )


def _verdict_for(reason: ExitReason) -> Verdict:
    if reason in {ExitReason.GREEN}:
        return Verdict.GREEN
    if reason in {
        ExitReason.FAILURES_REPORTED,
        ExitReason.NO_PROGRESS,
        ExitReason.SOURCE_PATCHES_PROPOSED,
        ExitReason.MUTATION_SURVIVED,
    }:
        return Verdict.RED
    return Verdict.INCONCLUSIVE


def run_verify(
    request: VerifyRequest,
    *,
    runner: TestRunner,
    repairer: RepairService,
    editor: TestEditor,
    proposer: PatchService,
    probe: MutationProbe,
    decider: Decider | None = None,
) -> VerifyReport:
    """Build the default registry and run one verify request."""
    registry = build_default_registry(
        runner=runner,
        repairer=repairer,
        editor=editor,
        proposer=proposer,
        probe=probe,
    )
    return VerifyController(request, registry, decider).run_to_report()
