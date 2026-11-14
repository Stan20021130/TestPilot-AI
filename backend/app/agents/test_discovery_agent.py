"""TestPilot AI — Test Discovery Agent.

Scans the repository to find existing test files, understand test coverage, and
identify which modules lack adequate test coverage.

Deliverables are shaped for the Test Generator and the sandbox:
- ``existing_tests``      test files with framework, names and covered modules
- ``test_coverage_map``   changed source file -> test files that cover it
- ``uncovered_modules``   changed files with no covering test at all
- ``test_config``         detected framework, test command and config files

Coverage is derived from the dependency graph (test -> source import edges plus
naming conventions) rather than filename stemming alone, so
``tests/test_api_auth.py`` is recognised as covering ``app/api/v1/auth.py``.

Supports: pytest, jest, vitest, junit, go test, mocha, rspec, nunit.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from app.agents.state import AgentState, TestFile
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.dependency_graph_builder import DependencyGraphBuilder
from app.services.pr_checkout import resolve_repo_root

logger = get_logger(__name__)
settings = get_settings()

# Patterns to identify test frameworks
FRAMEWORK_PATTERNS = {
    "pytest": [r"import pytest", r"from pytest", r"def test_", r"@pytest"],
    "jest": [r"describe\(", r"it\(", r"test\(", r"expect\(", r"jest\."],
    "vitest": [r"from ['\"]vitest['\"]", r"import \{.*\} from ['\"]vitest['\"]"],
    "junit": [r"@Test", r"@RunWith", r"import org\.junit"],
    "go_test": [r"func Test", r"testing\.T"],
    "mocha": [r"describe\(", r"it\(", r"require\('mocha'\)"],
    "rspec": [r"describe ", r"it ", r"expect\(", r"require 'spec_helper'"],
}

TEST_FILE_PATTERNS = [
    r"test_.*\.py$",
    r".*_test\.py$",
    r".*\.test\.(js|ts|jsx|tsx)$",
    r".*\.spec\.(js|ts|jsx|tsx)$",
    r".*Test\.java$",
    r".*_test\.go$",
    r".*_test\.rb$",
    r".*Tests?\.(cs|java)$",
]

MAX_FILES_SCANNED = 20_000


def test_discovery_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: discover existing tests and coverage for the changes.

    Args:
        state: Current AgentState.

    Returns:
        Partial state with existing_tests, test_coverage_map, uncovered_modules,
        coverage_gaps, test_config and warnings.
    """
    start_time = time.monotonic()
    logger.info("Test discovery agent started", repo_id=state.get("repository_id"))

    warnings: list[str] = []

    try:
        repo_path = resolve_repo_root(state)

        if not repo_path.exists():
            warnings.append(
                "test_discovery_agent: repository checkout not found on disk — "
                "coverage gaps are unknown, not zero"
            )

        test_files = _discover_test_files(repo_path)
        test_config = _detect_test_config(repo_path, test_files)

        builder = DependencyGraphBuilder.from_edge_records(
            records=state.get("dependency_graph_edges", []),
            repo_root=repo_path,
        )
        coverage_map = _map_tests_to_sources(test_files, builder)

        changed_file_paths = [
            f["path"] for f in state.get("changed_files", []) if f.get("status") != "deleted"
        ]
        changed_coverage = {path: coverage_map.get(path, []) for path in changed_file_paths}
        uncovered = sorted(path for path, tests in changed_coverage.items() if not tests)

        if changed_file_paths and not test_files:
            warnings.append(
                "test_discovery_agent: no test files found in the repository — "
                "generated tests will define the project's test conventions"
            )

        duration = time.monotonic() - start_time
        logger.info(
            "Test discovery agent completed",
            test_files=len(test_files),
            total_tests=test_config["total_tests"],
            frameworks=test_config["frameworks_detected"],
            changed_files_with_tests=sum(1 for tests in changed_coverage.values() if tests),
            uncovered_modules=len(uncovered),
            duration_ms=int(duration * 1000),
        )

        return {
            "existing_tests": test_files,
            "uncovered_modules": uncovered,
            "coverage_gaps": uncovered[:20],
            "test_coverage_map": changed_coverage,
            "test_config": test_config,
            "warnings": warnings,
            "completed_agents": ["test_discovery_agent"],
        }

    except Exception as e:
        logger.exception("Test discovery agent failed", error=str(e))
        return {
            "errors": [f"test_discovery_agent: {e}"],
            "existing_tests": [],
            "uncovered_modules": [],
            "coverage_gaps": [],
            "test_coverage_map": {},
            "test_config": {},
        }


