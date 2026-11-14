"""Map the legacy ``AgentState`` onto the next-generation verify engine.

This is the seam between the main graph and the verify engine: one node builds
a ``VerifyRequest`` from state, runs the engine, and maps the ``VerifyReport``
back into the state keys the review/persistence layers already consume.
"""

from __future__ import annotations

from typing import Any

from app.agents.node_guard import guarded
from app.agents.state import AgentState, decision
from app.core.config import get_settings
from app.core.logging import get_logger
from app.verify.assembly import run_default_verify
from app.verify.contracts import (
    TestBundle,
    TestCase,
    VerifyBudget,
    VerifyReport,
    VerifyRequest,
)

logger = get_logger(__name__)

_TOOL_AGENTS = {
    "execute": "execution_agent",
    "repair_tests": "repair_agent",
    "propose_source_patch": "repair_agent",
    "probe_mutations": "mutation_agent",
}


def request_from_state(state: AgentState) -> VerifyRequest:
    """Build the engine request from the ambient pipeline state."""
    generated = []
    for test in state.get("generated_tests", []):
        name = test.get("function_name")
        path = test.get("test_file_path")
        content = test.get("content")
        if not name or not path or not content:
            continue
        generated.append(TestCase(id=str(name), path=str(path), content=str(content)))

    budget = state.get("budget") or {}

    def _limit(key: str, default: int) -> int:
        value = budget.get(key)
        return default if value is None else int(value)

    return VerifyRequest(
        workspace=str(state.get("workspace_path") or ""),
        bundle=TestBundle(
            generated=generated,
            existing=[str(path) for path in state.get("affected_tests", [])],
        ),
        budget=VerifyBudget(
            max_rounds=_limit("max_rounds", 3),
            max_repairs=_limit("max_repairs", 2),
            max_mutations=_limit("max_mutation_attempts", 1),
            max_steps=get_settings().verify_max_steps,
        ),
    )


def source_files_from_state(state: AgentState) -> dict[str, set[int]]:
    """Changed Python files and their changed lines, for mutation probing."""
    files: dict[str, set[int]] = {}
    for changed in state.get("changed_files", []):
        path = str(changed.get("path", ""))
        if path.endswith(".py") and changed.get("status") != "deleted":
            files[path] = set(changed.get("changed_lines") or [])
    return files


def apply_report(
    state: AgentState,
    request: VerifyRequest,
    report: VerifyReport,
) -> dict[str, Any]:
    """Translate the report into the legacy state keys."""
    last_run = report.runs[-1] if report.runs else None
    execution_results = {
        "runner": request.runner,
        "status": last_run.status if last_run else "error",
        "total": last_run.total if last_run else 0,
        "passed": last_run.passed if last_run else 0,
        "failed": last_run.failed if last_run else 0,
        "skipped": last_run.skipped if last_run else 0,
        "duration_seconds": last_run.duration_seconds if last_run else 0.0,
        "coverage_percentage": None,
        "failed_tests": [
            {"name": failure.test_id, "message": failure.message}
            for failure in (last_run.failures if last_run else [])
        ],
        "logs": "",
    }

    repaired = {test.id: test.content for test in request.bundle.generated}
    generated_tests = []
    applied_test_patches = 0
    for test in state.get("generated_tests", []):
        updated = dict(test)
        content = repaired.get(str(test.get("function_name")))
        if content and content != test.get("content"):
            updated["content"] = content
            applied_test_patches += 1
        generated_tests.append(updated)

    failure_analyses = [
        {
            "test_name": failure.test_id,
            "failure_message": failure.message,
            "root_cause": "reported by verify engine",
            "affected_code_path": None,
            "suggested_fix": "",
            "failure_origin": failure.origin,
            "confidence": 0.0,
        }
        for failure in report.failures
    ]

    repair_patches = [
        {
            "target_type": "source_code",
            "file_path": proposal.file,
            "original_snippet": "",
            "repaired_code": proposal.diff,
            "patch_diff": proposal.diff,
            "explanation": proposal.rationale,
            "verified": False,
        }
        for proposal in report.proposals
    ]

    mutation_results = [
        {"mutant_id": mutant_id, "status": "survived"} for mutant_id in report.mutations.survived
    ]
    completed = []
    for call in report.ledger:
        if call.status != "ok":
            continue
        agent = _TOOL_AGENTS.get(call.tool)
        if agent and agent not in completed:
            completed.append(agent)

    budget = dict(state.get("budget") or {})
    budget["rounds"] = report.rounds

    return {
        "execution_results": execution_results,
        "execution_scope": "selected",
        "selected_test_count": len(request.bundle.existing) + len(request.bundle.generated),
        "generated_tests": generated_tests,
        "failure_analyses": failure_analyses,
        "repair_patches": repair_patches,
        "repair_applied_tests": applied_test_patches,
        "mutation_results": mutation_results,
        "survived_mutants": mutation_results,
        "mutation_score": report.mutations.score if report.mutations.total else None,
        "is_auto_remediated": bool(applied_test_patches or repair_patches),
        "verification": {
            "exit_reason": report.exit_reason.value,
            "engine": "next",
            "rounds": report.rounds,
            "generated": len(request.bundle.generated),
            "executed": last_run.total if last_run else 0,
            "passed": last_run.passed if last_run else 0,
            "failed": last_run.failed if last_run else 0,
            "mutation_score": report.mutations.score if report.mutations.total else None,
            "repaired": bool(applied_test_patches or repair_patches),
        },
        "budget": budget,
        "completed_agents": completed,
        "decisions": [
            decision(
                "verify",
                report.exit_reason.value,
                f"next engine: {report.verdict.value} after {report.rounds} round(s)",
                inputs={
                    "rounds": report.rounds,
                    "runs": len(report.runs),
                    "proposals": len(report.proposals),
                    "survived": len(report.mutations.survived),
                },
            )
        ],
    }


def run_next_verify(state: AgentState) -> dict[str, Any]:
    """Run the next engine for this state and map its report back."""
    request = request_from_state(state)
    if not request.workspace:
        return {"errors": ["verify_next: no workspace_path in state"]}
    report = run_default_verify(
        request,
        source_files=source_files_from_state(state),
    )
    return apply_report(state, request, report)


def build_next_verify_graph():
    """A one-node subgraph that runs the next engine inside the parent graph."""
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(AgentState)
    graph.add_node("next_verify", guarded("next_verify", run_next_verify))
    graph.add_edge(START, "next_verify")
    graph.add_edge("next_verify", END)
    return graph
