"""Contract tests for the next-generation verify engine.

The engine is intentionally wired to fake tools here: no sandbox, no LLM, no
``AgentState``. What is under test is the request/report contract, the tool
registry, the budget guards and the deterministic policy.
"""

from __future__ import annotations

import pytest

from app.verify import (
    ExitReason,
    FailureFact,
    MutationReport,
    PatchProposal,
    RunResult,
    TestBundle,
    TestCase,
    TestPatch,
    Verdict,
    VerifyBudget,
    VerifyRequest,
    run_verify,
)
from app.verify.controller import FINISH, ActionDecision
from app.verify.tools import build_default_registry

pytestmark = pytest.mark.unit


class FakeRunner:
    def __init__(self, results: list[RunResult]) -> None:
        self.results = list(results)
        self.calls = 0

    def run(self, request: VerifyRequest) -> RunResult:
        self.calls += 1
        return self.results.pop(0)


class FakeRepairer:
    def __init__(self, patches: list[TestPatch]) -> None:
        self.patches = patches

    def repair(self, failures, bundle) -> list[TestPatch]:
        return list(self.patches)


class FakeEditor:
    def __init__(self) -> None:
        self.applied: list[TestPatch] = []

    def apply(self, patch: TestPatch) -> None:
        self.applied.append(patch)


class FakeProposer:
    def __init__(self, proposals: list[PatchProposal]) -> None:
        self.proposals = proposals

    def propose(self, failures) -> list[PatchProposal]:
        return list(self.proposals)


class FakeProbe:
    def __init__(self, report: MutationReport) -> None:
        self.report = report

    def probe(self, targets) -> MutationReport:
        return self.report


class ScriptedDecider:
    def __init__(self, decisions: list[ActionDecision]) -> None:
        self.decisions = list(decisions)

    def decide(self, run, available):
        return self.decisions.pop(0)


def _request(*, generated=None, existing=None, **budget) -> VerifyRequest:
    return VerifyRequest(
        workspace="/tmp/repo",
        bundle=TestBundle(generated=generated or [], existing=existing or []),
        budget=VerifyBudget(**budget),
    )


def _failure(test_id: str, origin: str = "test_defect", message: str = "boom") -> FailureFact:
    return FailureFact(test_id=test_id, origin=origin, message=message)


def _run(request, runner, *, repairer=None, editor=None, proposer=None, probe=None, decider=None):
    return run_verify(
        request,
        runner=runner,
        repairer=repairer or FakeRepairer([]),
        editor=editor or FakeEditor(),
        proposer=proposer or FakeProposer([]),
        probe=probe or FakeProbe(MutationReport(total=1, killed=1)),
        decider=decider,
    )


def test_green_run_and_mutation_probe() -> None:
    request = _request(existing=["tests/test_a.py"])
    runner = FakeRunner([RunResult(status="passed", total=1, passed=1)])
    probe = FakeProbe(MutationReport(total=2, killed=2))

    report = _run(request, runner, probe=probe)

    assert report.verdict is Verdict.GREEN
    assert report.exit_reason is ExitReason.GREEN
    assert report.rounds == 1
    assert [call.tool for call in report.ledger] == ["execute", "probe_mutations"]


def test_failing_test_is_repaired_then_rerun() -> None:
    test = TestCase(id="t1", path="tests/test_a.py", content="old")
    request = _request(generated=[test], max_mutations=0)
    runner = FakeRunner(
        [
            RunResult(status="failed", total=1, failed=1, failures=[_failure("t1")]),
            RunResult(status="passed", total=1, passed=1),
        ]
    )
    editor = FakeEditor()

    report = _run(
        request,
        runner,
        repairer=FakeRepairer([TestPatch(test_id="t1", replacement="new")]),
        editor=editor,
    )

    assert report.verdict is Verdict.GREEN
    assert report.rounds == 2
    assert len(report.runs) == 2
    assert editor.applied[0].replacement == "new"
    assert runner.calls == 2


