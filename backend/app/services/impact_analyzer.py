"""
TestPilot AI — Impact Analyzer Service.

Queries the dependency graph of a repository to compute the impact radius
and find downstream files, services, and APIs affected by code changes.

Shares its traversal and classification logic with the Impact Agent so the
API endpoint and the pipeline cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dependency_graph import DependencyEdge
from app.services.dependency_graph_builder import DependencyGraphBuilder
from app.utils.path_classifier import classify_path


class ImpactAnalyzer:
    """Service to compute downstream impact of modified codebase files."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def analyze_impact(
        self,
        repository_id: str,
        repo_path: Path,
        changed_files: list[str],
        depth: int = 5,
    ) -> dict[str, Any]:
        """Determine downstream affected files from code changes.

        Args:
            repository_id: The repository UUID.
            repo_path: The local repository root path.
            changed_files: List of file paths relative to repo root.
            depth: Traversal depth limit.

        Returns:
            Dictionary containing affected modules with depth layers, APIs,
            covering tests, and impact statistics.
        """
        # Load edges from database
        result = await self.db.execute(
            select(DependencyEdge).where(DependencyEdge.repository_id == repository_id)
        )
        edges = result.scalars().all()

        edge_records = [
            {
                "source_node": e.source_node,
                "target_node": e.target_node,
                "edge_type": e.edge_type,
            }
            for e in edges
        ]

        # Build graph and traverse, keeping the distance from each change.
        builder = DependencyGraphBuilder.from_edge_records(edge_records, repo_path)
        affected_map = builder.get_affected_with_depth(changed_files, depth=depth)

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

        # Test files are verification targets, not blast radius.
        production_details = [entry for entry in impact_details if entry["kind"] != "test"]

        affected_modules = [entry["file"] for entry in production_details]
        affected_services = [
            entry["file"] for entry in production_details if entry["kind"] == "service"
        ]
        affected_apis = [entry["file"] for entry in production_details if entry["kind"] == "api"]

        affected_tests: set[str] = set()
        for target in list(changed_files) + [
            entry["file"] for entry in production_details if entry["depth"] == 1
        ]:
            if builder.is_known(target):
                affected_tests.update(builder.get_test_files_for(target))

        # Determine overall level
        impact_radius = len(affected_modules)
        if impact_radius == 0:
            level = "isolated"
        elif impact_radius < 5:
            level = "moderate"
        else:
            level = "widespread"

        return {
            "affected_modules": affected_modules,
            "affected_services": affected_services,
            "affected_apis": affected_apis,
            "affected_tests": sorted(affected_tests),
            "impact_details": impact_details,
            "impact_radius": impact_radius,
            "impact_level": level,
            "unknown_files": [path for path in changed_files if not builder.is_known(path)],
            "per_file": {key: sorted(value) for key, value in affected_map.items()},
        }
