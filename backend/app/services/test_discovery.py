"""
TestPilot AI — Test Discovery Service.

Identifies test files, framework usages, and test case counts in a codebase,
and resolves which source files are actually covered by an existing test.

Used by the ``/tests/discover`` API endpoint; the LangGraph Test Discovery
Agent owns the same helpers so the endpoint and the pipeline cannot disagree
about what "covered" means.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.agents.test_discovery_agent import (
    TEST_FILE_PATTERNS,
    _detect_test_config,
    _discover_test_files,
    _map_tests_to_sources,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.dependency_graph_builder import DependencyGraphBuilder

logger = get_logger(__name__)

# Source files considered when reporting coverage gaps.
SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".go",
    ".rb",
    ".cs",
    ".rs",
    ".kt",
}
MAX_SOURCE_FILES = 5000
MAX_REPORTED_GAPS = 200


class TestDiscovery:
    """Service to discover and analyze tests in a workspace."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path

    def discover(self) -> dict[str, Any]:
        """Scan directory and return structured list of tests.

        Returns:
            Dictionary with counts and test suite statistics.
        """
        test_files = _discover_test_files(self.repo_path)

        return {
            "total_test_files": len(test_files),
            "total_tests": sum(tf["test_count"] for tf in test_files),
            "frameworks_detected": sorted({tf["framework"] for tf in test_files}),
            "test_files": [self._serialize(tf) for tf in test_files],
        }

    def discover_with_coverage(
        self,
        edge_records: list[dict[str, str]] | None = None,
        changed_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Discover tests and resolve real coverage gaps.

        Args:
            edge_records: Persisted dependency-graph edges, used to resolve
                ``test -> source`` import relationships.
            changed_files: Optional changed paths to report coverage for.

        Returns:
            Discovery report including ``uncovered_modules`` (source files with
            no covering test), ``changed_coverage`` and the detected
            ``test_config``.
        """
        if not self.repo_path.exists():
            logger.warning("Repository checkout missing", path=str(self.repo_path))

        test_files = _discover_test_files(self.repo_path)
        builder = DependencyGraphBuilder.from_edge_records(
            records=edge_records or [], repo_root=self.repo_path
        )
        coverage_map = _map_tests_to_sources(test_files, builder)

        covered = set(coverage_map)
        source_files = self._iter_source_files()
        uncovered = sorted(path for path in source_files if path not in covered)

        return {
            "total_test_files": len(test_files),
            "total_tests": sum(tf["test_count"] for tf in test_files),
            "frameworks_detected": sorted({tf["framework"] for tf in test_files}),
            "test_files": [self._serialize(tf) for tf in test_files],
            "uncovered_modules": uncovered[:MAX_REPORTED_GAPS],
            "coverage_gaps": uncovered[:20],
            "coverage_map": coverage_map,
            "changed_coverage": {
                path: coverage_map.get(path, []) for path in (changed_files or [])
            },
            "test_config": _detect_test_config(self.repo_path, test_files),
        }

    def _iter_source_files(self) -> list[str]:
        """List repo-relative source files, excluding tests and vendored dirs."""
        if not self.repo_path.exists():
            return []

        settings = get_settings()
        test_patterns = [re.compile(pattern) for pattern in TEST_FILE_PATTERNS]
        found: list[str] = []

        for file_path in self.repo_path.rglob("*"):
            if len(found) >= MAX_SOURCE_FILES:
                logger.warning("Source scan truncated", limit=MAX_SOURCE_FILES)
                break
            if not file_path.is_file() or file_path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue

            try:
                relative_parts = file_path.relative_to(self.repo_path).parts
            except ValueError:
                continue
            if any(part in settings.ignored_directories for part in relative_parts):
                continue

            relative_path = str(file_path.relative_to(self.repo_path)).replace("\\", "/")
            if any(pattern.search(relative_path) for pattern in test_patterns):
                continue

            found.append(relative_path)

        return sorted(found)

    @staticmethod
    def _serialize(test_file: dict[str, Any]) -> dict[str, Any]:
        return {
            "file_path": test_file["path"],
            "framework": test_file["framework"],
            "test_count": test_file["test_count"],
            "test_names": test_file["test_names"],
            "covers_modules": test_file["covers_modules"],
        }
