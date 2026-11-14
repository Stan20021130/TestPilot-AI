"""TestPilot AI — Per-run workspace.

Every analysis run gets its own scratch directory and its own test-file suffix.
Before this existed, all runs shared ``/tmp/testpilot_report.json`` and wrote
generated tests to the same repository paths, so two PRs of the same repository
analysed concurrently could overwrite each other's tests and read each other's
report — producing confidently wrong results.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

RUN_DIR_PREFIX = "testpilot-"
RUN_MARKER = ".testpilot"


@dataclass(frozen=True)
class RunWorkspace:
    """Scratch space and naming scope for a single pipeline run."""

    run_id: str
    repo_path: Path
    root: Path

    @property
    def reports_dir(self) -> Path:
        """Directory for runner JSON reports, logs and ``HOME``."""
        return self.root

    def test_path(self, relative_path: str) -> Path:
        """Absolute path for a generated test file, unique to this run.

        The file stays inside its original directory so the repository's
        imports and ``conftest.py`` still apply; only the file name is scoped.
        """
        rel = Path(relative_path)
        token = self.run_id.rsplit("-", 1)[-1]
        return self.repo_path / rel.with_name(f"{rel.stem}__run_{token}{rel.suffix}")

    def report_path(self, name: str) -> Path:
        return self.reports_dir / name


def new_run_id(pr_id: str | None = None) -> str:
    """Build a short, filesystem-safe, collision-free run identifier."""
    prefix = (pr_id or "run").replace("-", "")[:8]
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def stable_run_id(pr_id: str | None, head_sha: str | None = None) -> str:
    """Deterministic run id for a (PR, head) pair.

    A crashed analysis is retried as the *same* run: the checkpoint thread, the
    PR-head worktree and the scratch directory must all be reachable from the
    retry, otherwise a resume has nothing to resume into. Deterministic ids also
    make two queued analyses of the same head collapse onto one tree instead of
    racing two worktrees.
    """
    prefix = (pr_id or "run").replace("-", "")[:8]
    digest = hashlib.sha256(f"{pr_id}:{head_sha or ''}".encode()).hexdigest()[:10]
    return f"{prefix}-{digest}"


def create_run_workspace(repo_path: Path, run_id: str) -> RunWorkspace:
    """Create the scratch directory for a run."""
    root = Path(tempfile.mkdtemp(prefix=f"{RUN_DIR_PREFIX}{run_id}-"))
    return RunWorkspace(run_id=run_id, repo_path=repo_path, root=root)


def cleanup_run_workspace(workspace: RunWorkspace) -> None:
    """Remove the scratch directory; never raises."""
    shutil.rmtree(workspace.root, ignore_errors=True)


def remove_empty_marker_dirs(repo_path: Path) -> None:
    """Drop the ``.testpilot`` marker directory when it is empty."""
    marker = repo_path / RUN_MARKER
    if not marker.is_dir():
        return
    for child in marker.iterdir():
        if child.is_dir() and not any(child.iterdir()):
            shutil.rmtree(child, ignore_errors=True)
    with contextlib.suppress(OSError):
        marker.rmdir()
