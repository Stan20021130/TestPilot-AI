"""The context phase as a LangGraph subgraph.

Diff parsing, dependency traversal, the three parallel collectors and the two
fusion gates were the last flat block in the main graph. They are one phase:
their only output is a :class:`~app.agents.state.GenerationPlan` — what to test,
with what evidence, or why generation should be declined.

Embedding them as a subgraph keeps the parent topology readable:

    planner → context → (verify | review) → review → documentation

The subgraph shares ``AgentState`` with its parent. Its exit is the context
controller, which writes ``generation_plan``; the parent decides from that plan
whether the verify loop is worth entering.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agents.context_controller_agent import context_controller_agent_node
from app.agents.dependency_agent import dependency_agent_node
from app.agents.diff_agent import diff_agent_node
from app.agents.impact_agent import impact_agent_node
from app.agents.node_guard import guarded
from app.agents.risk_ranker_agent import risk_ranker_agent_node
from app.agents.routing import should_continue_after_diff
from app.agents.search_agent import search_agent_node
from app.agents.state import AgentState
from app.agents.test_discovery_agent import test_discovery_agent_node
from app.core.logging import get_logger

logger = get_logger(__name__)


def build_context_graph() -> StateGraph:
    """Assemble the context subgraph (uncompiled)."""
    graph = StateGraph(AgentState)

    graph.add_node("diff_agent", guarded("diff_agent", diff_agent_node))
    graph.add_node("dependency_agent", guarded("dependency_agent", dependency_agent_node))
    graph.add_node("impact_agent", guarded("impact_agent", impact_agent_node))
    graph.add_node("search_agent", guarded("search_agent", search_agent_node))
    graph.add_node(
        "test_discovery_agent",
        guarded("test_discovery_agent", test_discovery_agent_node),
    )
    graph.add_node("risk_ranker_agent", guarded("risk_ranker_agent", risk_ranker_agent_node))
    graph.add_node(
        "context_controller_agent",
        guarded("context_controller_agent", context_controller_agent_node),
    )

    graph.add_edge(START, "diff_agent")

    # No supported change → skip straight to the gate, which will produce a
    # declined plan. A hard stop ends the subgraph; the parent routes to END.
    graph.add_conditional_edges(
        "diff_agent",
        should_continue_after_diff,
        {
            "dependency_agent": "dependency_agent",
            "review_agent": "context_controller_agent",
            END: END,
        },
    )

    graph.add_edge("dependency_agent", "impact_agent")
    graph.add_edge("dependency_agent", "search_agent")
    graph.add_edge("dependency_agent", "test_discovery_agent")

    graph.add_edge("impact_agent", "risk_ranker_agent")
    graph.add_edge("search_agent", "risk_ranker_agent")
    graph.add_edge("test_discovery_agent", "risk_ranker_agent")

    graph.add_edge("risk_ranker_agent", "context_controller_agent")
    graph.add_edge("context_controller_agent", END)

    logger.info("Context subgraph assembled")
    return graph


# Compiled once; the parent graph embeds this instance as the "context" node.
context_graph = build_context_graph().compile()
