"""TestPilot AI — Context retrieval primitives.

Shared by the Search Agent (first pass, symbol-based) and the Context Controller
(second pass, impact-aware), so both passes use identical filtering, scoring and
de-duplication semantics.

Two rules matter and are enforced here:

1. a vector store needs a *vector* — text is always embedded first;
2. filters must be ``models.Filter`` objects; qdrant-client forbids extra fields
   on the filter model, so a plain dict is rejected before the request is sent.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import models

# Content shorter than this is almost certainly an index label
# ("function foo in app/x.py") rather than usable code.
MIN_USEFUL_CONTENT_CHARS = 40


def build_symbol_query(node: dict[str, Any]) -> str:
    """Build one semantic query for a changed symbol."""
    file_path = node.get("file_path", "")
    language = node.get("language", "")
    return f"{node.get('type', 'symbol')} {node.get('name', '')} in {file_path} language:{language}"


def build_usage_query(path: str) -> str:
    """Build an impact-aware query: how is this module used elsewhere?"""
    stem = str(path).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return f"usage of {stem} imported from {path}"


def repository_filter(repository_id: str) -> models.Filter:
    """Payload filter scoped to one repository."""
    return models.Filter(
        must=[
            models.FieldCondition(
                key="repository_id",
                match=models.MatchValue(value=repository_id),
            )
        ]
    )


def vector_search(
    qdrant: Any,
    collection_name: str,
    vector: list[float],
    repository_id: str,
    limit: int,
) -> list[Any]:
    """Run a vector search across qdrant-client API generations.

    ``query_points`` is the current API; ``search`` is the pre-1.10 form kept as
    a fallback for deployments pinned to an older client.
    """
    query_filter = repository_filter(repository_id)

    if hasattr(qdrant, "query_points"):
        response = qdrant.query_points(
            collection_name=collection_name,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        points = getattr(response, "points", response)
        return list(points or [])

    return list(
        qdrant.search(
            collection_name=collection_name,
            query_vector=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        or []
    )


def normalize_hit(hit: Any) -> dict[str, Any] | None:
    """Convert a scored point into the state snippet shape; None when empty."""
    payload = getattr(hit, "payload", None)
    if payload is None and isinstance(hit, dict):
        payload = hit.get("payload")
    payload = payload or {}

    content = str(payload.get("content", "") or "").strip()
    if not content:
        return None

    score = getattr(hit, "score", None)
    if score is None and isinstance(hit, dict):
        score = hit.get("score")

    return {
        "score": float(score) if score is not None else 0.0,
        "content": content,
        "file_path": str(payload.get("file_path", "") or ""),
        "language": payload.get("language", ""),
        "function_name": payload.get("function_name"),
        "symbol": payload.get("class_name") or payload.get("function_name"),
        "content_type": payload.get("type", "unknown"),
    }


def rank_and_dedupe(
    items: list[dict[str, Any]],
    *,
    limit: int,
    min_content_chars: int = MIN_USEFUL_CONTENT_CHARS,
) -> list[dict[str, Any]]:
    """De-duplicate by ``(file_path, content prefix)`` and rank by score."""
    deduped: dict[tuple[str, str], dict[str, Any]] = {}

    for item in items:
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        key = (str(item.get("file_path", "")), content[:160])
        existing = deduped.get(key)
        if existing is None or float(item.get("score", 0.0)) > float(existing.get("score", 0.0)):
            deduped[key] = item

    ranked = sorted(deduped.values(), key=lambda item: float(item.get("score", 0.0)), reverse=True)
    useful = [item for item in ranked if len(str(item.get("content", ""))) >= min_content_chars]
    return (useful or ranked)[:limit]
