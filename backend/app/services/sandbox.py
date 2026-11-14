"""TestPilot AI — Execution sandbox.

Generated tests are untrusted code: they are written by a model, may contain
arbitrary statements, and run on the same host as the worker. This module
centralises *how* they are executed so the isolation story lives in one place:

- **environment scrubbing** — secrets (LLM keys, DB passwords, VCS tokens) are
  never inherited by the child process;
- **resource limits** — CPU, address space, file size and descriptor limits on
  POSIX (skipped elsewhere);
- **container mode** — optional ``SANDBOX_MODE=docker`` that runs the command
  with ``--network=none``, a read-only root, capped memory and a non-root user.

Default mode is ``subprocess``: it removes the env-leak and runaway-process
risks without requiring a Docker daemon, and is honest about not being a
security boundary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Only these inherited variables reach the child process.
SAFE_ENV_KEYS = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "VIRTUAL_ENV",
    "PYTHONPATH",
}

# Defence in depth: never forward these even if they appear in the allow-list.
DENIED_ENV_PREFIXES = ("GEMINI_", "OPENAI_", "ANTHROPIC_", "AWS_", "GITHUB_", "QDRANT_")
DENIED_ENV_KEYS = {
    "DATABASE_URL",
    "JWT_SECRET_KEY",
    "SECRET_KEY",
    "REDIS_PASSWORD",
    "POSTGRES_PASSWORD",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_CLIENT_SECRET",
}


def scrub_environment(
    base: dict[str, str] | None = None,
    *,
    home: Path | None = None,
) -> dict[str, str]:
    """Build a minimal environment for generated code.

    Args:
        base: Source environment (defaults to ``os.environ``).
        home: When provided, ``HOME``/``TMPDIR`` point at the per-run directory
            so the child cannot read the worker user's dotfiles or caches.

    Returns:
        A new environment dictionary safe to pass to ``subprocess``.
    """
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items() if key in SAFE_ENV_KEYS}

    for key in list(env):
        if key in DENIED_ENV_KEYS or any(key.startswith(prefix) for prefix in DENIED_ENV_PREFIXES):
            env.pop(key, None)

    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if home is not None:
        env["HOME"] = str(home)
        env["TMPDIR"] = str(home)
    return env


def _resource_limiter(cpu_seconds: int, memory_mb: int) -> Any:
    """Return a preexec_fn enforcing rlimits, or None on unsupported platforms."""
    try:
        import resource  # noqa: PLC0415 - POSIX only
    except ImportError:
        return None

    address_space = max(256, memory_mb) * 1024 * 1024

    def _apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _apply


def build_docker_command(cmd: list[str], workspace: Path, image: str) -> list[str]:
    """Wrap a command in a network-isolated, read-only container."""
    return [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        "--user",
        "65534:65534",
        "-v",
        f"{workspace}:/workspace:rw",
        "-w",
        "/workspace",
        image,
        *cmd,
    ]


def run_sandboxed(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    run_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """Execute a command under the configured sandbox.

    Args:
        cmd: Command and arguments.
        cwd: Working directory (the repository checkout).
        timeout: Wall-clock timeout in seconds.
        run_dir: Per-run scratch directory; used for ``HOME``/``TMPDIR`` and as
            the container mount root in docker mode.

    Returns:
        The completed process.

    Raises:
        FileNotFoundError: When the runner binary is missing.
        subprocess.TimeoutExpired: When the command exceeds ``timeout``.
    """
    settings = get_settings()
    mode = (settings.sandbox_mode or "subprocess").lower()

    if mode == "docker":
        docker_cmd = build_docker_command(cmd, run_dir, settings.sandbox_docker_image)
        logger.info("Running sandboxed command", mode="docker", cwd=str(run_dir))
        return subprocess.run(
            docker_cmd,
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    env = scrub_environment(home=run_dir)
    preexec = _resource_limiter(settings.sandbox_cpu_seconds, settings.sandbox_memory_mb)
    logger.info("Running sandboxed command", mode="subprocess", cwd=str(cwd))

    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
    }
    if preexec is not None:
        kwargs["preexec_fn"] = preexec

    return subprocess.run(cmd, **kwargs)


def docker_available() -> bool:
    """True when a docker CLI is present (used for diagnostics only)."""
    return shutil.which("docker") is not None
