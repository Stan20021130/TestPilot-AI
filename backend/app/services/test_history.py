"""TestPilot AI — Test execution history.

Pulls recent per-test outcomes from ``test_runs`` / ``test_results`` so the risk
ranker can weigh "this area broke last week" and "this test is flaky" alongside
static structure. History is best-effort: an empty result simply means the
ranking falls back to static signals.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.database.session import get_session
from app.models.test_result import TestResult
from app.models.test_run import TestRun

logger = get_logger(__name__)

MAX_RUNS = 20
SLOW_TEST_SECONDS = 20.0
FAILED_STATUSES = {"failed", "error"}


def normalize_path(path: str | None) -> str:
    """Normalize a repo-relative path for cross-source joins."""
    if not path:
        return ""
    normalized = str(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def aggregate_history(
    rows: Any,
    slow_threshold: float = SLOW_TEST_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Aggregate raw ``(test_file, test_name, status, duration)`` rows per file.

    Args:
        rows: Iterable of result rows.
        slow_threshold: Seconds above which a test counts as slow.

    Returns:
        Mapping of normalized test file -> ``{runs, failures, flaky,
        avg_duration, slow_tests}``.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "failures": 0})
    statuses: dict[tuple[str, str], set[str]] = defaultdict(set)
    durations: dict[str, list[float]] = defaultdict(list)

    for test_file, test_name, status, duration in rows:
        key = normalize_path(test_file)
        if not key:
            continue
        counts[key]["runs"] += 1
        if str(status) in FAILED_STATUSES:
            counts[key]["failures"] += 1
        statuses[(key, str(test_name))].add(str(status))
        if duration is not None:
            durations[key].append(float(duration))

    history: dict[str, dict[str, Any]] = {}
    for key, counters in counts.items():
        flaky = sum(
            1
            for (file_key, _name), seen in statuses.items()
            if file_key == key and "failed" in seen and "passed" in seen
        )
        file_durations = durations.get(key, [])
        history[key] = {
            "runs": counters["runs"],
            "failures": counters["failures"],
            "flaky": flaky,
            "avg_duration": round(sum(file_durations) / len(file_durations), 3)
            if file_durations
            else 0.0,
            "slow_tests": sum(1 for value in file_durations if value >= slow_threshold),
        }
    return history


async def load_test_history(
    repository_id: str,
    max_runs: int = MAX_RUNS,
) -> dict[str, dict[str, Any]]:
    """Load and aggregate recent test history for a repository."""
    async with get_session() as db:
        run_ids = (
            (
                await db.execute(
                    select(TestRun.id)
                    .where(TestRun.repository_id == repository_id)
                    .order_by(TestRun.created_at.desc())
                    .limit(max_runs)
                )
            )
            .scalars()
            .all()
        )
        if not run_ids:
            return {}

        rows = (
            await db.execute(
                select(
                    TestResult.test_file,
                    TestResult.test_name,
                    TestResult.status,
                    TestResult.duration_seconds,
                ).where(TestResult.test_run_id.in_(run_ids))
            )
        ).all()

    history = aggregate_history(rows)
    logger.info(
        "Test history loaded",
        repository_id=repository_id,
        runs=len(run_ids),
        test_files=len(history),
    )
    return history


def load_test_history_safe(repository_id: str) -> dict[str, dict[str, Any]]:
    """Blocking wrapper used from sync LangGraph nodes; never raises."""
    import asyncio

    try:
        return asyncio.run(load_test_history(repository_id))
    except Exception as exc:  # noqa: BLE001 - history is optional context
        logger.warning("Test history unavailable", repository_id=repository_id, error=str(exc))
        return {}
