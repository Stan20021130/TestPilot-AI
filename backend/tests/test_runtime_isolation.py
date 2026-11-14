"""Runtime isolation tests: sandbox, per-run workspace, repo lock, health, WS auth."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.api import websocket as ws_module
from app.core.security import create_access_token
from app.services.repo_lock import RepositoryLockTimeout, repository_lock
from app.services.run_workspace import (
    cleanup_run_workspace,
    create_run_workspace,
    new_run_id,
    stable_run_id,
)
from app.services.sandbox import (
    build_docker_command,
    run_sandboxed,
    scrub_environment,
)

# ==============================================================================
# Sandbox
# ==============================================================================


def test_scrub_environment_drops_secrets() -> None:
    env = scrub_environment(
        {
            "PATH": "/usr/bin",
            "GEMINI_API_KEY": "sk-secret",
            "OPENAI_API_KEY": "sk-secret",
            "DATABASE_URL": "postgres://user:pass@host/db",
            "JWT_SECRET_KEY": "jwt",
            "SOME_RANDOM": "x",
        }
    )

    assert env["PATH"] == "/usr/bin"
    assert "GEMINI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "JWT_SECRET_KEY" not in env
    assert "SOME_RANDOM" not in env


def test_scrub_environment_redirects_home(tmp_path: Path) -> None:
    env = scrub_environment({"PATH": "/usr/bin", "HOME": "/root"}, home=tmp_path)

    assert env["HOME"] == str(tmp_path)
    assert env["TMPDIR"] == str(tmp_path)


def test_build_docker_command_is_network_isolated(tmp_path: Path) -> None:
    cmd = build_docker_command(["python", "-m", "pytest"], tmp_path, "python:3.12-slim")

    assert cmd[0] == "docker"
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "--user" in cmd
    assert f"{tmp_path}:/workspace:rw" in cmd


def test_run_sandboxed_does_not_leak_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    repo = tmp_path / "repo"
    repo.mkdir()

    proc = run_sandboxed(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('GEMINI_API_KEY', 'missing'), "
            "os.environ.get('DATABASE_URL', 'missing'))",
        ],
        cwd=repo,
        timeout=30,
        run_dir=tmp_path,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == "missing missing", proc.stdout + proc.stderr


def test_run_sandboxed_returns_output(tmp_path: Path) -> None:
    proc = run_sandboxed(
        [sys.executable, "-c", "print('hello from sandbox')"],
        cwd=tmp_path,
        timeout=30,
        run_dir=tmp_path,
    )

    assert "hello from sandbox" in proc.stdout


# ==============================================================================
# Per-run workspace
# ==============================================================================


def test_run_ids_are_unique() -> None:
    assert new_run_id("pr-1") != new_run_id("pr-1")
    assert new_run_id("pr-1").startswith("pr1")


def test_stable_run_id_is_deterministic_per_revision() -> None:
    """A retry must find the worktree its checkpoint points at."""
    assert stable_run_id("pr-1", "a" * 40) == stable_run_id("pr-1", "a" * 40)
    assert stable_run_id("pr-1", "a" * 40) != stable_run_id("pr-1", "b" * 40)
    assert stable_run_id("pr-1", "a" * 40).startswith("pr1")


def test_workspaces_get_distinct_test_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first = create_run_workspace(repo, new_run_id("pr-1"))
    second = create_run_workspace(repo, new_run_id("pr-1"))

    try:
        path_a = first.test_path("tests/test_billing.py")
        path_b = second.test_path("tests/test_billing.py")

        assert path_a != path_b
        assert path_a.parent == repo / "tests"
        assert "__run_" in path_a.name
        assert first.reports_dir != second.reports_dir
        assert first.reports_dir.is_dir()
    finally:
        cleanup_run_workspace(first)
        cleanup_run_workspace(second)

    assert not first.root.exists()
    assert not second.root.exists()


def test_report_paths_live_inside_the_run_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = create_run_workspace(repo, new_run_id("pr-2"))
    try:
        report = workspace.report_path("report.json")
        assert report.parent == workspace.root
        report.write_text(json.dumps({"summary": {"total": 1}}), encoding="utf-8")
        assert json.loads(report.read_text())["summary"]["total"] == 1
    finally:
        cleanup_run_workspace(workspace)


# ==============================================================================
# Repository lock
# ==============================================================================


def test_repository_lock_is_mutually_exclusive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    # Nesting is intentional: the outer lock stays held while the inner
    # acquisition is expected to time out.
    with repository_lock(repo, timeout=1), pytest.raises(RepositoryLockTimeout):  # noqa: SIM117
        with repository_lock(repo, timeout=0.2, poll_interval=0.05):
            pass

    # Released: a later acquisition succeeds.
    with repository_lock(repo, timeout=1):
        pass


def test_repository_lock_reclaims_stale_lock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lock_dir = repo.parent / f".{repo.name}.lock"
    lock_dir.mkdir()
    old = os.path.getmtime(lock_dir) - 10_000
    os.utime(lock_dir, (old, old))

    with repository_lock(repo, timeout=1, stale_after=60):
        pass

    assert not lock_dir.exists()


def test_repository_lock_reclaims_previous_attempt_of_same_task(tmp_path: Path) -> None:
    """A crashed attempt's lock must not block its own checkpointed retry."""
    repo = tmp_path / "repo"
    repo.mkdir()
    lock_dir = repo.parent / f".{repo.name}.lock"
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": 0, "owner_id": "task-123"}),
        encoding="utf-8",
    )

    with repository_lock(repo, timeout=0.5, poll_interval=0.05, owner_id="task-123"):
        pass

    assert not lock_dir.exists()