def _discover_test_files(repo_path: Path) -> list[TestFile]:
    """Scan a repository for test files."""
    if not repo_path.exists():
        return []

    test_files = []
    compiled_patterns = [re.compile(p) for p in TEST_FILE_PATTERNS]
    scanned = 0

    for file_path in repo_path.rglob("*"):
        if not file_path.is_file():
            continue

        scanned += 1
        if scanned > MAX_FILES_SCANNED:
            logger.warning("Test discovery scan truncated", limit=MAX_FILES_SCANNED)
            break

        # Skip ignored directories (repo-relative, so an ancestor of the
        # checkout itself cannot accidentally exclude everything).
        try:
            relative_parts = file_path.relative_to(repo_path).parts
        except ValueError:
            relative_parts = file_path.parts
        if any(part in settings.ignored_directories for part in relative_parts):
            continue

        relative_path = str(file_path.relative_to(repo_path)).replace("\\", "/")

        # Check if this matches any test file pattern
        if not any(p.search(relative_path) for p in compiled_patterns):
            continue

        try:
            content = file_path.read_text(errors="replace")
        except OSError:
            continue

        framework = _detect_framework(content, relative_path)
        test_names = _extract_test_names(content, framework)
        covers = _infer_covered_modules(relative_path, content)

        test_files.append(
            TestFile(
                path=relative_path,
                framework=framework,
                test_count=len(test_names),
                test_names=test_names[:50],  # Cap at 50
                covers_modules=covers,
            )
        )

    return test_files


def _detect_framework(content: str, file_path: str) -> str:
    """Detect the test framework used in a file."""
    for framework, patterns in FRAMEWORK_PATTERNS.items():
        if any(re.search(p, content) for p in patterns):
            return framework

    # Fallback to extension-based detection
    if file_path.endswith(".py"):
        return "pytest"
    if file_path.endswith((".js", ".ts", ".jsx", ".tsx")):
        return "jest"
    if file_path.endswith(".java"):
        return "junit"
    if file_path.endswith("_test.go"):
        return "go_test"
    return "unknown"


def _extract_test_names(content: str, framework: str) -> list[str]:
    """Extract test function/method names from file content."""
    patterns_by_framework = {
        "pytest": r"def (test_\w+)",
        "jest": r"(?:it|test)\(['\"]([^'\"]+)['\"]",
        "vitest": r"(?:it|test)\(['\"]([^'\"]+)['\"]",
        "junit": r"public void (\w+)\(\)",
        "go_test": r"func (Test\w+)\(",
        "mocha": r"(?:it|describe)\(['\"]([^'\"]+)['\"]",
        "rspec": r"it ['\"]([^'\"]+)['\"]",
    }
    pattern = patterns_by_framework.get(framework)
    if not pattern:
        return []
    return re.findall(pattern, content)


def _infer_covered_modules(test_path: str, content: str) -> list[str]:
    """Infer which source modules a test file covers."""
    covered = []
    # From test file naming convention
    name = Path(test_path).stem
    source_name = name.replace("test_", "").replace("_test", "")
    if source_name:
        covered.append(source_name)
    # From import statements
    imports = re.findall(r"(?:from|import)\s+([\w.]+)", content)
    for imp in imports:
        if not imp.startswith(("test", "pytest", "jest", "unittest", "vitest")):
            covered.append(imp.split(".")[-1])
    return list(set(covered))[:10]


