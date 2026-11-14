"""TestPilot AI — Risk Ranker Agent.

Fusion node: it runs after the three context collectors and turns their outputs
into a single ranked answer to "where should tests be written, and why".

Before this node existed, Impact produced a file-set, Discovery produced another
file-set, and the multiplication between them was left to the generator's prompt.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.state import AgentState, decision
from app.core.logging import get_logger
from app.services.risk_ranker import rank_test_targets
from app.services.test_history import load_test_history_safe

logger = get_logger(__name__)


def risk_ranker_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: rank test targets from impact, coverage and history.

    Args:
        state: Current AgentState with impact/discovery outputs.

    Returns:
        Partial state with ``test_targets`` and ``test_history``.
    """
    start_time = time.monotonic()
    logger.info("Risk ranker agent started", pr_id=state.get("pr_id"))

    warnings: list[str] = []

    try:
        history = state.get("test_history")
        if history is None:
            history = load_test_history_safe(state.get("repository_id", ""))
            if not history:
                warnings.append(
                    "risk_ranker_agent: no test history available — ranking uses static signals only"
                )

        targets = rank_test_targets(
            changed_nodes=state.get("changed_nodes", []),
            impact_details=state.get("impact_details", []),
            coverage_map=state.get("test_coverage_map", {}),
            uncovered=state.get("uncovered_modules", []),
            history=history,
            contracts=state.get("change_contracts", []),
        )

        if not targets:
            warnings.append("risk_ranker_agent: no test targets resolved for this change")

        duration = time.monotonic() - start_time
        logger.info(
            "Risk ranker agent completed",
            targets=len(targets),
            top_score=targets[0]["score"] if targets else 0,
            top_priority=targets[0]["priority"] if targets else "none",
            duration_ms=int(duration * 1000),
        )

        return {
            "test_targets": targets,
            "test_history": history or {},
            "warnings": warnings,
            "completed_agents": ["risk_ranker_agent"],
            "decisions": [
                decision(
                    "risk_ranker_agent",
                    "rank" if targets else "no_targets",
                    f"{len(targets)} test target(s) ranked"
                    + (f"; top={targets[0]['file']} ({targets[0]['priority']})" if targets else ""),
                    inputs={
                        "targets": len(targets),
                        "top_priority": targets[0]["priority"] if targets else None,
                    },
                )
            ],
        }

    except Exception as e:
        logger.exception("Risk ranker agent failed", error=str(e))
        return {
            "errors": [f"risk_ranker_agent: {e}"],
            "test_targets": [],
            "test_history": {},
        }
