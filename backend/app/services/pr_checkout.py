"""TestPilot AI — PR head checkout.

The repository checkout under ``repo_storage_path/<repository_id>`` holds the
default branch. Analysing a PR against that tree means parsing the wrong
revision: line numbers, source slices and coverage mappings all describe code
the PR never touched.

This module materialises the PR head as a git worktree (outside the checkout,
so the index clone stays clean) and hands every agent a single path to read.
When the head cannot be resolved — missing checkout, private repository without
a token, unreachable ref — it degrades to the default branch and says so in
``warnings`` instead of silently analysing stale code.
"""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

GIT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class PrCheckout:
    """The tree the pipeline should read for this run."""

    path: Path
    mode: str  # "head" (PR revision) | "base" (default branch fallback)
    ref: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def resolve_repo_root(state: Any) -> Path:
    """Return the tree agents should read: the run workspace or the clone."""
    override = state.get("workspace_path") if hasattr(state, "get") else None
    if override:
        return Path(override)
    settings = get_settings()
    repo_id = state.get("repository_id", "default") if hasattr(state, "get") else "default"
    return settings.repo_storage_path / str(repo_id)


def _git(
    args: list[str],
    cwd: Path,
    token: str | None = None,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a git command without prompting and without persisting credentials.

    The token is passed through ``http.extraheader`` via the environment rather
    than embedded in the remote URL, so it is never written to ``.git/config``.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
            }
        )
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _has_commit(repo_path: Path, sha: str | None) -> bool:
    if not sha:
        return False
    return _git(["cat-file", "-e", f"{sha}^{{commit}}"], repo_path).returncode == 0


def _pr_refspecs(pr_number: int) -> list[str]:
    """PR head refs across the supported providers, most common first."""
    target = f"refs/testpilot/pr/{pr_number}"
    return [
        f"+refs/pull/{pr_number}/head:{target}",  # GitHub, Azure DevOps
        f"+refs/merge-requests/{pr_number}/head:{target}",  # GitLab
        f"+refs/pull-requests/{pr_number}/from:{target}",  # Bitbucket
    ]


def _resolve_head(repo_path: Path, pr_number: int, head_sha: str | None, token: str | None) -> str | None:
    """Find the PR head commit locally or fetch it; None when unavailable."""
    if _has_commit(repo_path, head_sha):
        return head_sha

    for refspec in _pr_refspecs(pr_number):
        proc = _git(["fetch", "--no-tags", "origin", refspec], repo_path, token)
        if proc.returncode != 0:
            continue
        ref = f"refs/testpilot/pr/{pr_number}"
        tip = _git(["rev-parse", ref], repo_path).stdout.strip()
        if tip:
            return tip

    if head_sha:
        proc = _git(["fetch", "--no-tags", "origin", head_sha], repo_path, token)
        if proc.returncode == 0 and _has_commit(repo_path, head_sha):
            return head_sha

    return None


def ensure_pr_checkout(
    *,
    repo_path: Path,
    pr_number: int,
    head_sha: str | None = None,
    token: str | None = None,
    run_id: str,
) -> PrCheckout:
    """Create a worktree at the PR head, or degrade to the default branch.

    Args:
        repo_path: Default-branch clone produced by the indexing task.
        pr_number: PR number, used to build provider refspecs.
        head_sha: Expected head commit (from the webhook payload).
        token: Optional VCS token for private repositories.
        run_id: Run identifier, keeps worktrees unique per run.

    Returns:
        A :class:`PrCheckout` describing the tree to read.
    """
    warnings: list[str] = []

    if not (repo_path / ".git").exists():
        warnings.append(
            "pr_checkout: repository checkout is missing or not a git repository; "
            "analysis falls back to whatever is on disk"
        )
        return PrCheckout(path=repo_path, mode="base", warnings=tuple(warnings))

    target = _resolve_head(repo_path, pr_number, head_sha, token)
    if target is None:
        warnings.append(
            "pr_checkout: could not resolve the PR head commit (no local object and "
            "fetch failed); analysing the default branch instead — line numbers and "
            "source slices may not match the PR"
        )
        logger.warning(
            "PR head unavailable, falling back to default branch",
            pr_number=pr_number,
            head_sha=head_sha,
        )
        return PrCheckout(path=repo_path, mode="base", warnings=tuple(warnings))

    if head_sha and target != head_sha:
        warnings.append(
            f"pr_checkout: PR ref tip {target[:8]} differs from webhook head {head_sha[:8]}"
        )

    worktree = repo_path.parent / f".{repo_path.name}-pr-{run_id}"
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)

    proc = _git(
        ["worktree", "add", "--detach", "--force", str(worktree), target],
        repo_path,
    )
    if proc.returncode != 0:
        warnings.append(
            "pr_checkout: git worktree creation failed "
            f"({proc.stderr.strip()[:200]}); analysing the default branch"
        )
        logger.warning("Worktree creation failed", error=proc.stderr.strip()[:200])
        return PrCheckout(path=repo_path, mode="base", ref=target, warnings=tuple(warnings))

    logger.info("PR head checkout ready", mode="worktree", ref=target[:12], path=str(worktree))
    return PrCheckout(path=worktree, mode="head", ref=target, warnings=tuple(warnings))


def release_pr_checkout(checkout: PrCheckout, repo_path: Path) -> None:
    """Remove the worktree; never raises."""
    if checkout.mode != "head":
        return

    with contextlib.suppress(Exception):
        _git(["worktree", "remove", "--force", str(checkout.path)], repo_path)
    with contextlib.suppress(Exception):
        _git(["worktree", "prune"], repo_path)
    if checkout.path.exists():
        shutil.rmtree(checkout.path, ignore_errors=True)