def _map_tests_to_sources(
    test_files: list[TestFile],
    builder: DependencyGraphBuilder,
) -> dict[str, list[str]]:
    """Map each source file to the test files that exercise it.

    Uses three signals, strongest first:
    1. an import edge ``test_file -> source_file`` in the dependency graph;
    2. filename conventions (``test_foo.py`` / ``foo_test.py`` / ``foo.test.ts``);
    3. the module names recorded on the TestFile (``covers_modules``).

    Args:
        test_files: Discovered test files.
        builder: Dependency graph reconstructed from persisted edge records.

    Returns:
        Mapping of source file path -> sorted list of covering test paths.
    """
    mapping: dict[str, set[str]] = {}
    test_paths = {tf["path"] for tf in test_files}

    # Index graph nodes by stem so naming conventions can be resolved.
    nodes_by_stem: dict[str, list[str]] = {}
    for node in builder.graph.nodes():
        if node in test_paths:
            continue
        nodes_by_stem.setdefault(Path(node).stem.lower(), []).append(node)

    for test_file in test_files:
        test_path = test_file["path"]

        # 1. Import edges: test -> source means the source is covered.
        if builder.is_known(test_path):
            for source in builder.graph.successors(test_path):
                if source not in test_paths:
                    mapping.setdefault(source, set()).add(test_path)

        # 2. Naming conventions.
        stem = Path(test_path).stem.lower()
        candidates = {stem}
        for prefix in ("test_", "tests_"):
            if stem.startswith(prefix):
                candidates.add(stem[len(prefix) :])
        if stem.endswith("_test"):
            candidates.add(stem[: -len("_test")])
        if stem.endswith((".test", ".spec")):
            candidates.add(Path(stem).stem)

        for candidate in candidates:
            for node in nodes_by_stem.get(candidate, []):
                mapping.setdefault(node, set()).add(test_path)

        # 3. Declared covered modules.
        for module in test_file["covers_modules"]:
            for node in nodes_by_stem.get(module.lower(), []):
                mapping.setdefault(node, set()).add(test_path)

    return {source: sorted(tests) for source, tests in mapping.items()}


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _detect_test_config(repo_path: Path, test_files: list[TestFile]) -> dict[str, Any]:
    """Detect the project's test framework, command and config files.

    The sandbox and the generator both need this: without the real test command
    the execution agent has to guess, and without fixtures/conftest the model
    invents setup that does not exist.

    Args:
        repo_path: Local repository root.
        test_files: Test files discovered on disk.

    Returns:
        Dictionary with framework, test_command, config_files, has_conftest,
        frameworks_detected, total_test_files and total_tests.
    """
    config: dict[str, Any] = {
        "framework": None,
        "test_command": None,
        "config_files": [],
        "has_conftest": False,
        "has_fixtures": False,
    }

    if not repo_path.exists():
        config["frameworks_detected"] = sorted({tf["framework"] for tf in test_files})
        config["total_test_files"] = len(test_files)
        config["total_tests"] = sum(tf["test_count"] for tf in test_files)
        return config

    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        text = _read_text(pyproject)
        config["config_files"].append("pyproject.toml")
        if "[tool.pytest" in text:
            config["framework"] = "pytest"
            config["test_command"] = "pytest"

    setup_cfg = repo_path / "setup.cfg"
    if setup_cfg.is_file() and "[tool:pytest]" in _read_text(setup_cfg):
        config["config_files"].append("setup.cfg")
        config["framework"] = "pytest"
        config["test_command"] = config["test_command"] or "pytest"

    pytest_ini = repo_path / "pytest.ini"
    if pytest_ini.is_file():
        config["config_files"].append("pytest.ini")
        config["framework"] = "pytest"
        config["test_command"] = config["test_command"] or "pytest"

    package_json = repo_path / "package.json"
    if package_json.is_file():
        config["config_files"].append("package.json")
        raw = _read_text(package_json)
        try:
            package = json.loads(raw)
        except json.JSONDecodeError:
            package = {}
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if isinstance(scripts, dict) and scripts.get("test"):
            # `scripts.test` is already the command (e.g. "jest --ci").
            config["test_command"] = config["test_command"] or scripts["test"]
        dev_deps = {}
        for key in ("devDependencies", "dependencies"):
            if isinstance(package.get(key), dict):
                dev_deps.update(package[key])
        for framework in ("vitest", "jest", "mocha"):
            if framework in dev_deps:
                config["framework"] = config["framework"] or framework
                break

    if (repo_path / "go.mod").is_file():
        config["config_files"].append("go.mod")
        config["framework"] = config["framework"] or "go_test"
        config["test_command"] = config["test_command"] or "go test ./..."

    if (repo_path / "pom.xml").is_file():
        config["config_files"].append("pom.xml")
        config["framework"] = config["framework"] or "junit"
        config["test_command"] = config["test_command"] or "mvn test"

    if (repo_path / "build.gradle").is_file() or (repo_path / "build.gradle.kts").is_file():
        config["config_files"].append("build.gradle")
        config["framework"] = config["framework"] or "junit"
        config["test_command"] = config["test_command"] or "gradle test"

    if (repo_path / ".rspec").is_file():
        config["config_files"].append(".rspec")
        config["framework"] = config["framework"] or "rspec"
        config["test_command"] = config["test_command"] or "bundle exec rspec"

    # conftest.py / fixtures within the first two directory levels (a full
    # rglob would double the cost of the test-file scan on large repos).
    conftest_candidates = [repo_path / "conftest.py"]
    conftest_candidates += list(repo_path.glob("*/conftest.py"))
    conftest_candidates += list(repo_path.glob("*/*/conftest.py"))
    for conftest in conftest_candidates:
        if conftest.is_file():
            config["has_conftest"] = True
            config["config_files"].append(str(conftest.relative_to(repo_path)))
            break
    for directory in ("tests/fixtures", "test/fixtures", "fixtures"):
        if (repo_path / directory).is_dir():
            config["has_fixtures"] = True
            break

    frameworks = sorted({tf["framework"] for tf in test_files})
    config["frameworks_detected"] = frameworks
    if not config["framework"] and frameworks:
        config["framework"] = frameworks[0]
    config["total_test_files"] = len(test_files)
    config["total_tests"] = sum(tf["test_count"] for tf in test_files)
    return config
