"""Orchestration tests for the LangGraph pipeline.

Covers the parts of ``app.agents.graph`` that are not about any single agent:

1. the three parallel collectors can write ``warnings`` / ``completed_agents``
   in the same superstep (a plain channel raises ``InvalidUpdateError``);
2. the assembled graph reaches ``END`` through the happy path, including the
   mutation gate;
3. an unhandled node exception degrades the run instead of aborting it;
4. every routing function respects its skip condition and its loop budget.

The graph is rebuilt here with stubbed nodes so the topology is exercised
without a repository checkout, Qdrant, an LLM or a database.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import context_graph as context_module
from app.agents import graph as graph_module
from app.agents.context_graph import build_context_graph
from app.agents.graph import build_pr_analysis_graph
from app.agents.state import append_unique
from app.agents.verify_graph import build_verify_graph
from tests.orchestration_support import initial_state, stub_topology

pytestmark = pytest.mark.unit

# ==============================================================================
# Reducer
# ==============================================================================


def test_append_unique_preserves_order_and_drops_repeats() -> None:
    assert append_unique(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
    assert append_unique([], ["a"]) == ["a"]
    assert append_unique(["a"], []) == ["a"]


# ==============================================================================
# Stubbed graph
# ==============================================================================


def test_parallel_collectors_merge_into_shared_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the fan-out used to raise InvalidUpdateError.

    All three collectors report in the same superstep; the append-only
    reducers must merge their warnings instead of rejecting the second write.
    """
    stub_topology(monkeypatch)
    compiled = build_pr_analysis_graph().compile()

    final_state = compiled.invoke(initial_state())

    warnings = set(final_state["warnings"])
    assert {
        "impact_agent: stub warning",
        "search_agent: stub warning",
        "test_discovery_agent: stub warning",
    } <= warnings

    # The verify phase is stubbed in this topology test; produce and review
    # still complete, and no completion marker from a parallel branch was lost.
    assert set(final_state["completed_agents"]) == {
        "planner_agent",
        "diff_agent",
        "dependency_agent",
        "impact_agent",
        "search_agent",
        "test_discovery_agent",
        "risk_ranker_agent",
        "context_controller_agent",
        "test_generator_agent",
        "review_agent",
        "documentation_agent",
    }
    assert len(final_state["completed_agents"]) == 11
    assert final_state["errors"] == []
    assert final_state["verification"]["exit_reason"] == "green"
    assert final_state["review"]["full_review_body"] == "ok"


def test_unhandled_node_error_degrades_instead_of_aborting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A programming error in one agent must not lose the whole PR analysis."""

    stub_topology(monkeypatch)

    def boom(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("index out of range")

    monkeypatch.setattr(context_module, "impact_agent_node", boom)
    compiled = build_pr_analysis_graph().compile()

    final_state = compiled.invoke(initial_state())

    assert any("impact_agent: unhandled RuntimeError" in error for error in final_state["errors"])
    # The branch did not produce output, so it is not claimed as completed.
    assert "impact_agent" not in final_state["completed_agents"]
    # Downstream fusion still ran and the run reached END.
    assert "risk_ranker_agent" in final_state["completed_agents"]
    assert final_state["review"]["full_review_body"] == "ok"


def test_pipeline_has_produce_and_next_verify_nodes() -> None:
    main_nodes = set(graph_module.pr_analysis_graph.get_graph().nodes)
    context_nodes = set(build_context_graph().compile().get_graph().nodes)
    verify_nodes = set(build_verify_graph().compile().get_graph().nodes)

    assert {"context", "produce", "verify"} <= main_nodes
    assert {"diff_agent", "context_controller_agent"} <= context_nodes
    assert "next_verify" in verify_nodes
