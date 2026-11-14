"""Real test-runner adapters for the next-generation verify engine.

The legacy execution node shells out to pytest with ``--json-report`` and
``--cov``; both are third-party plugins that are not installed in every
environment, so a plain ``pytest`` run can fail on "unrecognized arguments".
This adapter uses pytest's built-in JUnit XML output instead: no plugin
dependency, and failures are parsed from a stable schema.

Generated tests are written under run-scoped file names (never overwriting the
repository's own tests) and removed after the run.
"""

from __future__ import annotations

import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from app.core.logging import get_logger
from app.services.run_workspace import (
    RunWorkspace,
    cleanup_run_workspace,
    create_run_workspace,
    new_run_id,
)
from app.services.sandbox import run_sandboxed
from app.verify.contracts import FailureFact, RunResult, VerifyRequest

logger = get_logger(__name__)


class SandboxTestRunner:
    """Run pytest inside the existing sandbox and parse JUnit XML."""

    def __init__(self, repo_path: Path, *, timeout: int = 300) -> None:
        self.repo_path = Path(repo_path)
        self.timeout = timeout

    def run(self, request: VerifyRequest) -> RunResult:
        repo_path = Path(request.workspace) if request.workspace else self.repo_path
        workspace = create_run_workspace(repo_path, new_run_id("verify"))
        written: list[Path] = []
        try:
            written = self._write_generated(repo_path, request, workspace)
            paths = self._selected_paths(repo_path, request, workspace)
            report = workspace.report_path("junit.xml")
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                *paths,
                f"--junitxml={report}",
                "-q",
                "--tb=short",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:asyncio",
            ]

            start = time.monotonic()
            try:
                proc = run_sandboxed(
                    cmd,
                    cwd=repo_path,
                    timeout=self.timeout,
                    run_dir=workspace.root,
                )
            except subprocess.TimeoutExpired:
                return RunResult(
                    status="error",
                    duration_seconds=float(self.timeout),
                )
            except FileNotFoundError as exc:
                logger.warning("Test runner not found", error=str(exc))
                return RunResult(status="error")

            duration = time.monotonic() - start
            return self._parse(proc.returncode, report, duration)
        finally:
            for path in written:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove generated test", path=str(path))
            cleanup_run_workspace(workspace)

    def _write_generated(
        self,
        repo_path: Path,
        request: VerifyRequest,
        workspace: RunWorkspace,
    ) -> list[Path]:
        written: list[Path] = []
        for case in request.bundle.generated:
            if not case.content:
                continue
            relative = Path(case.path)
            if relative.is_absolute() or ".." in relative.parts:
                logger.warning("Refusing generated test outside the repository", path=case.path)
                continue
            target = workspace.test_path(str(relative))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(case.content, encoding="utf-8")
            written.append(target)
        return written

    def _selected_paths(
        self,
        repo_path: Path,
        request: VerifyRequest,
        workspace: RunWorkspace,
    ) -> list[str]:
        paths: list[str] = []
        for case in request.bundle.generated:
            if not case.content:
                continue
            relative = Path(case.path)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = workspace.test_path(str(relative))
            try:
                paths.append(str(target.relative_to(repo_path)).replace("\\", "/"))
            except ValueError:
                continue
        for existing in request.bundle.existing:
            candidate = repo_path / existing
            if candidate.is_file():
                paths.append(str(existing).replace("\\", "/"))
        return paths

    @staticmethod
    def _parse(returncode: int, report: Path, duration: float) -> RunResult:
        total = passed = failed = skipped = 0
        failures: list[FailureFact] = []

        if report.exists():
            try:
                root = ET.parse(report).getroot()
                suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
                for suite in suites:
                    total += int(suite.get("tests", 0) or 0)
                    failed += int(suite.get("failures", 0) or 0)
                    failed += int(suite.get("errors", 0) or 0)
                    skipped += int(suite.get("skipped", 0) or 0)
                    for case in suite.findall("testcase"):
                        problem = case.find("failure")
                        if problem is None:
                            problem = case.find("error")
                        if problem is None:
                            continue
                        name = case.get("name", "unknown")
                        classname = case.get("classname", "")
                        failures.append(
                            FailureFact(
                                test_id=f"{classname}::{name}" if classname else name,
                                message=(problem.get("message") or problem.text or "")[:2000],
                                origin="unknown",
                            )
                        )
            except (ET.ParseError, OSError) as exc:
                logger.warning("Could not parse JUnit report", error=str(exc))
                return RunResult(status="error", duration_seconds=round(duration, 2))

        passed = max(0, total - failed - skipped)
        if returncode == 0 and not failed:
            status = "passed"
        elif returncode in (0, 1):
            status = "failed"
        else:
            status = "error"

        return RunResult(
            status=status,
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=round(duration, 2),
            failures=failures,
        )


__all__ = ["SandboxTestRunner"]
