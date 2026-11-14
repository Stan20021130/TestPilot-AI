"""
TestPilot AI — PR Analysis Pipeline Celery Task.

This is the main entry point for the asynchronous PR analysis workflow.
When a GitHub webhook fires for a new or updated PR, this task is enqueued
and runs the full LangGraph multi-agent pipeline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from celery import Task
from sqlalchemy import select, update

from app.agents.checkpointing import checkpoint_thread_id, discard_thread
from app.agents.graph import get_compiled_graph
from app.agents.runner import checkpoint_snapshot, invoke_pipeline, is_resumable
from app.agents.state import AgentState
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import get_session
from app.models.analysis_checkpoint import AnalysisCheckpoint
from app.models.pull_request import PullRequest
from app.models.review_comment import ReviewComment
from app.services.pr_checkout import PrCheckout, ensure_pr_checkout, release_pr_checkout
from app.services.repo_lock import repository_lock
from app.services.run_workspace import stable_run_id
from app.workers.celery_app import celery_app

logger = get_logger(__name__)
settings = get_settings()


class PRPipelineTask(Task):
    """Custom Celery task with database session management.

    Inherits from Task to allow shared state across task retries.
    """

    abstract = True
    _db = None

    def on_failure(self, exc: Exception, task_id: str, args: Any, kwargs: Any, einfo: Any) -> None:
        """Handle task failure — update PR analysis status in database."""
        logger.error(
            "PR pipeline task failed",
            task_id=task_id,
            error=str(exc),
            exc_info=True,
        )
        # Update PR status to 'failed' in the database
        pr_id = kwargs.get("pr_id") or (args[0] if args else None)
        if pr_id:
            asyncio.run(_update_pr_status(pr_id, "failed", str(exc)))

    def on_success(self, retval: Any, task_id: str, args: Any, kwargs: Any) -> None:
        """Handle task success — update PR analysis status."""
        pr_id = kwargs.get("pr_id") or (args[0] if args else None)
        if pr_id:
            asyncio.run(_update_pr_status(pr_id, "completed"))


@celery_app.task(
    bind=True,
    base=PRPipelineTask,
    name="app.tasks.pr_pipeline.run_pr_analysis",
    max_retries=3,
    default_retry_delay=60,  # 1 minute before retry
    queue="pr_pipeline",
)
def run_pr_analysis(
    self: Task,
    pr_id: str,
    repository_id: str,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    installation_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the full PR analysis LangGraph pipeline.

    This task is triggered when a GitHub webhook fires for a PR event.
    It runs all 13 LangGraph agents in sequence and stores the results.

    Args:
        pr_id: Internal TestPilot PR UUID.
        repository_id: Internal repository UUID.
        repo_full_name: GitHub full repository name.
        pr_number: GitHub PR number.
        head_sha: HEAD commit SHA.
        base_sha: Base commit SHA.
        installation_id: Optional GitHub App installation ID.

    Returns:
        Dictionary with analysis results summary.
    """
    logger.info(
        "Starting PR analysis pipeline",
        task_id=self.request.id,
        pr_id=pr_id,
        repo=repo_full_name,
        pr_number=pr_number,
    )

    # Update PR status to 'running'
    asyncio.run(_update_pr_status(pr_id, "running"))

    checkout: PrCheckout | None = None
    invocation: Any | None = None
    keep_workspace = False
    repo_path = settings.repo_storage_path / repository_id
    graph = get_compiled_graph()
    thread_id = checkpoint_thread_id(pr_id, head_sha)
    run_id = stable_run_id(pr_id, head_sha)

    try:
        # A repository checkout is shared by indexing and by every PR analysis
        # of that repository; the lock keeps them from rewriting the working
        # tree under each other. Timeouts surface as a retryable failure.
        with repository_lock(
            repo_path,
            timeout=settings.repository_lock_timeout_seconds,
            owner_id=self.request.id,
        ):
            # A retry of the same (PR, head) resumes the interrupted thread when
            # its worktree survived; otherwise the run starts from scratch.
            snapshot = checkpoint_snapshot(graph, thread_id)
            snapshot_state = getattr(snapshot, "values", None) or {}
            # A crash inside the verify subgraph leaves the parent snapshot
            # empty; fall back to the deterministic worktree for this run id.
            expected_workspace = repo_path.parent / f".{repo_path.name}-pr-{run_id}"
            workspace_path = snapshot_state.get("workspace_path") or (
                str(expected_workspace) if expected_workspace.exists() else None
            )
            resumable = is_resumable(snapshot, workspace_path)
            invocation = None

            if resumable:
                assert workspace_path is not None  # narrowed by is_resumable
                inferred_mode = "head" if Path(workspace_path) == expected_workspace else "base"
                checkout = PrCheckout(
                    path=Path(workspace_path),
                    mode=snapshot_state.get("checkout_mode", inferred_mode),
                    ref=None,
                )
                try:
                    invocation = invoke_pipeline(
                        graph,
                        initial_state=None,
                        thread_id=thread_id,
                        resume=True,
                    )
                except Exception as exc:  # noqa: BLE001 - a bad checkpoint must not deadlock retries
                    logger.warning(
                        "Checkpoint resume failed; restarting the analysis from scratch",
                        thread_id=thread_id,
                        error=str(exc),
                    )
                    invocation = None

            if invocation is None:
                # Analyse the PR revision, not the default branch.
                checkout = ensure_pr_checkout(
                    repo_path=repo_path,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    token=settings.github_token,
                    run_id=run_id,
                )

                initial_state = AgentState(
                    pr_id=pr_id,
                    repository_id=repository_id,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    installation_id=installation_id,
                    run_id=run_id,
                    workspace_path=str(checkout.path),
                    checkout_mode=checkout.mode,
                    # One budget object for the whole verify loop. Routing is a
                    # pure function of it, and no loop can exceed its limits.
                    budget={
                        "max_rounds": settings.max_verify_rounds,
                        "max_generation_attempts": settings.max_generation_attempts,
                        "max_coverage_topups": settings.max_coverage_topups,
                        "max_repairs": settings.max_repair_attempts,
                        "max_mutation_attempts": settings.max_mutation_attempts,
                    },
                    errors=[],
                    warnings=list(checkout.warnings),
                    decisions=[],
                    retry_count=0,
                    should_stop=False,
                    completed_agents=[],
                )
                invocation = invoke_pipeline(
                    graph,
                    initial_state=initial_state,
                    thread_id=thread_id,
                )

            final_state = invocation.final_state

        # Store results in database
        asyncio.run(_store_analysis_results(pr_id, final_state))
        if (settings.checkpoint_backend or "").strip().lower() == "postgres":
            asyncio.run(_register_checkpoint(invocation.thread_id, pr_id))

        logger.info(
            "PR analysis pipeline completed",
            pr_id=pr_id,
            checkout_mode=checkout.mode,
            resumed=invocation.resumed,
            thread_id=invocation.thread_id,
            decisions=len(final_state.get("decisions", [])),
            verify_exit=(final_state.get("verification") or {}).get("exit_reason"),
            risk_level=final_state.get("risk_score", {}).get("level", "unknown"),
            completed_agents=final_state.get("completed_agents", []),
        )

        return {
            "pr_id": pr_id,
            "status": "completed",
            "risk_level": final_state.get("risk_score", {}).get("level"),
            "risk_score": final_state.get("risk_score", {}).get("score"),
            "completed_agents": final_state.get("completed_agents", []),
            "errors": final_state.get("errors", []),
            "decisions": final_state.get("decisions", []),
            "verification": final_state.get("verification", {}),
            "checkout_mode": checkout.mode,
            "resumed": invocation.resumed,
            "thread_id": invocation.thread_id,
        }

    except Exception as exc:
        logger.exception("PR analysis pipeline failed", pr_id=pr_id, error=str(exc))
        # Keep the worktree alive across retries so the checkpoint has a tree to
        # resume into; the final attempt (or success) releases it.
        keep_workspace = self.request.retries < self.max_retries
        try:
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        except self.MaxRetriesExceededError:
            asyncio.run(_update_pr_status(pr_id, "failed", str(exc)))
            if (settings.checkpoint_backend or "").strip().lower() == "postgres":
                asyncio.run(_register_checkpoint(thread_id, pr_id))
            return {"pr_id": pr_id, "status": "failed", "error": str(exc)}

    finally:
        if checkout is not None and not keep_workspace:
            release_pr_checkout(checkout, repo_path)
        # The memory backend has no retention policy; drop the completed thread
        # so a long-lived worker does not accumulate every analysis in RAM.
        if (
            invocation is not None
            and not keep_workspace
            and (settings.checkpoint_backend or "").strip().lower() != "postgres"
        ):
            discard_thread(invocation.thread_id)


