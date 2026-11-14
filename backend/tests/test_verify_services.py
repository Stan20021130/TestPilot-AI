"""Tests for the verify service adapters (repair, patch proposal, mutation)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.mutation_engine import MutantCandidate, MutationEngine
from app.verify.contracts import FailureFact, TestBundle, TestCase
from app.verify.services import (
    EngineMutationProbe,
    LLMPatchService,
    LLMRepairService,
    PatchItem,
    PatchPlan,
    RepairItem,
    RepairPlan,
)

pytestmark = pytest.mark.unit


class _Completions:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class _Client:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.completions = _Completions(response, error)
        self.chat = SimpleNamespace(completions=self.completions)


def _bundle() -> TestBundle:
    return TestBundle(
        generated=[
            TestCase(
                id="t1",
                path="tests/test_a.py",
                content="def test_a():\n    assert False\n",
            )
        ]
    )


def _failure() -> FailureFact:
    return FailureFact(test_id="t1", message="assert False", origin="test_defect")


def test_repair_service_returns_patch_for_known_test() -> None:
    client = _Client(
        RepairPlan(
            patches=[
                RepairItem(
                    test_id="t1",
                    replacement="def test_a():\n    assert True\n",
                    rationale="fix assertion",
                )
            ]
        )
    )

    patches = LLMRepairService(client).repair([_failure()], _bundle())

    assert len(patches) == 1
    assert patches[0].test_id == "t1"
    assert "assert True" in patches[0].replacement
    prompt = client.completions.calls[0]["messages"][1]["content"]
    assert "assert False" in prompt


def test_repair_service_drops_unknown_test_ids() -> None:
    client = _Client(RepairPlan(patches=[RepairItem(test_id="invented", replacement="whatever")]))

    assert LLMRepairService(client).repair([_failure()], _bundle()) == []


def test_repair_service_failure_is_a_noop() -> None:
    client = _Client(error=RuntimeError("model down"))

    assert LLMRepairService(client).repair([_failure()], _bundle()) == []


def test_patch_service_returns_repository_relative_proposal() -> None:
    client = _Client(
        PatchPlan(
            proposals=[
                PatchItem(file="app/services/billing.py", diff="+ return amount * 2"),
            ]
        )
    )

    proposals = LLMPatchService(client).propose([_failure()])

    assert proposals[0].file == "app/services/billing.py"
    assert "+ return" in proposals[0].diff


def test_patch_service_rejects_escaping_paths() -> None:
    client = _Client(
        PatchPlan(
            proposals=[
                PatchItem(file="../outside.py", diff="+ bad"),
                PatchItem(file="/abs/path.py", diff="+ bad"),
            ]
        )
    )

    assert LLMPatchService(client).propose([_failure()]) == []


def test_mutation_probe_maps_engine_results(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "app" / "x.py"
    source.parent.mkdir()
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    mutant = MutantCandidate(
        mutant_id="m1",
        file_path="app/x.py",
        line_number=2,
        mutation_type="return_tamper",
        description="tamper",
        original_snippet="return 1",
        mutated_snippet="return 2",
        mutated_source="def f():\n    return 2\n",
    )
    monkeypatch.setattr(
        MutationEngine,
        "generate_mutants",
        classmethod(lambda *_args, **_kwargs: [mutant]),
    )
    monkeypatch.setattr(
        MutationEngine,
        "run_mutant_in_sandbox",
        classmethod(
            lambda *_args, **_kwargs: {
                "mutant_id": "m1",
                "status": "killed",
            }
        ),
    )

    report = EngineMutationProbe(tmp_path, {"app/x.py": {2}}).probe([])

    assert report.total == 1
    assert report.killed == 1
    assert report.survived == []


def test_mutation_probe_lists_survivors(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "app" / "x.py"
    source.parent.mkdir()
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    mutant = MutantCandidate(
        mutant_id="m2",
        file_path="app/x.py",
        line_number=2,
        mutation_type="return_tamper",
        description="tamper",
        original_snippet="return 1",
        mutated_snippet="return 2",
        mutated_source="def f():\n    return 2\n",
    )
    monkeypatch.setattr(
        MutationEngine,
        "generate_mutants",
        classmethod(lambda *_args, **_kwargs: [mutant]),
    )
    monkeypatch.setattr(
        MutationEngine,
        "run_mutant_in_sandbox",
        classmethod(
            lambda *_args, **_kwargs: {
                "mutant_id": "m2",
                "status": "survived",
            }
        ),
    )

    report = EngineMutationProbe(tmp_path, {"app/x.py": {2}}).probe([])

    assert report.survived == ["m2"]
    assert report.score == 0.0
