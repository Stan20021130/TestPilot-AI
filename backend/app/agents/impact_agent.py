"""TestPilot AI — Impact Agent.

Traverses the dependency graph to determine which modules, services, APIs, and
downstream dependencies are affected by the PR changes.

The deliverable is deliberately *layered* rather than a flat path list:

- ``affected_modules``      flat union (backward compatible)
- ``impact_details``        one entry per file with traversal depth and kind
- ``affected_services`` / ``affected_apis`` derived from path structure, not
  substring matching on the whole path
- ``affected_tests``        existing test files that cover the blast radius
- ``impact_unknown_files``  changed files absent from the graph — these are
  "impact unknown", which is *not* the same as "no impact" and is reported
  separately so the review never claims a safe blast radius it cannot prove.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.agents.state import AgentState
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.dependency_graph_builder import DependencyGraphBuilder
from app.services.pr_checkout import resolve_repo_root
from app.utils.path_classifier import classify_path

logger = get_logger(__name__)
settings = get_settings()

# Reverse-import traversal depth. 5 covers router -> service -> repository ->
# model -> util chains, which is the deepest realistic Python/TS layering.
MAX_IMPACT_DEPTH = 5

# How many changed/direct dependents to scan for covering test files. The scan
# is O(graph nodes) per file, so it is capped to keep the agent sub-second.
MAX_TEST_SCAN_TARGETS = 25


def _paths_of_kind(details: list[dict[str, Any]], kind: str) -> list[str]:
    return [entry["file"] for entry in details if entry["kind"] == kind]


def _impact_level(radius: int, graph_size: int) -> str:
    if graph_size == 0:
        return "unknown"
    if radius == 0:
        return "isolated"
    if radius < 5:
        return "moderate"
    return "widespread"


def impact_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: determine impact radius of PR changes.

    Takes the dependency graph edges and changed files, then performs a
    reverse BFS to find all transitively affected modules — keeping the
    distance from the change so reviewers can tell direct from transitive
    impact.

    Args:
        state: Current AgentState with dependency_graph_edges and changed_files.

    Returns:
        Partial state with affected_modules, impact_details, impact_radius,
        impact_level, affected_services, affected_apis, affected_tests and
        impact_unknown_files.
    """
    start_time = time.monotonic()
    logger.info("Impact agent started", pr_id=state.get("pr_id"))

    warnings: list[str] = []

    try:
        repo_root = resolve_repo_root(state)

        builder = DependencyGraphBuilder.from_edge_records(
            records=state.get("dependency_graph_edges", []),
            repo_root=Path(repo_root),
        )

        changed_file_paths = [
            f["path"] for f in state.get("changed_files", []) if f.get("status") != "deleted"
        ]

        graph_size = builder.graph.number_of_nodes()
        if graph_size == 0:
            warnings.append(
                "impact_agent: dependency graph is empty for this repository — "
                "blast radius is unknown, not zero"
            )

        unknown_files = [path for path in changed_file_paths if not builder.is_known(path)]
        if unknown_files:
            warnings.append(
                f"impact_agent: {len(unknown_files)} changed file(s) are absent from the "
                f"dependency graph; impact unknown for {', '.join(unknown_files[:5])}"
            )

        # Reverse BFS with distance preserved per changed file.
        affected_map = builder.get_affected_with_depth(changed_file_paths, depth=MAX_IMPACT_DEPTH)

        depth_by_file: dict[str, int] = {}
        for dependents in affected_map.values():
            for path, distance in dependents.items():
                existing = depth_by_file.get(path)
                if existing is None or distance < existing:
                    depth_by_file[path] = distance

        impact_details = [
            {"file": path, "depth": distance, "kind": classify_path(path)}
            for path, distance in sorted(depth_by_file.items(), key=lambda item: (item[1], item[0]))
        ]

        # Test files are verification targets, not blast radius: they are
        # reported through affected_tests instead of inflating the radius.
        production_details = [entry for entry in impact_details if entry["kind"] != "test"]

        affected_modules = [entry["file"] for entry in production_details]
        affected_services = _paths_of_kind(production_details, "service")
        affected_apis = _paths_of_kind(production_details, "api")

        # Existing tests that exercise the change or its direct dependents.
        scan_targets = changed_file_paths + [
            entry["file"] for entry in production_details if entry["depth"] == 1
        ]
        affected_tests: set[str] = set()
        for target in scan_targets[:MAX_TEST_SCAN_TARGETS]:
            if builder.is_known(target):
                affected_tests.update(builder.get_test_files_for(target))

        impact_radius = len(affected_modules)
        impact_level = _impact_level(impact_radius, graph_size)

        duration = time.monotonic() - start_time
        logger.info(
            "Impact agent completed",
            affected_modules=impact_radius,
            direct=sum(1 for entry in impact_details if entry["depth"] == 1),
            transitive=sum(1 for entry in impact_details if entry["depth"] > 1),
            affected_tests=len(affected_tests),
            unknown_files=len(unknown_files),
            impact_level=impact_level,
            duration_ms=int(duration * 1000),
        )

        return {
            "affected_modules": affected_modules,
            "impact_details": impact_details,
            "impact_radius": impact_radius,
            "impact_level": impact_level,
            "affected_services": affected_services,
            "affected_apis": affected_apis,
            "affected_tests": sorted(affected_tests),
            "impact_unknown_files": unknown_files,
            "warnings": warnings,
            "completed_agents": ["impact_agent"],
        }

    except Exception as e:
        logger.exception("Impact agent failed", error=str(e))
        return {
            "errors": [f"impact_agent: {e}"],
            "affected_modules": [],
            "impact_details": [],
            "impact_radius": 0,
            "impact_level": "unknown",
            "affected_services": [],
            "affected_apis": [],
            "affected_tests": [],
            "impact_unknown_files": [
                f["path"] for f in state.get("changed_files", []) if f.get("status") != "deleted"
            ],
        }
