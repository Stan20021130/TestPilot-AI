"""Tests for the AgentState ↔ next-engine adapter and VERIFY_ENGINE switch."""

from __future__ import annotations

import pytest

from app.agents import verify_graph
from app.agents.state import AgentState
from app.verify import adapter
from app.verify.contracts import (
    ExitReason,
    MutationReport,
    RunResult,
    ToolCall,
    Verdict,
    VerifyReport,
)

pytestmark = pytest.mark.unit


def _state() -> AgentState:
    return {
        "workspace_path": "/tmp/repo",
        "generated_tests": [
            {
                "function_name": "test_charge",
                "test_file_path": "tests/test_billing.py",
                "content": "def test_charge():\n    assert True\n",
            }
        ],
        "affected_tests": ["tests/test_router.py"],
        "changed_files": [
            {"path": "app/services/billing.py", "status": "modified", "changed_lines": [3, 4]},
            {"path": "README.md", "status": "modified", "changed_lines": [1]},
        ],
        "budget": {"max_rounds": 4, "max_repairs": 1, "max_mutation_attempts": 0},
    }


def _report() -> VerifyReport:
    return VerifyReport(
        verdict=Verdict.GREEN,
        exit_reason=ExitReason.GREEN,
        rounds=1,
        runs=[RunResult(status="passed", total=2, passed=2)],
        mutations=MutationReport(total=2, killed=2),
        ledger=[
            ToolCall(tool="execute", args_digest="execute", summary="ok"),
            ToolCall(tool="probe_mutations", args_digest="probe", summary="ok"),
        ],
    )


def test_request_from_state_maps_bundle_budget_and_sources() -> None:
    request = adapter.request_from_state(_state())

    assert request.workspace == "/tmp/repo"
    assert [test.id for test in request.bundle.generated] == ["test_charge"]
    assert request.bundle.existing == ["tests/test_router.py"]
    assert request.budget.max_rounds == 4
    assert request.budget.max_repairs == 1
    assert request.budget.max_mutations == 0

    sources = adapter.source_files_from_state(_state())
    assert sources == {"app/services/billing.py": {3, 4}}


def test_apply_report_maps_execution_verification_and_agents() -> None:
    state = _state()
    request = adapter.request_from_state(state)

    updates = adapter.apply_report(state, request, _report())

    assert updates["execution_results"]["status"] == "passed"
    assert updates["execution_results"]["passed"] == 2
    assert updates["verification"]["exit_reason"] == "green"
    assert updates["verification"]["engine"] == "next"
    assert updates["verification"]["rounds"] == 1
    assert updates["completed_agents"] == ["execution_agent", "mutation_agent"]
    assert updates["decisions"][0]["agent"] == "verify"
    assert updates["mutation_score"] == 1.0


def test_apply_report_keeps_repaired_test_content() -> None:
    state = _state()
    request = adapter.request_from_state(state)
    request.bundle.generated[0].content = "def test_charge():\n    assert fixed\n"

    updates = adapter.apply_report(state, request, _report())

    assert updates["generated_tests"][0]["content"] == "def test_charge():\n    assert fixed\n"
    assert updates["repair_applied_tests"] == 1


def test_run_next_verify_calls_engine_with_mapped_request(monkeypatch) -> None:
    captured = {}

    def fake_run(request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return _report()

    monkeypatch.setattr(adapter, "run_default_verify", fake_run)

    updates = adapter.run_next_verify(_state())

    assert captured["request"].bundle.generated[0].id == "test_charge"
    assert captured["kwargs"]["source_files"] == {"app/services/billing.py": {3, 4}}
    assert updates["verification"]["exit_reason"] == "green"


def test_run_next_verify_without_workspace_is_an_error() -> None:
    updates = adapter.run_next_verify({"workspace_path": ""})

    assert "no workspace_path" in updates["errors"][0]


def test_verify_graph_is_the_next_engine() -> None:
    graph = verify_graph.build_verify_graph().compile()
    nodes = set(graph.get_graph().nodes)

    assert "next_verify" in nodes
    assert "decide" not in nodes
