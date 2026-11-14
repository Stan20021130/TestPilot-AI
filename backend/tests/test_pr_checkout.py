"""Tests for the PR head checkout (analysing the PR revision, not the default branch)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import pr_checkout
from app.services.pr_checkout import (
    ensure_pr_checkout,
    release_pr_checkout,
    resolve_repo_root,
)

GIT = shutil.which("git")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        [GIT or "git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str) -> str:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return subprocess.run(
        [GIT or "git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Repository with a base commit and a head commit, checked out at base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)

    target = repo / "app.py"
    target.write_text("BASE = 1\n", encoding="utf-8")
    base_sha = _commit(repo, "base")

    target.write_text("BASE = 1\n\n\ndef head_only():\n    return 42\n", encoding="utf-8")
    head_sha = _commit(repo, "head")

    # Simulate the default-branch clone: working tree back at base.
    _git("reset", "-q", "--hard", base_sha, cwd=repo)
    return repo, base_sha, head_sha


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_checkout_materialises_pr_head_not_default_branch(git_repo) -> None:
    repo, base_sha, head_sha = git_repo

    checkout = ensure_pr_checkout(repo_path=repo, pr_number=7, head_sha=head_sha, run_id="run-1")

    try:
        assert checkout.mode == "head"
        assert checkout.ref == head_sha
        assert checkout.path != repo
        assert checkout.path.is_dir()

        head_source = (checkout.path / "app.py").read_text(encoding="utf-8")
        base_source = (repo / "app.py").read_text(encoding="utf-8")

        assert "head_only" in head_source
        assert "head_only" not in base_source
        assert base_sha != head_sha
    finally:
        release_pr_checkout(checkout, repo)

    assert not checkout.path.exists()


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_checkout_falls_back_with_warning_when_head_unresolvable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "app.py").write_text("BASE = 1\n", encoding="utf-8")
    _commit(repo, "base")

    checkout = ensure_pr_checkout(
        repo_path=repo,
        pr_number=99,
        head_sha="0" * 40,
        run_id="run-2",
    )

    assert checkout.mode == "base"
    assert checkout.path == repo
    assert checkout.warnings
    assert any("default branch" in warning for warning in checkout.warnings)


def test_missing_checkout_degrades_to_given_path(tmp_path: Path) -> None:
    checkout = ensure_pr_checkout(
        repo_path=tmp_path / "does-not-exist",
        pr_number=1,
        head_sha="abc",
        run_id="run-3",
    )

    assert checkout.mode == "base"
    assert checkout.warnings


def test_resolve_repo_root_prefers_run_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        pr_checkout,
        "get_settings",
        lambda: SimpleNamespace(repo_storage_path=tmp_path / "repos"),
    )

    assert resolve_repo_root({"workspace_path": str(tmp_path / "ws")}) == tmp_path / "ws"
    assert resolve_repo_root({"repository_id": "repo-1"}) == tmp_path / "repos" / "repo-1"


def test_diff_agent_reads_the_workspace_override(tmp_path: Path) -> None:
    from app.agents.diff_agent import _identify_changed_nodes

    workspace = tmp_path / "ws"
    source = workspace / "app/services/billing.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def first():\n    return 1\n\n\ndef second():\n    return 2\n",
        encoding="utf-8",
    )

    nodes, contracts = _identify_changed_nodes(
        {"repository_id": "repo-1", "workspace_path": str(workspace)},
        [
            {
                "path": "app/services/billing.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "old_path": None,
                "changed_lines": [5],
            }
        ],
    )

    assert [node["name"] for node in nodes] == ["second"]
    assert [contract["symbol"] for contract in contracts] == ["second"]