# Alias for webhook task dispatch
analyze_pull_request = run_pr_analysis


async def _update_pr_status(
    pr_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update the PR analysis status in the database."""
    async with get_session() as db:
        stmt = (
            update(PullRequest)
            .where(PullRequest.id == pr_id)
            .values(analysis_status=status, analysis_error=error)
        )
        await db.execute(stmt)


async def _register_checkpoint(thread_id: str, pr_id: str) -> None:
    """Record a finished thread for the retention task (idempotent)."""
    async with get_session() as db:
        row = await db.get(AnalysisCheckpoint, thread_id)
        now = datetime.now(UTC)
        if row is not None:
            row.completed_at = now
        else:
            db.add(
                AnalysisCheckpoint(
                    thread_id=thread_id,
                    pull_request_id=pr_id,
                    completed_at=now,
                )
            )
        await db.flush()


async def _store_analysis_results(pr_id: str, state: dict[str, Any]) -> None:
    """Store the complete analysis results in the database."""
    async with get_session() as db:
        risk_score = state.get("risk_score", {})
        review = state.get("review", {})
        verification = state.get("verification") or {}
        plan = state.get("generation_plan") or {}
        plan_summary = {
            "declined": plan.get("declined"),
            "decline_reason": plan.get("decline_reason"),
            "context_score": plan.get("context_score"),
            "targets": len(plan.get("targets") or []),
            "changed_symbols": plan.get("changed_symbols"),
        }

        # Update PR with analysis results
        await db.execute(
            update(PullRequest)
            .where(PullRequest.id == pr_id)
            .values(
                analysis_status="completed",
                risk_level=risk_score.get("level"),
                risk_score=risk_score.get("score"),
                risk_factors=json.dumps(risk_score.get("factors", [])),
                affected_modules=json.dumps(state.get("affected_modules", [])),
                analysis_warnings=json.dumps(state.get("warnings", [])),
                analysis_decisions=json.dumps(state.get("decisions", [])),
                verify_exit_reason=verification.get("exit_reason"),
                verify_engine=verification.get("engine"),
                verify_rounds=verification.get("rounds"),
                context_mode=(state.get("context_health") or {}).get("mode")
                or plan.get("context_mode"),
                generation_plan_summary=json.dumps(plan_summary),
            )
        )

        # Store the review comment. Retries re-run the whole task, so this must
        # be an upsert: otherwise a retried analysis duplicates the review.
        if review:
            existing = (
                (
                    await db.execute(
                        select(ReviewComment).where(ReviewComment.pull_request_id == pr_id).limit(1)
                    )
                )
                .scalars()
                .first()
            )
            payload = {
                "body": review.get("full_review_body", ""),
                "risk_level": review.get("risk_level", "low"),
                "risk_score": review.get("risk_score", 0.0),
                "summary": review.get("summary"),
                "action_items": json.dumps(review.get("action_items", [])),
            }
            if existing is not None:
                for key, value in payload.items():
                    setattr(existing, key, value)
                existing.is_posted = False
            else:
                db.add(ReviewComment(pull_request_id=pr_id, is_posted=False, **payload))

        logger.info(
            "Analysis results stored",
            pr_id=pr_id,
            warnings=state.get("warnings", []),
            decisions=len(state.get("decisions", [])),
            impact_level=state.get("impact_level"),
            impact_radius=state.get("impact_radius"),
        )
