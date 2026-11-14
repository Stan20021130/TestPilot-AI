"""TestPilot AI — Search Agent.

Retrieves relevant code context for the changed symbols from the Qdrant
``repository_chunks`` collection.

The deliverable is what the Test Generator reads, so the pipeline here is
strict:

1. queries are embedded with the same embedding service used at index time
   (a text query is never handed to a vector store directly);
2. hits are de-duplicated and globally ranked by score — not left in
   changed-node order;
3. every degradation (zero hits, failed query) is recorded in ``warnings`` and
   ``search_metrics`` instead of being silently swallowed.

Retrieval primitives live in :mod:`app.services.context_retrieval` so the Context
Controller can run a second, impact-aware pass with identical semantics.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.state import AgentState
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.context_retrieval import (
    build_symbol_query,
    normalize_hit,
    rank_and_dedupe,
    vector_search,
)
from app.services.embedding_service import get_embedding_service
from app.utils.qdrant_client import get_qdrant_client

logger = get_logger(__name__)

MAX_QUERIES = 10
HITS_PER_QUERY = 3
MAX_CONTEXT_ITEMS = 12


def search_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: retrieve ranked, de-duplicated code context.

    Args:
        state: Current AgentState with changed_nodes and repository_id.

    Returns:
        Partial state with retrieved_context, search_metrics and warnings.
    """
    start_time = time.monotonic()
    logger.info("Search agent started", pr_id=state.get("pr_id"))

    settings = get_settings()
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {
        "queries": 0,
        "failed_queries": 0,
        "raw_hits": 0,
        "unique_hits": 0,
        "kept": 0,
        "last_error": None,
        "duration_ms": 0,
    }

    try:
        changed_nodes = state.get("changed_nodes", [])
        repository_id = state.get("repository_id", "")

        if not changed_nodes:
            metrics["duration_ms"] = int((time.monotonic() - start_time) * 1000)
            warnings.append("search_agent: no changed symbols to search for")
            return {
                "retrieved_context": [],
                "search_metrics": metrics,
                "warnings": warnings,
                "completed_agents": ["search_agent"],
            }

        qdrant = get_qdrant_client()
        embedding_service = get_embedding_service()
        collection = settings.qdrant_collection_repository_chunks

        queries = [build_symbol_query(node) for node in changed_nodes[:MAX_QUERIES]]
        metrics["queries"] = len(queries)

        for query in queries:
            try:
                vector = embedding_service.generate_embedding(query)
                hits = vector_search(
                    qdrant=qdrant,
                    collection_name=collection,
                    vector=vector,
                    repository_id=repository_id,
                    limit=HITS_PER_QUERY,
                )
            except Exception as search_err:  # noqa: BLE001 - one bad query must not kill the node
                metrics["failed_queries"] += 1
                metrics["last_error"] = str(search_err)
                logger.warning("Qdrant search failed for query", query=query, error=str(search_err))
                continue

            metrics["raw_hits"] += len(hits)
            for hit in hits:
                item = normalize_hit(hit)
                if item is not None:
                    items.append(item)

        selected = rank_and_dedupe(items, limit=MAX_CONTEXT_ITEMS)

        metrics["unique_hits"] = len({(item["file_path"], item["content"][:160]) for item in items})
        metrics["kept"] = len(selected)
        metrics["duration_ms"] = int((time.monotonic() - start_time) * 1000)

        if metrics["failed_queries"] and metrics["failed_queries"] == metrics["queries"]:
            warnings.append(
                "search_agent: every vector search failed "
                f"({metrics['last_error']}) — generation ran without code context"
            )
        elif metrics["failed_queries"]:
            warnings.append(
                f"search_agent: {metrics['failed_queries']}/{metrics['queries']} queries failed"
            )

        if not selected:
            warnings.append(
                "search_agent: 0 code snippets retrieved — generated tests rely on "
                "changed symbols only (index may be missing or stale)"
            )

        logger.info(
            "Search agent completed",
            queries=metrics["queries"],
            raw_hits=metrics["raw_hits"],
            kept=metrics["kept"],
            failed=metrics["failed_queries"],
            duration_ms=metrics["duration_ms"],
        )

        return {
            "retrieved_context": selected,
            "search_metrics": metrics,
            "warnings": warnings,
            "completed_agents": ["search_agent"],
        }

    except Exception as e:
        logger.exception("Search agent failed", error=str(e))
        metrics["duration_ms"] = int((time.monotonic() - start_time) * 1000)
        return {
            "errors": [f"search_agent: {e}"],
            "retrieved_context": [],
            "search_metrics": metrics,
        }
