"""Node guard shared by the main graph and the verify subgraph.

Agents catch their own domain errors; this wrapper is the last line of defence
against programming errors. A failure is recorded on the ``errors`` channel and
the graph continues with the state it already has, instead of failing the Celery
task and re-running every expensive stage on retry.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from app.agents.state import AgentState
from app.core.logging import get_logger

logger = get_logger(__name__)


def guarded(
    name: str,
    node: Callable[[AgentState], dict[str, Any]],
) -> Callable[[AgentState], dict[str, Any]]:
    """Wrap a node so one unhandled exception cannot abort the whole run.

    The node is *not* marked completed when it raises: ``completed_agents``
    must stay an honest record of what actually produced output.
    """

    @wraps(node)
    def run(state: AgentState) -> dict[str, Any]:
        try:
            return node(state)
        except Exception as exc:  # noqa: BLE001 - pipeline resilience boundary
            logger.exception(
                "Agent node raised an unhandled error",
                agent=name,
                error=str(exc),
            )
            return {"errors": [f"{name}: unhandled {type(exc).__name__}: {exc}"]}

    return run
