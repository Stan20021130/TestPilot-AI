"""TestPilot AI — WebSocket Endpoints for Real-time Streaming.

Both endpoints require a valid access token (``?token=...``) and check that the
caller owns the PR / repository being subscribed to. Without that check any
client could stream another tenant's analysis progress.
"""

from __future__ import annotations

import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import verify_access_token
from app.database.session import AsyncSessionLocal
from app.models.pull_request import PullRequest
from app.models.repository import Repository

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore[assignment]

logger = get_logger(__name__)
ws_router = APIRouter()

AUTH_CLOSE_CODE = 1008  # policy violation


async def _user_owns_pr(user_id: str | None, pr_id: str) -> bool:
    """True when the PR exists and belongs to the user's repository."""
    if not user_id:
        return False
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PullRequest.id)
                .join(Repository, Repository.id == PullRequest.repository_id)
                .where(PullRequest.id == pr_id, Repository.owner_id == user_id)
            )
            return result.scalar_one_or_none() is not None
    except Exception as exc:  # noqa: BLE001 - auth failures must deny, not crash
        logger.warning("WebSocket PR authorization failed", pr_id=pr_id, error=str(exc))
        return False


async def _user_owns_repo(user_id: str | None, repo_id: str) -> bool:
    """True when the repository belongs to the user."""
    if not user_id:
        return False
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Repository.id).where(
                    Repository.id == repo_id, Repository.owner_id == user_id
                )
            )
            return result.scalar_one_or_none() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("WebSocket repository authorization failed", repo_id=repo_id, error=str(exc))
        return False


async def _authorize(websocket: WebSocket, token: str | None) -> str | None:
    """Validate the token and close the connection when it is unusable."""
    payload = verify_access_token(token) if token else None
    user_id = (payload or {}).get("sub")
    if not user_id:
        logger.info("WebSocket rejected: missing or invalid token")
        await websocket.close(code=AUTH_CLOSE_CODE)
        return None
    return str(user_id)


@ws_router.websocket("/pr/{pr_id}")
async def websocket_pr_updates(
    websocket: WebSocket, pr_id: str, token: str | None = None
) -> None:
    """WebSocket endpoint for real-time PR analysis progress updates.

    Streams:
    - Agent start/completion events
    - Risk score updates
    - Test generation progress
    - Final review

    Usage:
        ws://localhost:8000/ws/pr/{pr_id}?token=<access_token>

    Message format:
        {"event": "agent_started", "agent": "diff_agent", "timestamp": "..."}
        {"event": "agent_completed", "agent": "diff_agent", "data": {...}}
        {"event": "pipeline_completed", "risk_level": "high", "score": 7.2}
        {"event": "error", "message": "..."}
    """
    user_id = await _authorize(websocket, token)
    if user_id is None:
        return
    if not await _user_owns_pr(user_id, pr_id):
        logger.info("WebSocket rejected: PR not owned by caller", pr_id=pr_id)
        await websocket.close(code=AUTH_CLOSE_CODE)
        return

    await websocket.accept()
    logger.info("WebSocket connection opened", pr_id=pr_id)

    try:
        settings = get_settings()

        redis = aioredis.from_url(settings.redis_url)
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"pr_updates:{pr_id}")

        # Send initial connection confirmation
        await websocket.send_json(
            {
                "event": "connected",
                "pr_id": pr_id,
                "message": "Subscribed to PR analysis updates",
            }
        )

        # Listen for updates from Redis pub/sub
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    await websocket.send_json(data)

                    # Close when pipeline completes
                    if data.get("event") in ("pipeline_completed", "pipeline_failed"):
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

        await pubsub.unsubscribe(f"pr_updates:{pr_id}")
        await redis.close()

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected", pr_id=pr_id)
    except Exception as e:
        logger.exception("WebSocket error", pr_id=pr_id, error=str(e))
        with contextlib.suppress(Exception):
            await websocket.send_json({"event": "error", "message": str(e)})
    finally:
        logger.info("WebSocket connection closed", pr_id=pr_id)


@ws_router.websocket("/indexing/{repo_id}")
async def websocket_indexing_progress(
    websocket: WebSocket, repo_id: str, token: str | None = None
) -> None:
    """WebSocket endpoint for repository indexing progress.

    Streams:
    - Files parsed count
    - Current stage (cloning, parsing, embedding)
    - Completion percentage
    - Final indexed stats
    """
    user_id = await _authorize(websocket, token)
    if user_id is None:
        return
    if not await _user_owns_repo(user_id, repo_id):
        logger.info("Indexing WebSocket rejected: repository not owned by caller", repo_id=repo_id)
        await websocket.close(code=AUTH_CLOSE_CODE)
        return

    await websocket.accept()
    logger.info("Indexing WebSocket opened", repo_id=repo_id)

    try:
        settings = get_settings()

        redis = aioredis.from_url(settings.redis_url)
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"indexing:{repo_id}")

        await websocket.send_json(
            {
                "event": "connected",
                "repo_id": repo_id,
            }
        )

        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    await websocket.send_json(data)
                    if data.get("event") in ("indexing_completed", "indexing_failed"):
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

        await pubsub.unsubscribe(f"indexing:{repo_id}")
        await redis.close()

    except WebSocketDisconnect:
        logger.info("Indexing WebSocket client disconnected", repo_id=repo_id)
    except Exception as e:
        logger.exception("Indexing WebSocket error", repo_id=repo_id, error=str(e))
