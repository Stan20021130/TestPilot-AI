"""Unit tests for the context-collection agents.

Covers the three parallel fan-out branches that feed the Test Generator
(impact, search, test discovery) plus the diff hunk plumbing they depend on.
These are deliberately dependency-free: no Qdrant, no LLM, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agents import impact_agent, search_agent, test_discovery_agent, test_generator_agent
from app.agents.diff_agent import _filter_nodes_by_changed_lines
from app.utils.patch_parser import lines_intersect, parse_added_lines

# ==============================================================================
# Patch / hunk parsing
# ==============================================================================


SAMPLE_PATCH = """@@ -1,4 +1,5 @@
 import os
-old = 1
+new = 2
+another = 3
 keep = 4
@@ -10,2 +11,2 @@
-x
+y
"""


def test_parse_added_lines_tracks_new_file_numbering() -> None:
    assert parse_added_lines(SAMPLE_PATCH) == [2, 3, 11]


def test_parse_added_lines_handles_missing_patch() -> None:
    assert parse_added_lines(None) == []
    assert parse_added_lines("") == []


def test_parse_added_lines_ignores_no_newline_marker() -> None:
    patch = "@@ -1,1 +1,2 @@\n a\n+b\n\\ No newline at end of file\n"
    assert parse_added_lines(patch) == [2]


def test_lines_intersect() -> None:
    assert lines_intersect(10, 20, [12]) is True
    assert lines_intersect(10, 20, [21]) is False
    assert lines_intersect(20, 10, [15]) is True  # reversed range tolerated
    assert lines_intersect(1, 5, []) is False


def test_filter_nodes_prefers_symbol_granularity() -> None:
    nodes = [
        {"name": "kept_elsewhere", "line_start": 1, "line_end": 5},
        {"name": "touched", "line_start": 10, "line_end": 20},
    ]

    kept, used_fallback = _filter_nodes_by_changed_lines(nodes, [12])
    assert [node["name"] for node in kept] == ["touched"]
    assert used_fallback is False


def test_filter_nodes_falls_back_without_line_data() -> None:
    nodes = [
        {"name": "a", "line_start": 1, "line_end": 5},
        {"name": "b", "line_start": 10, "line_end": 20},
    ]

    kept, used_fallback = _filter_nodes_by_changed_lines(nodes, [])
    assert [node["name"] for node in kept] == ["a", "b"]
    assert used_fallback is True


def test_filter_nodes_falls_back_for_module_level_change() -> None:
    nodes = [{"name": "a", "line_start": 1, "line_end": 5}]

    kept, used_fallback = _filter_nodes_by_changed_lines(nodes, [99])
    assert len(kept) == 1
    assert used_fallback is True


# ==============================================================================
# Impact agent
# ==============================================================================


def _impact_state() -> dict:
    return {
        "pr_id": "pr-1",
        "repository_id": "repo-1",
        "changed_files": [
            {
                "path": "app/services/billing.py",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "old_path": None,
                "changed_lines": [4],
            },
            {
                "path": "app/new_module.py",
                "status": "added",
                "additions": 10,
                "deletions": 0,
                "old_path": None,
                "changed_lines": [1],
            },
        ],
        "dependency_graph_edges": [
            {
                "source_node": "app/api/v1/billing.py",
                "target_node": "app/services/billing.py",
                "edge_type": "imports",
            },
            {
                "source_node": "app/api/v1/router.py",
                "target_node": "app/api/v1/billing.py",
                "edge_type": "imports",
            },
            {
                "source_node": "tests/test_billing.py",
                "target_node": "app/services/billing.py",
                "edge_type": "imports",
            },
            {
                "source_node": "app/models/invoice.py",
                "target_node": "app/services/billing.py",
                "edge_type": "imports",
            },
        ],
        "warnings": [],
        "completed_agents": [],
    }


def test_impact_agent_preserves_depth_and_flags_unknown_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(impact_agent, "settings", SimpleNamespace(repo_storage_path=tmp_path))

    result = impact_agent.impact_agent_node(_impact_state())

    assert result["impact_radius"] == 3
    assert result["impact_level"] == "moderate"

    depths = {entry["file"]: entry["depth"] for entry in result["impact_details"]}
    assert depths["app/api/v1/billing.py"] == 1
    assert depths["app/api/v1/router.py"] == 2
    assert depths["app/models/invoice.py"] == 1
    # Test files are verification targets, not blast radius.
    assert "tests/test_billing.py" not in result["affected_modules"]
    assert result["affected_tests"] == ["tests/test_billing.py"]

    # Structural classification, not substring matching.
    assert result["affected_apis"] == ["app/api/v1/billing.py", "app/api/v1/router.py"]
    assert result["affected_services"] == []

    # A changed file absent from the graph is "unknown", never "no impact".
    assert result["impact_unknown_files"] == ["app/new_module.py"]
    assert any("impact unknown" in warning for warning in result["warnings"])


def test_impact_agent_reports_unknown_when_graph_is_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(impact_agent, "settings", SimpleNamespace(repo_storage_path=tmp_path))
    state = _impact_state()
    state["dependency_graph_edges"] = []

    result = impact_agent.impact_agent_node(state)

    assert result["impact_level"] == "unknown"
    assert result["impact_radius"] == 0
    assert any("dependency graph is empty" in warning for warning in result["warnings"])


# ==============================================================================
# Search agent
# ==============================================================================


class _FakeHit:
    def __init__(self, score: float, content: str, file_path: str) -> None:
        self.score = score
        self.payload = {
            "content": content,
            "file_path": file_path,
            "language": "python",
            "type": "function",
            "function_name": "charge",
        }


class _FakeQdrant:
    def __init__(self, batches: list[list[_FakeHit]]) -> None:
        self.batches = batches
        self.calls: list[str] = []
        self.filters: list[object] = []

    def query_points(self, collection_name, query, query_filter, limit, with_payload=True):
        self.calls.append(query)
        self.filters.append(query_filter)
        index = min(len(self.calls) - 1, len(self.batches) - 1)
        return SimpleNamespace(points=self.batches[index])


def _search_state() -> dict:
    return {
        "pr_id": "pr-1",
        "repository_id": "repo-1",
        "changed_nodes": [
            {"name": "charge", "type": "function", "file_path": "app/services/billing.py"},
            {"name": "refund", "type": "function", "file_path": "app/services/billing.py"},
        ],
        "warnings": [],
        "completed_agents": [],
    }


def test_search_agent_ranks_and_dedupes(monkeypatch) -> None:
    first = "def charge(amount):\n    return amount * 1.2\n" * 2
    second = "def refund(amount):\n    return amount\n" * 2
    third = "class Invoice:\n    total: int = 0\n" * 2

    qdrant = _FakeQdrant(
        [
            [
                _FakeHit(0.9, first, "app/services/billing.py"),
                _FakeHit(0.5, second, "app/services/billing.py"),
            ],
            [
                _FakeHit(0.95, first, "app/services/billing.py"),
                _FakeHit(0.7, third, "app/models/invoice.py"),
            ],
        ]
    )

    monkeypatch.setattr(search_agent, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(
        search_agent,
        "get_embedding_service",
        lambda: SimpleNamespace(generate_embedding=lambda _text: [0.1, 0.2, 0.3]),
    )
    monkeypatch.setattr(
        search_agent,
        "get_settings",
        lambda: SimpleNamespace(qdrant_collection_repository_chunks="repo_chunks"),
    )

    result = search_agent.search_agent_node(_search_state())

    scores = [item["score"] for item in result["retrieved_context"]]
    assert scores == [0.95, 0.7, 0.5]
    assert len(qdrant.calls) == 2
    assert result["search_metrics"]["raw_hits"] == 4
    assert result["search_metrics"]["unique_hits"] == 3
    assert result["warnings"] == []

    # A plain dict is rejected by qdrant-client; the agent must send a Filter.
    from qdrant_client import models

    assert all(isinstance(item, models.Filter) for item in qdrant.filters)


def test_search_agent_surfaces_failures_instead_of_silent_empty(monkeypatch) -> None:
    def _boom(text: str):
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(search_agent, "get_qdrant_client", lambda: _FakeQdrant([[]]))
    monkeypatch.setattr(
        search_agent,
        "get_embedding_service",
        lambda: SimpleNamespace(generate_embedding=_boom),
    )
    monkeypatch.setattr(
        search_agent,
        "get_settings",
        lambda: SimpleNamespace(qdrant_collection_repository_chunks="repo_chunks"),
    )

    result = search_agent.search_agent_node(_search_state())

    assert result["retrieved_context"] == []
    assert result["search_metrics"]["failed_queries"] == 2
    assert any("every vector search failed" in warning for warning in result["warnings"])


# ==============================================================================
# Test discovery agent
# ==============================================================================


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _discovery_state() -> dict:
    return {
        "pr_id": "pr-1",
        "repository_id": "repo-1",
        "changed_files": [
            {
                "path": "app/services/billing.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "old_path": None,
                "changed_lines": [1],
            },
            {
                "path": "app/api/v1/unknown.py",
                "status": "added",
                "additions": 5,
                "deletions": 0,
                "old_path": None,
                "changed_lines": [1],
            },
        ],
        "dependency_graph_edges": [
            {
                "source_node": "tests/test_billing.py",
                "target_node": "app/services/billing.py",
                "edge_type": "imports",
            }
        ],
        "warnings": [],
        "completed_agents": [],
    }


def test_test_discovery_maps_tests_to_sources(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo-1"
    _write(repo / "pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-q'\n")
    _write(repo / "conftest.py", "import pytest\n")
    _write(repo / "app/services/billing.py", "def charge():\n    return 1\n")
    _write(repo / "app/api/v1/unknown.py", "def handler():\n    return 2\n")
    _write(
        repo / "tests/test_billing.py",
        "from app.services.billing import charge\n\n\ndef test_charge():\n    assert charge() == 1\n",
    )

    monkeypatch.setattr(
        test_discovery_agent,
        "settings",
        SimpleNamespace(
            repo_storage_path=tmp_path,
            ignored_directories={".git", "node_modules", "__pycache__", "venv"},
        ),
    )

    state = _discovery_state()
    state["workspace_path"] = str(repo)
    result = test_discovery_agent.test_discovery_agent_node(state)

    assert [test["path"] for test in result["existing_tests"]] == ["tests/test_billing.py"]
    assert result["test_coverage_map"]["app/services/billing.py"] == ["tests/test_billing.py"]
    assert result["test_coverage_map"]["app/api/v1/unknown.py"] == []
    assert result["uncovered_modules"] == ["app/api/v1/unknown.py"]
    assert result["test_config"]["framework"] == "pytest"
    assert result["test_config"]["test_command"] == "pytest"
    assert result["test_config"]["has_conftest"] is True


def test_test_discovery_warns_when_checkout_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        test_discovery_agent,
        "settings",
        SimpleNamespace(
            repo_storage_path=tmp_path / "missing",
            ignored_directories={".git"},
        ),
    )

    state = _discovery_state()
    state["workspace_path"] = str(tmp_path / "missing")
    result = test_discovery_agent.test_discovery_agent_node(state)

    assert result["existing_tests"] == []
    assert any("checkout not found" in warning for warning in result["warnings"])


# ==============================================================================
# Test generator context assembly
# ==============================================================================


def test_build_prompt_includes_real_code_contracts_and_targets() -> None:
    prompt = test_generator_agent._build_prompt(
        changed_summary="- FUNCTION: charge in app/services/billing.py",
        changed_code="### FILE: app/services/billing.py\ndef charge():\n    return 1",
        contracts_block="- def charge(amount: float) -> Decimal [app/services/billing.py]",
        style_block="### tests/test_billing.py (pytest)\ndef test_charge():\n    ...",
        context_block="### app/models/invoice.py\nclass Invoice:\n    total: int = 0",
        targets_block="- [CRITICAL] app/api/v1/unknown.py (score 84) — no existing test covers this file",
        test_config={
            "framework": "pytest",
            "test_command": "pytest",
            "config_files": ["pyproject.toml"],
            "has_conftest": True,
            "has_fixtures": False,
        },
    )

    assert "def charge()" in prompt
    assert "def charge(amount: float) -> Decimal" in prompt
    assert "app/api/v1/unknown.py" in prompt
    assert "[CRITICAL]" in prompt
    assert "Test command: pytest" in prompt
    assert "conftest.py present: yes" in prompt


def test_render_context_ranks_and_dedupes() -> None:
    filler = "x = 1\n" * 20
    items = [
        {"score": 0.2, "content": filler, "file_path": "b.py"},
        {"score": 0.9, "content": filler, "file_path": "a.py"},
        {"score": 0.5, "content": filler, "file_path": "a.py"},
    ]

    block, _stats = test_generator_agent._render_context(items)

    assert block.index("a.py") < block.index("b.py")
    assert block.count("###") == 2


def test_resolve_source_file_attributes_tests_to_the_right_file() -> None:
    nodes = [
        {"file_path": "app/services/billing.py", "name": "charge"},
        {"file_path": "app/api/v1/auth.py", "name": "login"},
    ]

    assert (
        test_generator_agent._resolve_source_file(
            "tests/test_billing.py", "def test_charge(): ...", nodes
        )
        == "app/services/billing.py"
    )
    assert (
        test_generator_agent._resolve_source_file(
            "tests/test_auth_flow.py", "def test_login(): login()", nodes
        )
        == "app/api/v1/auth.py"
    )


def test_load_changed_code_slices_symbol_body(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo-1"
    _write(repo / "app/services/billing.py", "line1\nline2\ndef charge():\n    return 42\n")

    block, warnings = test_generator_agent._load_changed_code(
        {"repository_id": "repo-1", "workspace_path": str(repo)},
        [
            {
                "file_path": "app/services/billing.py",
                "type": "function",
                "name": "charge",
                "line_start": 3,
                "line_end": 4,
            }
        ],
    )

    assert "return 42" in block
    assert "line1" not in block
    assert warnings == []


def test_path_classifier_uses_structure_not_substrings() -> None:
    from app.utils.path_classifier import classify_path

    assert classify_path("app/api/v1/auth.py") == "api"
    assert classify_path("app/services/billing.py") == "service"
    assert classify_path("app/models/invoice.py") == "model"
    assert classify_path("tests/test_billing.py") == "test"
    # Substring matching would misread these as api/service/model.
    assert classify_path("utils/rapid.py") == "module"
    assert classify_path("app/servers/manager.py") == "module"
    assert classify_path("app/db_backup.py") == "module"


# ==============================================================================
# Test discovery service (API endpoint backing)
# ==============================================================================


def test_discovery_service_reports_real_coverage_gaps(tmp_path, monkeypatch) -> None:
    from app.services import test_discovery as service_module

    repo = tmp_path / "repo-1"
    _write(repo / "pyproject.toml", "[tool.pytest.ini_options]\n")
    _write(repo / "app/services/billing.py", "def charge():\n    return 1\n")
    _write(repo / "app/api/v1/unknown.py", "def handler():\n    return 2\n")
    _write(
        repo / "tests/test_billing.py",
        "from app.services.billing import charge\n\n\ndef test_charge():\n    assert charge() == 1\n",
    )

    monkeypatch.setattr(
        service_module,
        "get_settings",
        lambda: SimpleNamespace(ignored_directories={".git", "node_modules", "__pycache__"}),
    )

    report = service_module.TestDiscovery(repo).discover_with_coverage(
        edge_records=[
            {
                "source_node": "tests/test_billing.py",
                "target_node": "app/services/billing.py",
                "edge_type": "imports",
            }
        ]
    )

    assert report["uncovered_modules"] == ["app/api/v1/unknown.py"]
    assert report["coverage_gaps"] == ["app/api/v1/unknown.py"]
    assert report["total_test_files"] == 1
    assert report["test_files"][0]["file_path"] == "tests/test_billing.py"
    assert report["coverage_map"]["app/services/billing.py"] == ["tests/test_billing.py"]
