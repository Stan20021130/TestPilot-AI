"""Checkpoint, resume and decision-audit tests.

The graph guard turns node exceptions into ``errors`` records, so a crashed
*node* does not interrupt a run. What does interrupt a run is the process or
worker dying between supersteps. That is simulated here with a static
``interrupt_after`` breakpoint, which leaves exactly the checkpoint a killed
worker would leave: state up to the last completed node and the next node
pending.

The resume itself is exercised against ``InMemorySaver``; the Postgres backend
shares the same ``BaseCheckpointSaver`` contract, so the semantics under test
are backend-independent.
"""

from __future__ import annotations

import contextlib
import os
import sys
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents import checkpointing
from app.agents.graph import build_pr_analysis_graph
from app.agents.runner import (
    invoke_pipeline,
    is_resumable,
)
from app.agents.state import MAX_DECISIONS, append_decisions, decision
from tests.orchestration_support import initial_state, stub_topology

pytestmark = pytest.mark.unit

# ==============================================================================
# Decision audit
# ==============================================================================


def test_decision_records_carry_evidence_and_timestamp() -> None:
    record = decision(
        "test_generator_agent",
        "decline",
        "context mode is 'insufficient'",
        inputs={"score": 20},
        attempt=2,
    )

    assert record["agent"] == "test_generator_agent"
    assert record["choice"] == "decline"
    assert record["inputs"] == {"score": 20}
    assert record["attempt"] == 2
    assert record["at"].endswith("+00:00")


def test_append_decisions_is_append_only_and_bounded() -> None:
    existing = [decision("a", "one", "first")]
    update = [decision("b", "two", "second")]

    merged = append_decisions(existing, update)
    assert [item["agent"] for item in merged] == ["a", "b"]

    many = [decision("a", "step", str(index)) for index in range(MAX_DECISIONS + 5)]
    assert len(append_decisions([], many)) == MAX_DECISIONS
    # The newest decisions survive, the oldest are dropped.
    assert append_decisions([], many)[-1]["reason"] == str(MAX_DECISIONS + 4)


# ==============================================================================
# Checkpointer configuration
# ==============================================================================


def test_checkpoint_thread_id_is_stable_per_revision() -> None:
    first = checkpointing.checkpoint_thread_id("pr-1", "a" * 40)
    again = checkpointing.checkpoint_thread_id("pr-1", "a" * 40)
    other_head = checkpointing.checkpoint_thread_id("pr-1", "b" * 40)
    other_pr = checkpointing.checkpoint_thread_id("pr-2", "a" * 40)

    assert first == again
    assert len({first, other_head, other_pr}) == 3


def test_to_libpq_dsn_strips_async_driver() -> None:
    dsn = checkpointing.to_libpq_dsn("postgresql+asyncpg://user:pass@localhost:5432/testpilot")
    assert dsn == "postgresql://user:pass@localhost:5432/testpilot"
    assert checkpointing.to_libpq_dsn("postgresql://x/y") == "postgresql://x/y"


def test_postgres_backend_falls_back_when_driver_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker must not fail to start because the checkpoint extra is absent."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setattr(
        checkpointing,
        "get_settings",
        lambda: SimpleNamespace(
            checkpoint_backend="postgres",
            checkpoint_dsn=None,
            database_url="postgresql+asyncpg://user:pass@localhost/db",
        ),
    )
    checkpointing.reset_checkpointer()
    try:
        assert isinstance(checkpointing.get_checkpointer(), InMemorySaver)
    finally:
        checkpointing.reset_checkpointer()


def test_checkpointer_factory_supports_memory_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checkpointing,
        "get_settings",
        lambda: SimpleNamespace(
            checkpoint_backend="memory",
            checkpoint_dsn=None,
            database_url="postgresql+asyncpg://x/y",
        ),
    )
    checkpointing.reset_checkpointer()
    try:
        saver = checkpointing.get_checkpointer()
        assert isinstance(saver, InMemorySaver)
    finally:
        checkpointing.reset_checkpointer()

    monkeypatch.setattr(
        checkpointing,
        "get_settings",
        lambda: SimpleNamespace(
            checkpoint_backend="off",
            checkpoint_dsn=None,
            database_url="postgresql+asyncpg://x/y",
        ),
    )
    checkpointing.reset_checkpointer()
    try:
        assert checkpointing.get_checkpointer() is None
    finally:
        checkpointing.reset_checkpointer()


