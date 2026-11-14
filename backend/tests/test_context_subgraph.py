"""Tests for the context subgraph and its GenerationPlan contract."""

from __future__ import annotations

import pytest
from langgraph.graph import END

from app.agents import context_controller_agent
from app.agents.context_graph import build_context_graph
from app.agents.routing import should_start_verify
from tests.orchestration_support import initial_state, stub_topology

pytestmark = pytest.mark.unit


def _state(**overrides):
    base = {
        "changed_nodes": [],
        "change_contracts": [],
        "retrieved_context": [],
        "impact_level": "moderate",
        "test_config": {"total_test_files": 1},
        "test_coverage_map": {},
        "test_targets": [],
        "affected_modules": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_controller_declines_without_changed_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context_controller_agent,
        "_supplementary_retrieval",
        lambda _state: ([], None),
    )

    result = context_controller_agent.context_controller_agent_node(_state())
    plan = result["generation_plan"]

    assert plan["declined"] is True
    assert plan["decline_reason"] == "no_changed_symbols"
    assert plan["changed_symbols"] == 0


def test_plan_carries_targets_and_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context_controller_agent,
        "_supplementary_retrieval",
        lambda _state: ([], None),
    )
    targets = [{"file": "app/api/billing.py", "priority": "critical"}]

    result = context_controller_agent.context_controller_agent_node(
        _state(
            changed_nodes=[
                {"name": "charge", "type": "function", "file_path": "app/api/billing.py"}
            ],
            change_contracts=[{"symbol": "charge", "file_path": "app/api/billing.py"}],
            retrieved_context=[{"file_path": "app/api/billing.py", "content": "x" * 300}],
            test_coverage_map={"app/api/billing.py": ["tests/test_billing.py"]},
            test_targets=targets,
            affected_modules=["app/services/refund.py"],
        )
    )
    plan = result["generation_plan"]

    assert plan["declined"] is False
    assert plan["decline_reason"] is None
    assert plan["context_mode"] == "full"
    assert plan["context_score"] == 100
    assert plan["targets"] == targets
    assert plan["evidence"]["contracts"] is True
    assert plan["affected_files"] == 1


def test_should_start_verify_routes_on_the_plan() -> None:
    assert should_start_verify({"should_stop": True}) == END
    assert (
        should_start_verify({"generation_plan": {"declined": True}, "affected_tests": []})
        == "review_agent"
    )
    assert (
        should_start_verify(
            {
                "generation_plan": {"declined": True},
                "affected_tests": ["tests/test_billing.py"],
            }
        )
        == "verify"
    )
    assert (
        should_start_verify(
            {
                "generation_plan": {"declined": False},
                "generated_tests": [{"function_name": "test_charge"}],
            }
        )
        == "verify"
    )
    assert should_start_verify({"generation_plan": {"declined": False}}) == "review_agent"


def test_context_subgraph_ends_with_a_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_topology(monkeypatch)
    graph = build_context_graph().compile()

    out = graph.invoke(initial_state())

    assert out["generation_plan"]["declined"] is False
    assert out["generation_plan"]["context_mode"] == "full"
    assert "context_controller_agent" in out["completed_agents"]
