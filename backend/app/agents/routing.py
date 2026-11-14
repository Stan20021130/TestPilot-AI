"""Routing gates for the main PR graph.

Only two decisions remain outside the verify engine:

- after diff: continue to dependency analysis, skip to review, or stop;
- after produce: enter verify or skip it when there is nothing to run.

The verify engine owns all decisions inside the loop.
"""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END

from app.agents.state import AgentState
from app.core.logging import get_logger

logger = get_logger(__name__)


def should_continue_after_diff(
    state: AgentState,
) -> Literal["dependency_agent", "review_agent", END]:  # type: ignore[valid-type]
    """Route after diff: stop early if no supported files changed."""
    if state.get("should_stop"):
        logger.info("Pipeline stopping early after diff agent")
        return END

    if not state.get("changed_nodes"):
        logger.info("No code nodes changed, routing to review agent directly")
        return "review_agent"

    return "dependency_agent"


def should_start_verify(state: AgentState) -> Literal["verify", "review_agent", END]:  # type: ignore[valid-type]
    """Route after produce: verify only when there is something to run."""
    if state.get("should_stop"):
        logger.info("Pipeline stopping after context: missing required inputs")
        return END

    plan = state.get("generation_plan") or {}
    generated = state.get("generated_tests") or []
    affected = state.get("affected_tests") or []
    if not generated and not affected:
        logger.info(
            "No generated or covering tests after produce, skipping verify",
            reason=plan.get("decline_reason"),
        )
        return "review_agent"

    return "verify"
