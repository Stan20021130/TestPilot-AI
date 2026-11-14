"""TestPilot AI — Context Controller Agent.

The controller owns two things the retrieval pipeline previously lacked:

1. **Sufficiency assessment** — is there enough evidence (contracts, real code,
   impact, coverage) to write a meaningful test at all?
2. **A second retrieval pass** — the Search Agent runs in parallel with Impact, so
   it cannot query "how are the affected modules used". The controller runs after
   fan-in, where that information exists, and retries with impact-aware queries
   before generation.

It also produces the **stop right**: when contracts and code are both missing, the
mode is ``insufficient`` and the generator declines to emit tests instead of
producing confident-looking stubs.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.state import AgentState, decision
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.context_retrieval import (
    build_usage_query,
    normalize_hit,
    rank_and_dedupe,
    vector_search,
)
from app.services.embedding_service import get_embedding_service
from app.utils.qdrant_client import get_qdrant_client

logger = get_logger(__name__)

# A snippet shorter than this is a label, not code worth asserting against.
CODE_MIN_CHARS = 200
MAX_SUPPLEMENTARY_QUERIES = 6
HITS_PER_QUERY = 3
MAX_CONTEXT_ITEMS = 12

WEIGHTS: dict[str, int] = {
    "contracts": 30,
    "code_context": 35,
    "impact": 20,
    "test_coverage": 15,
}


def assess_context(
    state: AgentState,
    context: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
) -> dict[str, bool]:
    """Evaluate the four evidence checks that gate generation."""
    code_items = sum(
        1 for item in context if len(str(item.get("content", "")).strip()) >= CODE_MIN_CHARS
    )
    total_tests = int((state.get("test_config") or {}).get("total_test_files", 0) or 0)

    return {
        "contracts": bool(contracts),
        "code_context": code_items > 0,
        "impact": state.get("impact_level") not in (None, "", "unknown"),
        # A repository with no tests at all cannot have a coverage map; that is
        # "nothing to compare against", not "context missing".
        "test_coverage": bool(state.get("test_coverage_map")) or total_tests == 0,
    }


def context_score(checks: dict[str, bool]) -> int:
    """Weighted 0-100 context health score."""
    return sum(weight for key, weight in WEIGHTS.items() if checks.get(key))


def context_mode(checks: dict[str, bool]) -> str:
    """Map checks to full / degraded / insufficient."""
    if not checks.get("contracts") and not checks.get("code_context"):
        return "insufficient"
    if all(checks.values()):
        return "full"
    return "degraded"


def _supplementary_queries(state: AgentState) -> list[str]:
    """Impact-aware queries: how are the affected modules used elsewhere?"""
    queries: list[str] = []

    for path in (state.get("affected_modules") or [])[:4]:
        queries.append(build_usage_query(path))

    for changed_file in (state.get("changed_files") or [])[:3]:
        path = str(changed_file.get("path", ""))
        if path:
            queries.append(build_usage_query(path))

    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        unique.append(query)
    return unique[:MAX_SUPPLEMENTARY_QUERIES]


def _supplementary_retrieval(
    state: AgentState,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run the impact-aware retrieval pass. Returns ``(items, error)``."""
    queries = _supplementary_queries(state)
    if not queries:
        return [], None

    try:
        settings = get_settings()
        qdrant = get_qdrant_client()
        embedding_service = get_embedding_service()
        collection = settings.qdrant_collection_repository_chunks
        repository_id = state.get("repository_id", "")

        items: list[dict[str, Any]] = []
        for query in queries:
            vector = embedding_service.generate_embedding(query)
            hits = vector_search(
                qdrant=qdrant,
                collection_name=collection,
                vector=vector,
                repository_id=repository_id,
                limit=HITS_PER_QUERY,
            )
            for hit in hits:
                item = normalize_hit(hit)
                if item is not None:
                    items.append(item)
        return items, None
    except Exception as exc:  # noqa: BLE001 - a failed retry must not fail the PR
        logger.warning("Supplementary retrieval failed", error=str(exc))
        return [], str(exc)


def context_controller_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: assess context sufficiency and retry if it is thin.

    Args:
        state: Current AgentState with all three collectors' outputs.

    Returns:
        Partial state with refreshed ``retrieved_context`` and ``context_health``.
    """
    start_time = time.monotonic()
    logger.info("Context controller started", pr_id=state.get("pr_id"))

    warnings: list[str] = []

    try:
        contracts = list(state.get("change_contracts", []))
        context = list(state.get("retrieved_context", []))
        attempts = 0

        checks = assess_context(state, context, contracts)

        if not checks["code_context"]:
            extra, error = _supplementary_retrieval(state)
            if error:
                warnings.append(
                    f"context_controller_agent: impact-aware retrieval failed ({error})"
                )
            if extra:
                context = rank_and_dedupe(context + extra, limit=MAX_CONTEXT_ITEMS)
                attempts = 1
                checks = assess_context(state, context, contracts)
                warnings.append(
                    "context_controller_agent: code context was thin; ran an impact-aware "
                    f"second retrieval pass (+{len(extra)} snippet(s))"
                )

        mode = context_mode(checks)
        score = context_score(checks)

        if mode == "insufficient":
            warnings.append(
                "context_controller_agent: insufficient context — no symbol contracts and no "
                "source snippets; test generation will be skipped rather than guessed"
            )

        health = {
            "mode": mode,
            "score": score,
            "checks": checks,
            "retrieval_attempts": attempts,
            "context_items": len(context),
        }

        decline_reason: str | None = None
        if not state.get("changed_nodes"):
            decline_reason = "no_changed_symbols"
        elif mode == "insufficient":
            decline_reason = "context_insufficient"

        plan = {
            "declined": decline_reason is not None,
            "decline_reason": decline_reason,
            "context_mode": mode,
            "context_score": score,
            "evidence": checks,
            "targets": list(state.get("test_targets", [])),
            "changed_symbols": len(state.get("changed_nodes", [])),
            "affected_files": len(state.get("affected_modules", [])),
        }

        duration = time.monotonic() - start_time
        logger.info(
            "Context controller completed",
            mode=mode,
            score=score,
            checks=checks,
            retried=attempts,
            items=len(context),
            duration_ms=int(duration * 1000),
        )

        return {
            "retrieved_context": context,
            "context_health": health,
            "generation_plan": plan,
            "warnings": warnings,
            "completed_agents": ["context_controller_agent"],
            "decisions": [
                decision(
                    "context_controller_agent",
                    mode,
                    f"context score {score}/100 "
                    f"(contracts={checks['contracts']}, code={checks['code_context']}, "
                    f"impact={checks['impact']}, coverage={checks['test_coverage']}); "
                    f"retrieval passes={attempts}",
                    inputs={"score": score, **checks},
                )
            ],
        }

    except Exception as e:
        logger.exception("Context controller failed", error=str(e))
        return {
            "errors": [f"context_controller_agent: {e}"],
            "context_health": {
                "mode": "degraded",
                "score": 0,
                "checks": {},
                "retrieval_attempts": 0,
                "context_items": len(state.get("retrieved_context", [])),
            },
        }