def test_repository_lock_does_not_reclaim_another_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with (  # noqa: SIM117
        repository_lock(repo, timeout=1, owner_id="task-1"),
        pytest.raises(RepositoryLockTimeout),
    ):
        with repository_lock(repo, timeout=0.2, poll_interval=0.05, owner_id="task-2"):
            pass


# ==============================================================================
# Health endpoint
# ==============================================================================


async def test_health_returns_503_when_dependency_unhealthy(client, monkeypatch) -> None:
    import app.main as main_module

    async def _unhealthy() -> dict[str, str]:
        return {"postgres": "unhealthy", "redis": "healthy", "qdrant": "healthy"}

    monkeypatch.setattr(main_module, "_check_services", _unhealthy)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_health_returns_200_when_all_dependencies_healthy(client, monkeypatch) -> None:
    import app.main as main_module

    async def _healthy() -> dict[str, str]:
        return {"postgres": "healthy", "redis": "healthy", "qdrant": "healthy"}

    monkeypatch.setattr(main_module, "_check_services", _healthy)

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


# ==============================================================================
# WebSocket authorization
# ==============================================================================


class _FakeWebSocket:
    def __init__(self) -> None:
        self.closed: list[int] = []

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


async def test_websocket_rejects_missing_token() -> None:
    socket = _FakeWebSocket()

    user_id = await ws_module._authorize(socket, None)

    assert user_id is None
    assert socket.closed == [ws_module.AUTH_CLOSE_CODE]


async def test_websocket_rejects_invalid_token() -> None:
    socket = _FakeWebSocket()

    user_id = await ws_module._authorize(socket, "not-a-jwt")

    assert user_id is None
    assert socket.closed == [ws_module.AUTH_CLOSE_CODE]


async def test_websocket_accepts_valid_token() -> None:
    socket = _FakeWebSocket()
    token = create_access_token("user-123")

    user_id = await ws_module._authorize(socket, token)

    assert user_id == "user-123"
    assert socket.closed == []


async def test_websocket_ownership_check_fails_closed() -> None:
    assert await ws_module._user_owns_pr(None, "pr-1") is False
    assert await ws_module._user_owns_repo("user-123", "does-not-exist") is False


# ==============================================================================
# Retry idempotency
# ==============================================================================


async def test_store_analysis_results_upserts_single_review(db_session, monkeypatch) -> None:
    """A retried analysis must update the review instead of duplicating it."""
    import uuid

    from app.models.pull_request import PullRequest
    from app.models.repository import Repository
    from app.models.review_comment import ReviewComment
    from app.models.user import User
    from app.tasks import pr_pipeline

    suffix = uuid.uuid4().hex[:8]
    user = User(
        id=str(uuid.uuid4()),
        github_id=f"gh-{suffix}",
        username=f"user-{suffix}",
        email=f"{suffix}@example.com",
        name="Isolation Test",
        role="member",
    )
    db_session.add(user)
    await db_session.flush()

    repository = Repository(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        github_repo_id=f"repo-{suffix}",
        full_name=f"owner-{suffix}/repo",
        name=f"repo-{suffix}",
        owner_login=f"owner-{suffix}",
        clone_url="https://example.com/repo.git",
        default_branch="main",
        index_status="indexed",
        is_indexed=True,
    )
    db_session.add(repository)
    await db_session.flush()

    pull_request = PullRequest(
        id=str(uuid.uuid4()),
        repository_id=repository.id,
        pr_number=7,
        github_pr_id=f"pr-{suffix}",
        title="Retry idempotency",
        state="open",
        author="dev",
        base_branch="main",
        head_branch="feature",
        head_sha="a" * 40,
        base_sha="b" * 40,
        analysis_status="pending",
    )
    db_session.add(pull_request)
    await db_session.flush()

    class _Shared:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc) -> bool:
            # Mirror the real session context manager: persist on success.
            await db_session.flush()
            return False

    monkeypatch.setattr(pr_pipeline, "get_session", lambda: _Shared())

    pr_id = pull_request.id
    db_session.expunge_all()

    state = {
        "warnings": [],
        "risk_score": {"level": "medium", "score": 4.0, "factors": []},
        "affected_modules": [],
        "review": {
            "full_review_body": "first run",
            "risk_level": "medium",
            "risk_score": 4.0,
            "summary": "first",
            "action_items": ["a"],
        },
    }
    await pr_pipeline._store_analysis_results(pr_id, state)

    state["review"]["full_review_body"] = "second run"
    state["review"]["summary"] = "second"
    await pr_pipeline._store_analysis_results(pr_id, state)

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(ReviewComment)
            .where(ReviewComment.pull_request_id == pr_id)
        )
    ).scalar_one()
    stored = (
        await db_session.execute(
            select(ReviewComment).where(ReviewComment.pull_request_id == pr_id)
        )
    ).scalar_one()

    assert count == 1
    assert stored.body == "second run"
    assert stored.summary == "second"
