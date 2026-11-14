"""Assemble the real verify engine from its default adapters."""

from __future__ import annotations

from pathlib import Path

from app.verify.contracts import VerifyReport, VerifyRequest
from app.verify.controller import Decider, run_verify
from app.verify.runners import SandboxTestRunner
from app.verify.services import (
    BundleTestEditor,
    EngineMutationProbe,
    LLMPatchService,
    LLMRepairService,
)


def run_default_verify(
    request: VerifyRequest,
    *,
    source_files: dict[str, set[int]] | None = None,
    decider: Decider | None = None,
    test_command: list[str] | None = None,
) -> VerifyReport:
    """Run the next-gen engine with sandbox runner and LLM services.

    This is the production entry point; tests inject fake deciders/services
    through :func:`app.verify.controller.run_verify` instead.
    """
    repo_path = Path(request.workspace)
    return run_verify(
        request,
        runner=SandboxTestRunner(repo_path),
        repairer=LLMRepairService(),
        editor=BundleTestEditor(request.bundle),
        proposer=LLMPatchService(),
        probe=EngineMutationProbe(repo_path, source_files or {}, test_command=test_command),
        decider=decider,
    )
