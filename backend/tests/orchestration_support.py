"""Shared node stubs for graph tests.

The stubs replace every agent with a deterministic, side-effect-free function so
tests can exercise topology, channel merging, routing and checkpoint resume
without a repository checkout, Qdrant, an LLM or a database.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.agents import context_graph as context_module
from app.agents import graph as graph_module
from app.agents.state import AgentState


def ok_node(name: str, **updates: Any):
    """A stub node that returns ``updates`` plus its completion marker."""

    def node(state: dict[str, Any]) -> dict[str, Any]:
        return {**updates, "completed_agents": [name]}

    return node


def stub_topology(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: dict[str, int] | None = None,
    flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace every node in ``app.agents.graph`` with a stub.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        calls: Optional counters, incremented per node invocation.
        flags: Optional switches. ``fail_execution`` makes the execution stub
            raise once, simulating a worker crash mid-pipeline.

    Returns:
        The ``flags`` dict, so callers can flip switches between attempts.
    """
    calls = calls if calls is not None else {}
    flags = flags if flags is not None else {}

    def counted(name: str, **updates: Any):
        node = ok_node(name, **updates)

        def wrapped(state: dict[str, Any]) -> dict[str, Any]:
            calls[name] = calls.get(name, 0) + 1
            return node(state)

        return wrapped

    def execution(state: dict[str, Any]) -> dict[str, Any]:
        calls["execution_agent"] = calls.get("execution_agent", 0) + 1
        budget = dict(state.get("budget") or {})
        rounds = int(budget.get("rounds", state.get("verify_rounds", 0)) or 0) + 1
        budget["rounds"] = rounds
        if flags.pop("crash_execution", False):
            # BaseException bypasses the node guard, like a killed worker.
            raise KeyboardInterrupt("worker killed")
        if flags.pop("fail_execution", False):
            raise RuntimeError("sandbox crashed")
        if flags.pop("raise_execution", False):
            raise RuntimeError("tool failed")
        failing = flags.get("fail_tests", False)
        signature = hashlib.sha1(b"test_charge::boom").hexdigest()[:16] if failing else ""
        previous = state.get("failure_signature", "")
        return {
            "budget": budget,
            "execution_results": {
                "runner": "pytest",
                "status": "failed" if failing else "passed",
                "total": 1,
                "passed": 0 if failing else 1,
                "failed": 1 if failing else 0,
                "skipped": 0,
                "failed_tests": [{"name": "test_charge", "message": "boom"}] if failing else [],
            },
            "previous_failure_signature": previous,
            "failure_signature": signature,
            "execution_scope": "selected",
            "completed_agents": ["execution_agent"],
        }

    def impact(state: dict[str, Any]) -> dict[str, Any]:
        calls["impact_agent"] = calls.get("impact_agent", 0) + 1
        affected = [] if flags.get("no_affected_tests") else ["tests/test_billing.py"]
        return {
            "impact_level": "moderate",
            "impact_radius": 1,
            "affected_modules": ["app/api/router.py"],
            "affected_tests": affected,
            "warnings": ["impact_agent: stub warning"],
            "completed_agents": ["impact_agent"],
        }

    def failure_analysis(state: dict[str, Any]) -> dict[str, Any]:
        calls["failure_analysis_agent"] = calls.get("failure_analysis_agent", 0) + 1
        if flags.get("test_defects"):
            analyses = [
                {
                    "test_name": "test_charge",
                    "failure_message": "boom",
                    "root_cause": "wrong assumption",
                    "affected_code_path": "tests/test_billing.py",
                    "suggested_fix": "use charge()",
                    "failure_origin": "test_defect",
                    "confidence": 0.9,
                }
            ]
        else:
            analyses = []
        return {
            "failure_analyses": analyses,
            "completed_agents": ["failure_analysis_agent"],
        }

    def repair(state: dict[str, Any]) -> dict[str, Any]:
        calls["repair_agent"] = calls.get("repair_agent", 0) + 1
        budget = dict(state.get("budget") or {})
        budget["repair_attempts"] = int(budget.get("repair_attempts", 0) or 0) + 1
        patches: list[dict[str, Any]] = []
        applied = 0
        if flags.get("source_only_repair"):
            patches.append(
                {
                    "target_type": "source_code",
                    "file_path": "app/services/billing.py",
                    "original_snippet": "return amount",
                    "repaired_code": "return amount * 2",
                    "patch_diff": "",
                    "explanation": "stub",
                    "verified": False,
                }
            )
        return {
            "repair_patches": patches,
            "repair_applied_tests": applied,
            "budget": budget,
            "completed_agents": ["repair_agent"],
        }

    def generator(state: dict[str, Any]) -> dict[str, Any]:
        calls["test_generator_agent"] = calls.get("test_generator_agent", 0) + 1
        if flags.get("no_tests"):
            return {
                "generated_tests": [],
                "decisions": [
                    {
                        "agent": "test_generator_agent",
                        "choice": "decline",
                        "reason": "stub: no tests",
                        "at": "2026-01-01T00:00:00+00:00",
                    }
                ],
                "completed_agents": ["test_generator_agent"],
            }
        budget = dict(state.get("budget") or {})
        budget["generation_attempts"] = int(budget.get("generation_attempts", 0) or 0) + 1
        return {
            "generated_tests": [
                {
                    "function_name": "test_charge",
                    "file_path": "app/services/billing.py",
                    "test_file_path": "tests/test_billing.py",
                    "content": "def test_charge():\n    assert True\n",
                }
            ],
            "budget": budget,
            "decisions": [
                {
                    "agent": "test_generator_agent",
                    "choice": "generate",
                    "reason": "1 stub test",
                    "inputs": {"generated": 1},
                    "attempt": 1,
                    "at": "2026-01-01T00:00:00+00:00",
                }
            ],
            "completed_agents": ["test_generator_agent"],
        }

    def mutation(state: dict[str, Any]) -> dict[str, Any]:
        calls["mutation_agent"] = calls.get("mutation_agent", 0) + 1
        budget = dict(state.get("budget") or {})
        attempts = int(budget.get("mutation_attempts", 0) or 0) + 1
        budget["mutation_attempts"] = attempts
        if flags.get("mutants_survive"):
            survived = [{"mutant_id": "m1", "status": "survived"}]
            return {
                "mutation_results": survived,
                "mutation_score": 0.0,
                "survived_mutants": survived,
                "budget": budget,
                "completed_agents": ["mutation_agent"],
            }
        return {
            "mutation_results": [],
            "mutation_score": 1.0,
            "survived_mutants": [],
            "budget": budget,
            "completed_agents": ["mutation_agent"],
        }

    stubs = {
        "planner_agent_node": counted("planner_agent"),
        "diff_agent_node": counted(
            "diff_agent",
            changed_files=[
                {"path": "app/services/billing.py", "status": "modified", "changed_lines": [3]}
            ],
            changed_nodes=[
                {
                    "name": "charge",
                    "type": "function",
                    "file_path": "app/services/billing.py",
                    "line_start": 1,
                    "line_end": 5,
                    "language": "python",
                }
            ],
            change_contracts=[{"symbol": "charge", "file_path": "app/services/billing.py"}],
        ),
        "dependency_agent_node": counted("dependency_agent", dependency_graph_edges=[]),
        # The three collectors all write the shared append-only channels.
        "impact_agent_node": impact,
        "search_agent_node": counted(
            "search_agent",
            retrieved_context=[{"file_path": "app/services/billing.py", "content": "x" * 300}],
            warnings=["search_agent: stub warning"],
        ),
        "test_discovery_agent_node": counted(
            "test_discovery_agent",
            test_coverage_map={"app/services/billing.py": ["tests/test_billing.py"]},
            uncovered_modules=[],
            test_config={"total_test_files": 1},
            warnings=["test_discovery_agent: stub warning"],
        ),
        "risk_ranker_agent_node": counted(
            "risk_ranker_agent",
            test_targets=[{"file": "app/services/billing.py", "priority": "critical"}],
        ),
        "context_controller_agent_node": counted(
            "context_controller_agent",
            context_health={"mode": "full", "checks": {"contracts": True}, "score": 100},
            generation_plan={
                "declined": False,
                "decline_reason": None,
                "context_mode": "full",
                "context_score": 100,
                "targets": [{"file": "app/services/billing.py", "priority": "critical"}],
            },
        ),
        "test_generator_agent_node": generator,
        "execution_agent_node": execution,
        "mutation_agent_node": mutation,
        "failure_analysis_agent_node": failure_analysis,
        "repair_agent_node": repair,
        "review_agent_node": counted(
            "review_agent",
            risk_score={"level": "low", "score": 1.0, "factors": []},
            review={"full_review_body": "ok"},
        ),
        "documentation_agent_node": counted("documentation_agent", documentation_updates=[]),
    }

    for name, stub in stubs.items():
        if hasattr(graph_module, name):
            monkeypatch.setattr(graph_module, name, stub)
        if hasattr(context_module, name):
            monkeypatch.setattr(context_module, name, stub)

    # The verify phase is the next engine; stub its subgraph so graph tests do
    # not need a sandbox or LLM.
    monkeypatch.setattr(graph_module, "build_verify_graph", _stub_verify_graph)

    return flags


def _stub_verify_graph():
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(AgentState)

    def verify(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "verification": {
                "exit_reason": "green",
                "engine": "next",
                "rounds": 0,
                "generated": 0,
                "executed": 0,
                "passed": 0,
                "failed": 0,
                "mutation_score": None,
                "repaired": False,
            }
        }

    graph.add_node("verify", verify)
    graph.add_edge(START, "verify")
    graph.add_edge("verify", END)
    return graph


def initial_state() -> dict[str, Any]:
    """Minimal valid input state for a stubbed run."""
    return {
        "pr_id": "pr-1",
        "repository_id": "repo-1",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "budget": {
            "max_rounds": 3,
            "max_generation_attempts": 2,
            "max_coverage_topups": 1,
            "max_repairs": 2,
            "max_mutation_attempts": 1,
        },
        "errors": [],
        "warnings": [],
        "decisions": [],
        "completed_agents": [],
    }