def test_source_patch_is_proposed_not_applied_or_rerun() -> None:
    request = _request(existing=["tests/test_a.py"], max_mutations=0)
    runner = FakeRunner(
        [
            RunResult(
                status="failed",
                total=1,
                failed=1,
                failures=[_failure("t1", origin="product_defect")],
            )
        ]
    )
    editor = FakeEditor()

    report = _run(
        request,
        runner,
        editor=editor,
        proposer=FakeProposer([PatchProposal(file="app/x.py", diff="+ fix")]),
    )

    assert report.exit_reason is ExitReason.SOURCE_PATCHES_PROPOSED
    assert report.verdict is Verdict.RED
    assert report.rounds == 1
    assert editor.applied == []
    assert runner.calls == 1


def test_same_failure_twice_is_no_progress() -> None:
    request = _request(existing=["tests/test_a.py"], max_mutations=0)
    failing = RunResult(
        status="failed",
        total=1,
        failed=1,
        failures=[_failure("t1", message="same")],
    )
    runner = FakeRunner([failing, failing])

    report = _run(
        request,
        runner,
        repairer=FakeRepairer([TestPatch(test_id="t1", replacement="new")]),
    )

    assert report.exit_reason is ExitReason.NO_PROGRESS
    assert report.rounds == 2


def test_round_budget_stops_before_another_execution() -> None:
    request = _request(existing=["tests/test_a.py"], max_rounds=1, max_mutations=0)
    runner = FakeRunner(
        [
            RunResult(
                status="failed",
                total=1,
                failed=1,
                failures=[_failure("t1")],
            )
        ]
    )

    report = _run(
        request,
        runner,
        repairer=FakeRepairer([TestPatch(test_id="t1", replacement="new")]),
    )

    assert report.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert report.rounds == 1
    assert runner.calls == 1
    assert report.ledger[-1].status == "rejected"


def test_unknown_tool_is_rejected_by_the_registry() -> None:
    request = _request(existing=["tests/test_a.py"])
    runner = FakeRunner([RunResult(status="passed", total=1, passed=1)])
    decider = ScriptedDecider([ActionDecision("rm -rf", {}, "nope")])

    report = _run(request, runner, decider=decider)

    assert report.exit_reason is ExitReason.NO_APPLICABLE_ACTION
    assert report.ledger[-1].status == "rejected"


def test_invalid_tool_args_are_rejected() -> None:
    request = _request(existing=["tests/test_a.py"])
    runner = FakeRunner([RunResult(status="passed", total=1, passed=1)])
    decider = ScriptedDecider(
        [ActionDecision("probe_mutations", {"targets": "not-a-list"}, "bad args")]
    )

    report = _run(request, runner, decider=decider)

    assert report.exit_reason is ExitReason.NO_APPLICABLE_ACTION
    assert "invalid args" in report.ledger[-1].summary


def test_no_tests_is_inconclusive() -> None:
    request = _request()
    runner = FakeRunner([])

    report = _run(request, runner)

    assert report.exit_reason is ExitReason.NO_TESTS
    assert report.verdict is Verdict.INCONCLUSIVE
    assert runner.calls == 0


def test_registry_exposes_exactly_four_capabilities() -> None:
    registry = build_default_registry(
        runner=FakeRunner([]),
        repairer=FakeRepairer([]),
        editor=FakeEditor(),
        proposer=FakeProposer([]),
        probe=FakeProbe(MutationReport()),
    )

    assert registry.names() == [
        "execute",
        "repair_tests",
        "propose_source_patch",
        "probe_mutations",
    ]
    assert all("args" in item for item in registry.describe())


def test_finish_is_control_flow_not_a_tool() -> None:
    request = _request(existing=["tests/test_a.py"])
    runner = FakeRunner([RunResult(status="passed", total=1, passed=1)])
    decider = ScriptedDecider([ActionDecision(FINISH, {}, "done")])

    report = _run(request, runner, decider=decider)

    assert report.rounds == 0
    assert report.exit_reason is ExitReason.NO_APPLICABLE_ACTION


def test_report_is_serialisable() -> None:
    request = _request(existing=["tests/test_a.py"], max_mutations=0)
    report = _run(request, FakeRunner([RunResult(status="passed", total=1, passed=1)]))

    payload = report.model_dump()
    assert payload["verdict"] == "green"
    assert payload["ledger"][0]["tool"] == "execute"
