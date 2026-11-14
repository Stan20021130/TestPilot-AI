"""Real-sandbox tests for the next-generation verify runner.

These run actual pytest in a temporary repository: the point is to prove the
adapter does not depend on third-party report plugins and does not leak
generated test files into the checkout.
"""

from __future__ import annotations

import site
from pathlib import Path

import pytest

from app.verify import run_default_verify, run_verify
from app.verify.contracts import (
    MutationReport,
    TestBundle,
    TestCase,
    TestPatch,
    VerifyBudget,
    VerifyRequest,
)
from app.verify.runners import SandboxTestRunner
from app.verify.services import BundleTestEditor

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _pytest_visible_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox drops APPDATA, so pytest in the user site is not importable.

    Production installs pytest into the system site-packages; local development
    needs the explicit PYTHONPATH (which the sandbox already allows).
    """
    monkeypatch.setenv("PYTHONPATH", site.getusersitepackages())


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    return tmp_path


def _request(repo: Path, *, generated=None, existing=None) -> VerifyRequest:
    return VerifyRequest(
        workspace=str(repo),
        bundle=TestBundle(generated=generated or [], existing=existing or []),
        budget=VerifyBudget(max_mutations=0),
    )


def test_passing_pytest_suite(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    result = SandboxTestRunner(repo).run(_request(repo, existing=["tests/test_sample.py"]))

    assert result.status == "passed"
    assert result.total == 1
    assert result.passed == 1
    assert result.failed == 0
    assert result.failures == []


def test_failing_pytest_suite_reports_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_bad():\n    assert 1 == 2\n",
        encoding="utf-8",
    )

    result = SandboxTestRunner(repo).run(_request(repo, existing=["tests/test_sample.py"]))

    assert result.status == "failed"
    assert result.total == 1
    assert result.failed == 1
    assert result.failures
    assert "assert" in result.failures[0].message.lower()
    assert result.failures[0].origin == "unknown"


def test_generated_test_is_scoped_and_cleaned_up(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    generated = TestCase(
        id="gen-1",
        path="tests/test_generated.py",
        content="def test_generated():\n    assert True\n",
    )

    result = SandboxTestRunner(repo).run(_request(repo, generated=[generated]))

    assert result.status == "passed"
    assert result.total == 1
    # The declared path is never written; only the run-scoped sibling is, and
    # it is removed in the finally block.
    assert not (repo / "tests" / "test_generated.py").exists()
    assert not list((repo / "tests").glob("*__run_*"))


class _NoopRepairer:
    def repair(self, failures, bundle):
        return []


class _NoopProposer:
    def propose(self, failures):
        return []


class _NoopProbe:
    def probe(self, targets):
        return MutationReport()


def test_run_verify_end_to_end_with_real_pytest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    request = _request(repo, existing=["tests/test_sample.py"])

    report = run_verify(
        request,
        runner=SandboxTestRunner(repo),
        repairer=_NoopRepairer(),
        editor=BundleTestEditor(request.bundle),
        proposer=_NoopProposer(),
        probe=_NoopProbe(),
    )

    assert report.verdict.value == "green"
    assert report.exit_reason.value == "green"
    assert report.runs[0].passed == 1


def test_default_assembly_runs_green_without_llm(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    request = _request(repo, existing=["tests/test_sample.py"])

    report = run_default_verify(request)

    assert report.verdict.value == "green"
    assert report.runs[0].passed == 1


def test_bundle_editor_updates_the_runner_input(tmp_path: Path) -> None:
    bundle = TestBundle(generated=[TestCase(id="t1", path="tests/test_a.py", content="old")])

    BundleTestEditor(bundle).apply(TestPatch(test_id="t1", replacement="new"))

    assert bundle.generated[0].content == "new"


def test_junit_parser_reads_failures(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """<?xml version="1.0"?>
<testsuites>
  <testsuite tests="2" failures="1" errors="0" skipped="0">
    <testcase classname="tests.test_a" name="test_ok" />
    <testcase classname="tests.test_a" name="test_bad">
      <failure message="assert 1 == 2">traceback</failure>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    result = SandboxTestRunner._parse(1, report, 0.25)

    assert result.status == "failed"
    assert result.total == 2
    assert result.failed == 1
    assert result.failures[0].test_id == "tests.test_a::test_bad"


def test_missing_report_and_nonzero_exit_is_error(tmp_path: Path) -> None:
    result = SandboxTestRunner._parse(2, tmp_path / "missing.xml", 0.1)

    assert result.status == "error"
