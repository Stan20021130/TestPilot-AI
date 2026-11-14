"""Service adapters for the verify engine.

The engine's tools are deliberately dumb: ``repair_tests`` asks a service for
test patches, ``propose_source_patch`` asks for proposals, and
``probe_mutations`` wraps the existing mutation engine. None of these services
see ``AgentState``; their contracts are the verify types.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.mutation_engine import MutantCandidate, MutationEngine
from app.verify.contracts import (
    FailureFact,
    MutationReport,
    PatchProposal,
    TestBundle,
    TestPatch,
)

logger = get_logger(__name__)


class BundleTestEditor:
    """Apply a test patch to the mutable bundle used by the runner.

    The runner writes the bundle's current content before every execution, so
    an editor that only recorded patches would make repair a no-op. This is the
    in-memory implementation used by the engine; a future editor could write a
    database row instead.
    """

    def __init__(self, bundle: TestBundle) -> None:
        self.bundle = bundle

    def apply(self, patch: TestPatch) -> None:
        for test in self.bundle.generated:
            if test.id == patch.test_id:
                test.content = patch.replacement
                return
        logger.warning("Test patch references an unknown test", test_id=patch.test_id)


class RepairItem(BaseModel):
    test_id: str = Field(description="Id of the generated test to replace")
    replacement: str = Field(description="Complete new content of that test file")
    rationale: str = ""


class RepairPlan(BaseModel):
    patches: list[RepairItem] = Field(default_factory=list)


class PatchItem(BaseModel):
    file: str = Field(description="Repository-relative source file")
    diff: str = Field(description="Unified diff or exact replacement snippet")
    rationale: str = ""


class PatchPlan(BaseModel):
    proposals: list[PatchItem] = Field(default_factory=list)


class LLMRepairService:
    """Synthesize test-only repairs from failures and the generated bundle."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import instructor
            import litellm

            self._client = instructor.from_litellm(litellm.completion)
        return self._client

    def repair(
        self,
        failures: Sequence[FailureFact],
        bundle: TestBundle,
    ) -> list[TestPatch]:
        if not failures or not bundle.generated:
            return []

        known = {test.id: test for test in bundle.generated}
        blocks = []
        for failure in failures[:5]:
            test = known.get(failure.test_id)
            if test is None:
                test = next(
                    (
                        candidate
                        for candidate in bundle.generated
                        if candidate.id in failure.test_id
                        or failure.test_id.endswith(f"::{candidate.id}")
                    ),
                    None,
                )
            blocks.append(
                f"### Failure: {failure.test_id}\n"
                f"- message: {failure.message[:1500]}\n"
                f"- current test content:\n{(test.content if test else 'not found')[:3000]}"
            )

        prompt = (
            "You repair generated tests, never source code. "
            "Return a complete replacement file for each failing test. "
            "Keep the imports and fixtures; change only what makes the test wrong. "
            "If a failure is a real product defect, return no patch for it.\n\n"
            + "\n\n".join(blocks)
        )
        try:
            plan = self._get_client().chat.completions.create(
                model=get_settings().litellm_default_model,
                response_model=RepairPlan,
                messages=[
                    {
                        "role": "system",
                        "content": "Return only test patches for known test ids.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001 - a failed repair is a no-op, not a crash
            logger.warning("LLM repair failed", error=str(exc))
            return []

        patches: list[TestPatch] = []
        for item in plan.patches:
            if item.test_id not in known:
                logger.warning("Repair service returned an unknown test id", test_id=item.test_id)
                continue
            patches.append(
                TestPatch(
                    test_id=item.test_id,
                    replacement=item.replacement,
                    rationale=item.rationale,
                )
            )
        return patches


class LLMPatchService:
    """Propose source patches for review; never apply them."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import instructor
            import litellm

            self._client = instructor.from_litellm(litellm.completion)
        return self._client

    def propose(self, failures: Sequence[FailureFact]) -> list[PatchProposal]:
        if not failures:
            return []
        prompt = (
            "These tests fail. Propose a minimal source-code fix for review. "
            "Do not edit tests; do not apply anything. Return repository-relative "
            "files and a unified diff.\n\n"
            + "\n\n".join(
                f"### {failure.test_id}\n{failure.message[:2000]}" for failure in failures[:5]
            )
        )
        try:
            plan = self._get_client().chat.completions.create(
                model=get_settings().litellm_default_model,
                response_model=PatchPlan,
                messages=[
                    {
                        "role": "system",
                        "content": "Return source patch proposals only; never test edits.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001 - proposal failure is a no-op
            logger.warning("LLM patch proposal failed", error=str(exc))
            return []

        proposals: list[PatchProposal] = []
        for item in plan.proposals:
            relative = Path(item.file)
            if relative.is_absolute() or relative.anchor or ".." in relative.parts:
                logger.warning("Refusing patch proposal outside the repository", file=item.file)
                continue
            proposals.append(
                PatchProposal(file=item.file, diff=item.diff, rationale=item.rationale)
            )
        return proposals


class EngineMutationProbe:
    """Wrap the existing AST mutation engine as a ``MutationProbe``."""

    def __init__(
        self,
        repo_root: Path,
        source_files: dict[str, set[int]] | None = None,
        *,
        test_command: list[str] | None = None,
        max_mutants: int = 5,
        timeout: int = 45,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.source_files = source_files or {}
        self.test_command = test_command or [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=line",
            "-W",
            "ignore::pytest.PytestDeprecationWarning",
        ]
        self.max_mutants = max_mutants
        self.timeout = timeout

    def probe(self, targets: Sequence[str]) -> MutationReport:
        # Mutation must target source files, never the test files in ``targets``.
        # Without configured source files the probe is a no-op.
        files = list(self.source_files)
        if not files:
            return MutationReport()
        mutants: list[MutantCandidate] = []
        for relative in files[:5]:
            path = self.repo_root / relative
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            mutants.extend(
                MutationEngine.generate_mutants(
                    source_code=source,
                    file_path=relative,
                    changed_lines=self.source_files.get(relative),
                    max_mutants=max(1, self.max_mutants - len(mutants)),
                )
            )
            if len(mutants) >= self.max_mutants:
                break

        results = [
            MutationEngine.run_mutant_in_sandbox(
                repo_root=self.repo_root,
                mutant=mutant,
                test_command=self.test_command,
                timeout=self.timeout,
            )
            for mutant in mutants[: self.max_mutants]
        ]
        killed = sum(1 for result in results if result.get("status") == "killed")
        survived = [
            str(result.get("mutant_id")) for result in results if result.get("status") == "survived"
        ]
        return MutationReport(total=len(results), killed=killed, survived=survived)
