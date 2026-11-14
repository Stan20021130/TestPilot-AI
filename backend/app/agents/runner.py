"""Checkpoint-aware invocation helpers for the analysis graph.

The graph itself stays free of persistence concerns: this module decides whether
a run should start fresh, resume an interrupted thread, or start a new thread
because the previous one already finished.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PipelineInvocation:
    """Result of invoking the graph, with the thread that was used."""

    final_state: dict[str, Any]
    thread_id: str
    resumed: bool


def checkpoint_snapshot(graph: Any, thread_id: str) -> Any | None:
    """Return the latest snapshot for ``thread_id``, or ``None``.

    ``None`` covers both "no checkpointer configured" and "nothing stored yet";
    both mean the caller must start from an initial state.
    """
    try:
        return graph.get_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001 - absence of a checkpointer is expected
        logger.debug("No checkpoint available", thread_id=thread_id, error=str(exc))
        return None


def is_resumable(snapshot: Any, workspace_path: str | None = None) -> bool:
    """An interrupted thread is resumable only if its worktree still exists.

    A checkpoint whose workspace was already released cannot be continued: the
    agents would read a deleted tree. Starting fresh is the safe answer.

    ``snapshot.values`` may be empty when the interruption happened *inside* a
    subgraph: LangGraph stores the subgraph's state in its own namespace and the
    parent snapshot only carries ``next == ('verify',)``. Pending work plus a
    live deterministic worktree is therefore enough to resume — requiring parent
    values would silently disable resume for the most expensive part of the run.
    """
    if snapshot is None:
        return False
    if not getattr(snapshot, "next", None):
        return False
    if not workspace_path:
        return False
    return _path_exists(workspace_path)


def _path_exists(path: str) -> bool:
    from pathlib import Path

    try:
        return Path(path).exists()
    except OSError:
        return False


def invoke_pipeline(
    graph: Any,
    *,
    initial_state: dict[str, Any] | None,
    thread_id: str,
    resume: bool = False,
) -> PipelineInvocation:
    """Invoke ``graph`` on the given thread, resuming when asked.

    Args:
        graph: Compiled LangGraph (with or without a checkpointer).
        initial_state: Required when ``resume`` is false.
        thread_id: Stable thread for the PR revision.
        resume: Continue the checkpointed thread with ``None`` input.

    Returns:
        The final state plus the thread id actually used and whether the run
        resumed. A completed thread is never reused for a fresh input, because
        append-only channels would merge the old and new audit trails.
    """
    config = {"configurable": {"thread_id": thread_id}}

    if resume:
        logger.info("Resuming interrupted analysis from checkpoint", thread_id=thread_id)
        final_state = graph.invoke(None, config)
        return PipelineInvocation(dict(final_state), thread_id, True)

    if initial_state is None:
        raise ValueError("initial_state is required when resume=False")

    snapshot = checkpoint_snapshot(graph, thread_id)
    if snapshot is not None and (
        getattr(snapshot, "values", None) or getattr(snapshot, "next", None)
    ):
        # The thread ended, or is pending after a failed resume: a fresh import
        # must not inherit its warnings/decisions or accidentally continue it,
        # so it gets its own checkpoint thread.
        thread_id = f"{thread_id}-{uuid4().hex[:6]}"
        config = {"configurable": {"thread_id": thread_id}}
        logger.info(
            "Existing checkpoint cannot be resumed; starting a new thread",
            thread_id=thread_id,
        )

    final_state = graph.invoke(initial_state, config)
    return PipelineInvocation(dict(final_state), thread_id, False)
