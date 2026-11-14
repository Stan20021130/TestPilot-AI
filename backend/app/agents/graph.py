"""TestPilot AI — LangGraph Multi-Agent Pipeline Graph.

The parent graph is deliberately thin; the two expensive phases are subgraphs:

    planner → context → (verify | review | END) → review → documentation

``context`` (diff, dependency, three parallel collectors, risk ranker, context
controller) produces a ``generation_plan``. ``verify`` is one controller
(observe → decide → act → settle) whose tools are generate/execute/diagnose/
repair/mutate; it consumes the plan and produces a ``verification`` outcome.
The parent only routes between phases; it no longer knows how either phase
works internally.

The three collectors run in the same superstep, so the channels they share
(``warnings``, ``decisions``, ``completed_agents``) carry append-only reducers
defined in :mod:`app.agents.state` — without them LangGraph rejects concurrent
writes.

Routing gates live in :mod:`app.agents.routing` and are re-exported here for
backward compatibility.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.context_graph import build_context_graph
from app.agents.node_guard import guarded
from app.agents.planner_agent import documentation_agent_node, planner_agent_node
from app.agents.review_agent import review_agent_node
from app.agents.routing import should_continue_after_diff, should_start_verify
from app.agents.state import AgentState
from app.agents.test_generator_agent import test_generator_agent_node
from app.agents.verify_graph import build_verify_graph
from app.core.logging import get_logger

logger = get_logger(__name__)

# Re-exported for callers and tests that import gates from this module.
__all__ = [
    "build_pr_analysis_graph",
    "get_compiled_graph",
    "pr_analysis_graph",
    "should_continue_after_diff",
    "should_start_verify",
]


def build_pr_analysis_graph() -> StateGraph:
    """Assemble the PR analysis pipeline.

    Returns:
        An uncompiled ``StateGraph``; the module-level ``pr_analysis_graph`` is
        the compiled instance used by the Celery task.
    """
    graph = StateGraph(AgentState)

    # --- Phase nodes (wrapped so one crash cannot abort the run) ---
    graph.add_node("planner_agent", guarded("planner_agent", planner_agent_node))
    graph.add_node("context", build_context_graph().compile())
    # Produce is its own phase now: the verify loop never generates on entry.
    graph.add_node("produce", guarded("produce", test_generator_agent_node))
    graph.add_node("verify", build_verify_graph().compile())
    graph.add_node("review_agent", guarded("review_agent", review_agent_node))
    graph.add_node(
        "documentation_agent",
        guarded("documentation_agent", documentation_agent_node),
    )

    # --- Phase transitions ---
    graph.add_edge(START, "planner_agent")
    graph.add_edge("planner_agent", "context")
    graph.add_edge("context", "produce")

    # The context subgraph ends with a generation_plan; produce turns it into
    # tests, then this gate decides whether verify has anything to do. A hard
    # stop (missing inputs) ends the run.
    graph.add_conditional_edges(
        "produce",
        should_start_verify,
        {
            "verify": "verify",
            "review_agent": "review_agent",
            END: END,
        },
    )

    graph.add_edge("verify", "review_agent")
    graph.add_edge("review_agent", "documentation_agent")
    graph.add_edge("documentation_agent", END)

    logger.info("PR analysis LangGraph pipeline assembled")
    return graph


# Compiled once at import time without a checkpointer. Kept as the default so
# unit tests and ad-hoc invocations do not write checkpoint rows.
pr_analysis_graph = build_pr_analysis_graph().compile()


@lru_cache(maxsize=1)
def get_compiled_graph() -> Any:
    """Return the graph compiled with the configured checkpointer.

    The Celery task uses this entry point. When checkpointing is disabled or
    unavailable, the plain compiled graph is returned and runs behave exactly
    as before (no resume).
    """
    from app.agents.checkpointing import get_checkpointer

    checkpointer = get_checkpointer()
    if checkpointer is None:
        return pr_analysis_graph
    return build_pr_analysis_graph().compile(checkpointer=checkpointer)
