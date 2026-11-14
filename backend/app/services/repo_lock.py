"""TestPilot AI — Repository-level mutual exclusion.

A repository checkout is shared by indexing and by every PR analysis of that
repository. Two concurrent runs can rewrite the working tree mid-analysis
(indexing pulls, repair writes source, execution writes generated tests).

This lock serialises those operations per repository. It is a portable
``mkdir``-based lock (atomic on local filesystems) rather than ``flock`` so it
behaves the same on the Linux worker and on a developer Windows box, and it
reclaims locks left behind by a crashed worker after ``stale_after`` seconds.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_SECONDS = 0.5
DEFAULT_STALE_AFTER_SECONDS = 3600.0


class RepositoryLockTimeout(RuntimeError):
    """Raised when the repository lock cannot be acquired in time."""


def _lock_dir(repo_path: Path) -> Path:
    return repo_path.parent / f".{repo_path.name}.lock"


def _is_stale(lock_dir: Path, stale_after: float) -> bool:
    try:
        age = time.time() - lock_dir.stat().st_mtime
    except OSError:
        return False
    return age > stale_after


def _write_owner(lock_dir: Path, owner_id: str | None = None) -> None:
    payload: dict[str, object] = {"pid": os.getpid(), "acquired_at": time.time()}
    if owner_id:
        payload["owner_id"] = owner_id
    with contextlib.suppress(OSError):
        (lock_dir / "owner.json").write_text(json.dumps(payload), encoding="utf-8")


def _read_owner_id(lock_dir: Path) -> str | None:
    try:
        payload = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    owner_id = payload.get("owner_id")
    return str(owner_id) if owner_id else None


@contextlib.contextmanager
def repository_lock(
    repo_path: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_SECONDS,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
    owner_id: str | None = None,
) -> Iterator[Path]:
    """Hold an exclusive lock for a repository.

    Args:
        repo_path: Repository checkout being protected.
        timeout: Maximum seconds to wait for the lock.
        poll_interval: Seconds between acquisition attempts.
        stale_after: Age after which an abandoned lock is reclaimed.
        owner_id: Stable id of the current task. A lock left behind by the
            *previous attempt of the same task* is reclaimed immediately, so a
            checkpointed retry does not wait an hour for ``stale_after``.

    Yields:
        The lock directory path.

    Raises:
        RepositoryLockTimeout: When the lock is held for longer than ``timeout``.
    """
    lock_dir = _lock_dir(repo_path)
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    waited = False

    while True:
        try:
            lock_dir.mkdir()
            _write_owner(lock_dir, owner_id)
            break
        except FileExistsError:
            if owner_id is not None and _read_owner_id(lock_dir) == owner_id:
                logger.warning(
                    "Reclaiming repository lock left by a previous attempt of this task",
                    lock=str(lock_dir),
                    owner_id=owner_id,
                )
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            if _is_stale(lock_dir, stale_after):
                logger.warning("Reclaiming stale repository lock", lock=str(lock_dir))
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise RepositoryLockTimeout(
                    f"Repository {repo_path.name!r} is busy (lock held at {lock_dir})"
                ) from None
            if not waited:
                logger.info("Waiting for repository lock", lock=str(lock_dir))
                waited = True
            time.sleep(poll_interval)

    try:
        yield lock_dir
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)
