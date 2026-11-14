"""Verify phase entry point.

There is one implementation: the contracts-first engine in :mod:`app.verify`.
The parent graph embeds it as a single ``verify`` node; the engine owns its
own controller, tools and budget.
"""

from __future__ import annotations

from langgraph.graph import StateGraph


def build_verify_graph() -> StateGraph:
    """Return the verify subgraph (uncompiled)."""
    from app.verify.adapter import build_next_verify_graph

    return build_next_verify_graph()


# Compiled once; the parent graph embeds this instance as the "verify" node.
verify_graph = build_verify_graph().compile()