# ==============================================================================
# Resume semantics
# ==============================================================================


def _compiled(saver: InMemorySaver, interrupt_after: list[str] | None = None):
    return build_pr_analysis_graph().compile(
        checkpointer=saver,
        interrupt_after=interrupt_after or [],
    )


def test_fresh_run_does_not_reuse_a_completed_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished checkpoint must not swallow a new analysis' audit trail."""
    calls: dict[str, int] = {}
    stub_topology(monkeypatch, calls=calls)

    saver = InMemorySaver()
    graph = _compiled(saver)
    thread_id = "pr-completed-test"
    config = {"configurable": {"thread_id": thread_id}}

    graph.invoke(initial_state(), config)
    assert calls["planner_agent"] == 1

    invocation = invoke_pipeline(
        graph,
        initial_state=initial_state(),
        thread_id=thread_id,
    )

    assert invocation.resumed is False
    assert invocation.thread_id != thread_id
    assert calls["planner_agent"] == 2


def test_memory_threads_can_be_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memory backend has no retention policy, so completed threads are dropped."""
    stub_topology(monkeypatch)
    saver = InMemorySaver()
    graph = _compiled(saver)
    thread_id = "pr-discard-test"

    graph.invoke(initial_state(), {"configurable": {"thread_id": thread_id}})
    assert saver.get_tuple({"configurable": {"thread_id": thread_id}}) is not None

    monkeypatch.setattr(checkpointing, "_saver", saver)
    checkpointing.discard_thread(thread_id)

    assert saver.get_tuple({"configurable": {"thread_id": thread_id}}) is None


@pytest.mark.integration
def test_postgres_checkpoint_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in: verify the real PostgresSaver wiring against a live database.

    Run with ``CHECKPOINT_TEST_DSN=postgresql://... pytest -m integration``.
    """
    pytest.importorskip("psycopg")
    pytest.importorskip("langgraph.checkpoint.postgres")
    dsn = os.environ.get("CHECKPOINT_TEST_DSN")
    if not dsn:
        pytest.skip("CHECKPOINT_TEST_DSN is not set")

    monkeypatch.setattr(
        checkpointing,
        "get_settings",
        lambda: SimpleNamespace(
            checkpoint_backend="postgres",
            checkpoint_dsn=dsn,
            database_url="",
        ),
    )
    checkpointing.reset_checkpointer()
    saver = checkpointing.get_checkpointer()
    try:
        assert type(saver).__name__ == "PostgresSaver"

        graph = _tiny_graph().compile(checkpointer=saver, interrupt_after=["a"])
        config = {"configurable": {"thread_id": "checkpoint-roundtrip-test"}}
        graph.invoke({"n": 0}, config)

        assert graph.get_state(config).next == ("b",)
        assert graph.invoke(None, config)["n"] == 11
    finally:
        with contextlib.suppress(Exception):
            saver.delete_thread("checkpoint-roundtrip-test")
        checkpointing.reset_checkpointer()


class _Counter(TypedDict, total=False):
    n: int


def _tiny_graph() -> StateGraph:
    def increment(state: _Counter) -> dict[str, int]:
        return {"n": state.get("n", 0) + 1}

    def add_ten(state: _Counter) -> dict[str, int]:
        return {"n": state.get("n", 0) + 10}

    graph: StateGraph = StateGraph(_Counter)
    graph.add_node("a", increment)
    graph.add_node("b", add_ten)
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    return graph


def test_is_resumable_requires_pending_work_and_a_live_workspace(tmp_path) -> None:
    snapshot = SimpleNamespace(values={"workspace_path": str(tmp_path)}, next=("execution_agent",))
    assert is_resumable(snapshot, str(tmp_path)) is True

    # No pending nodes: the previous run ended.
    ended = SimpleNamespace(values={"workspace_path": str(tmp_path)}, next=())
    assert is_resumable(ended, str(tmp_path)) is False

    # Pending work but the worktree was released.
    missing = SimpleNamespace(
        values={"workspace_path": str(tmp_path / "gone")}, next=("execution_agent",)
    )
    assert is_resumable(missing, str(tmp_path / "gone")) is False

    # A crash inside the verify subgraph leaves parent values empty, but the
    # pending node and the deterministic worktree still make it resumable.
    nested = SimpleNamespace(values={}, next=("verify",))
    assert is_resumable(nested, str(tmp_path)) is True
    assert is_resumable(nested, None) is False

    assert is_resumable(None) is False
