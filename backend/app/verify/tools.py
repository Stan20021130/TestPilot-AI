"""Verify tools: capabilities, not graph nodes.

A tool declares its argument model, its side effects and a runner. The
controller validates the arguments and dispatches; the tool performs exactly
one capability. Repair and source-patch synthesis are services behind the
tools, so a tool does not need the whole run state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.verify.contracts import (
    FailureFact,
    MutationReport,
    PatchProposal,
    RunResult,
    TestBundle,
    TestPatch,
    VerifyRequest,
)


class UnknownTool(LookupError):
    """Raised when the decider names a tool that is not registered."""


class TestRunner(Protocol):
    def run(self, request: VerifyRequest) -> RunResult: ...


class RepairService(Protocol):
    def repair(
        self,
        failures: Sequence[FailureFact],
        bundle: TestBundle,
    ) -> list[TestPatch]: ...


class TestEditor(Protocol):
    def apply(self, patch: TestPatch) -> None: ...


class PatchService(Protocol):
    def propose(self, failures: Sequence[FailureFact]) -> list[PatchProposal]: ...


class MutationProbe(Protocol):
    def probe(self, targets: Sequence[str]) -> MutationReport: ...


class ExecuteArgs(BaseModel):
    scope: Literal["selected", "full"] = "selected"


class RepairArgs(BaseModel):
    test_ids: list[str] = Field(default_factory=list)


class ProposeArgs(BaseModel):
    test_ids: list[str] = Field(default_factory=list)


class ProbeArgs(BaseModel):
    targets: list[str] = Field(default_factory=list)


@dataclass
class ToolOutcome:
    """What one tool call changed; the controller owns state mutation."""

    summary: str
    run_result: RunResult | None = None
    patches: list[TestPatch] = field(default_factory=list)
    proposals: list[PatchProposal] = field(default_factory=list)
    mutation: MutationReport | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    effects: frozenset[str]
    run: Callable[[VerifyRequest, Sequence[FailureFact], BaseModel], ToolOutcome]

    def args_digest(self, args: BaseModel) -> str:
        return f"{self.name}:{args.model_dump_json()}"


class ToolRegistry:
    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def names(self) -> list[str]:
        return list(self._specs)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise UnknownTool(f"unknown verify tool {name!r}") from exc

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "args": spec.args_model.model_json_schema(),
                "effects": sorted(spec.effects),
            }
            for spec in self._specs.values()
        ]


def build_default_registry(
    *,
    runner: TestRunner,
    repairer: RepairService,
    editor: TestEditor,
    proposer: PatchService,
    probe: MutationProbe,
) -> ToolRegistry:
    """The four verify capabilities; ``finish`` is controller control flow."""

    def execute(
        request: VerifyRequest,
        failures: Sequence[FailureFact],
        args: BaseModel,
    ) -> ToolOutcome:
        result = runner.run(request)
        return ToolOutcome(
            summary=f"run {result.status} ({result.passed}/{result.total})", run_result=result
        )

    def repair_tests(
        request: VerifyRequest,
        failures: Sequence[FailureFact],
        args: BaseModel,
    ) -> ToolOutcome:
        parsed = RepairArgs.model_validate(args)
        selected = [
            failure
            for failure in failures
            if not parsed.test_ids or failure.test_id in parsed.test_ids
        ]
        patches = repairer.repair(selected, request.bundle)
        for patch in patches:
            editor.apply(patch)
        return ToolOutcome(
            summary=f"applied {len(patches)} test patch(es)",
            patches=patches,
        )

    def propose_source_patch(
        request: VerifyRequest,
        failures: Sequence[FailureFact],
        args: BaseModel,
    ) -> ToolOutcome:
        proposals = proposer.propose(failures)
        return ToolOutcome(
            summary=f"proposed {len(proposals)} source patch(es)",
            proposals=proposals,
        )

    def probe_mutations(
        request: VerifyRequest,
        failures: Sequence[FailureFact],
        args: BaseModel,
    ) -> ToolOutcome:
        parsed = ProbeArgs.model_validate(args)
        targets = parsed.targets or request.bundle.paths()
        report = probe.probe(targets)
        return ToolOutcome(
            summary=f"{report.killed}/{report.total} mutant(s) killed",
            mutation=report,
        )

    return ToolRegistry(
        [
            ToolSpec(
                name="execute",
                description="Run the selected test bundle in the sandbox.",
                args_model=ExecuteArgs,
                effects=frozenset({"sandbox"}),
                run=execute,
            ),
            ToolSpec(
                name="repair_tests",
                description="Repair generated tests for the current failures.",
                args_model=RepairArgs,
                effects=frozenset({"workspace"}),
                run=repair_tests,
            ),
            ToolSpec(
                name="propose_source_patch",
                description="Propose a source patch for review; never applies it.",
                args_model=ProposeArgs,
                effects=frozenset({"proposal"}),
                run=propose_source_patch,
            ),
            ToolSpec(
                name="probe_mutations",
                description="Inject mutants and measure whether tests kill them.",
                args_model=ProbeArgs,
                effects=frozenset({"sandbox"}),
                run=probe_mutations,
            ),
        ]
    )
