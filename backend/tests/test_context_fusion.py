"""Tests for the fusion layer: contracts, risk ranking, history and controller."""

from __future__ import annotations

from types import SimpleNamespace

from app.agents import context_controller_agent, risk_ranker_agent, test_generator_agent
from app.services.contract_extractor import (
    build_class_contract,
    build_function_contract,
    contracts_from_parse_result,
    render_contracts,
)
from app.services.risk_ranker import rank_test_targets
from app.services.test_history import aggregate_history, normalize_path

# ==============================================================================
# Contracts
# ==============================================================================


def _function_info(**overrides):
    base = {
        "name": "charge",
        "parameters": ["amount: float", "currency: str = 'USD'"],
        "return_type": "Decimal",
        "decorators": ["@app.post('/charge')"],
        "line_start": 10,
        "line_end": 20,
        "is_async": False,
        "is_method": False,
        "class_name": None,
        "docstring": "Charge the customer.\n\n    Raises ValueError on bad input.",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_function_contract_captures_signature_and_intent() -> None:
    contract = build_function_contract(_function_info(), "app/services/billing.py")

    assert contract["signature"] == "def charge(amount: float, currency: str = 'USD') -> Decimal"
    assert contract["return_type"] == "Decimal"
    assert contract["public"] is True
    assert contract["docstring"] == "Charge the customer. Raises ValueError on bad input."
    assert contract["decorators"] == ["@app.post('/charge')"]


def test_private_symbols_are_marked_non_public() -> None:
    contract = build_function_contract(_function_info(name="_charge"), "app/services/billing.py")
    assert contract["public"] is False


def test_async_and_class_contracts() -> None:
    async_contract = build_function_contract(
        _function_info(name="fetch", is_async=True, return_type=None), "app/api/x.py"
    )
    assert async_contract["signature"].startswith("async def fetch(")

    class_contract = build_class_contract(
        SimpleNamespace(
            name="Invoice",
            base_classes=["BaseModel"],
            methods=["total", "apply"],
            decorators=[],
            line_start=1,
            line_end=30,
            docstring="An invoice.",
        ),
        "app/models/invoice.py",
    )
    assert class_contract["signature"] == "class Invoice(BaseModel)"
    assert class_contract["methods"] == ["total", "apply"]


def test_contracts_from_parse_result_filters_to_changed_symbols() -> None:
    result = SimpleNamespace(
        functions=[_function_info(name="charge"), _function_info(name="refund")],
        classes=[],
    )

    contracts = contracts_from_parse_result(
        result, "app/services/billing.py", keep={("function", "refund")}
    )

    assert [contract["symbol"] for contract in contracts] == ["refund"]


def test_render_contracts_is_prompt_ready() -> None:
    block = render_contracts([build_function_contract(_function_info(), "app/services/billing.py")])

    assert "def charge(" in block
    assert "intent: Charge the customer." in block
    assert "returns: Decimal" in block


# ==============================================================================
# Test history aggregation
# ==============================================================================


def test_aggregate_history_detects_flaky_and_slow_tests() -> None:
    rows = [
        ("tests/test_a.py", "test_one", "passed", 0.5),
        ("tests/test_a.py", "test_one", "failed", 0.6),
        ("tests\\test_a.py", "test_two", "passed", 25.0),
    ]

    history = aggregate_history(rows)

    stats = history["tests/test_a.py"]
    assert stats["runs"] == 3
    assert stats["failures"] == 1
    assert stats["flaky"] == 1
    assert stats["slow_tests"] == 1
    assert stats["avg_duration"] > 8


def test_normalize_path_strips_dot_prefixes() -> None:
    assert normalize_path("./tests/test_a.py") == "tests/test_a.py"
    assert normalize_path("tests\\test_a.py") == "tests/test_a.py"
    assert normalize_path(None) == ""


# ==============================================================================
# Risk ranking
# ==============================================================================


def _rank(**overrides):
    kwargs = {
        "changed_nodes": [
            {"name": "charge", "type": "function", "file_path": "app/api/v1/billing.py"}
        ],
        "impact_details": [
            {"file": "app/api/v1/router.py", "depth": 1, "kind": "api"},
            {"file": "app/main.py", "depth": 2, "kind": "module"},
        ],
        "coverage_map": {},
        "uncovered": ["app/api/v1/billing.py"],
        "history": {},
        "contracts": [{"symbol": "charge", "file_path": "app/api/v1/billing.py", "public": True}],
    }
    kwargs.update(overrides)
    return rank_test_targets(**kwargs)


def test_uncovered_api_file_outranks_covered_module() -> None:
    targets = _rank(
        changed_nodes=[
            {"name": "charge", "type": "function", "file_path": "app/api/v1/billing.py"},
            {"name": "helper", "type": "function", "file_path": "app/utils/text.py"},
        ],
        coverage_map={"app/utils/text.py": ["tests/test_text.py"]},
    )

    assert [target["file"] for target in targets][0] == "app/api/v1/billing.py"
    top = targets[0]
    assert top["priority"] == "critical"
    assert any("no existing test covers" in reason for reason in top["reasons"])
    assert any("direct dependent" in reason for reason in top["reasons"])
    assert any("public symbol" in reason for reason in top["reasons"])


def test_history_failures_raise_priority() -> None:
    targets = _rank(
        coverage_map={"app/api/v1/billing.py": ["tests/test_billing.py"]},
        uncovered=[],
        history={"tests/test_billing.py": {"runs": 4, "failures": 2, "flaky": 1}},
    )

    top = targets[0]
    assert any("covering test failed" in reason for reason in top["reasons"])
    assert any("flaky" in reason for reason in top["reasons"])
    assert top["history"]["tests/test_billing.py"]["failures"] == 2


def test_test_files_are_not_targets() -> None:
    targets = _rank(
        changed_nodes=[{"name": "t", "type": "function", "file_path": "tests/test_a.py"}],
        coverage_map={},
        uncovered=[],
        contracts=[],
    )
    assert targets == []


# ==============================================================================
# Context controller
# ==============================================================================


def _long_snippet(name: str = "a.py", size: int = 400) -> dict:
    return {"file_path": name, "content": "x = 1\n" * size, "score": 0.9}


def test_assess_context_flags_missing_evidence() -> None:
    checks = context_controller_agent.assess_context({}, [], [])

    assert checks["contracts"] is False
    assert checks["code_context"] is False
    assert context_controller_agent.context_mode(checks) == "insufficient"
    assert context_controller_agent.context_score(checks) < 50


def test_controller_upgrades_mode_with_impact_aware_retry(monkeypatch) -> None:
    extra = [_long_snippet("app/services/billing.py"), _long_snippet("app/models/invoice.py")]
    monkeypatch.setattr(
        context_controller_agent, "_supplementary_retrieval", lambda _state: (extra, None)
    )

    result = context_controller_agent.context_controller_agent_node(
        {
            "repository_id": "repo-1",
            "changed_nodes": [{"name": "charge", "file_path": "app/services/billing.py"}],
            "affected_modules": ["app/api/v1/router.py"],
            "retrieved_context": [],
            "change_contracts": [],
            "impact_level": "moderate",
            "test_config": {"total_test_files": 3},
            "test_coverage_map": {"app/services/billing.py": ["tests/test_billing.py"]},
            "warnings": [],
        }
    )

    health = result["context_health"]
    assert health["retrieval_attempts"] == 1
    assert health["mode"] != "insufficient"
    assert health["checks"]["code_context"] is True
    assert any("second retrieval pass" in warning for warning in result["warnings"])
    assert result["decisions"][0]["choice"] == "degraded"
    assert result["decisions"][0]["inputs"]["score"] == health["score"]


def test_controller_reports_full_health_when_everything_present(monkeypatch) -> None:
    monkeypatch.setattr(
        context_controller_agent, "_supplementary_retrieval", lambda _state: ([], None)
    )

    result = context_controller_agent.context_controller_agent_node(
        {
            "changed_nodes": [{"name": "charge", "file_path": "app/services/billing.py"}],
            "retrieved_context": [_long_snippet()],
            "change_contracts": [{"symbol": "charge", "file_path": "app/services/billing.py"}],
            "impact_level": "moderate",
            "test_config": {"total_test_files": 1},
            "test_coverage_map": {"app/services/billing.py": ["tests/test_billing.py"]},
            "warnings": [],
        }
    )

    assert result["context_health"]["mode"] == "full"
    assert result["context_health"]["score"] == 100


# ==============================================================================
# Risk ranker node + generation gate
# ==============================================================================


def test_risk_ranker_node_uses_injected_history() -> None:
    result = risk_ranker_agent.risk_ranker_agent_node(
        {
            "repository_id": "repo-1",
            "changed_nodes": [
                {"name": "charge", "type": "function", "file_path": "app/services/billing.py"}
            ],
            "changed_files": [{"path": "app/services/billing.py"}],
            "impact_details": [{"file": "app/api/router.py", "depth": 1, "kind": "api"}],
            "test_coverage_map": {"app/services/billing.py": ["tests/test_billing.py"]},
            "uncovered_modules": [],
            "change_contracts": [],
            "test_history": {"tests/test_billing.py": {"runs": 2, "failures": 1}},
            "warnings": [],
        }
    )

    assert result["test_targets"][0]["file"] == "app/services/billing.py"
    assert result["test_history"]["tests/test_billing.py"]["failures"] == 1
    assert not any("no test history" in warning for warning in result["warnings"])


def test_generator_skips_when_context_is_insufficient() -> None:
    result = test_generator_agent.test_generator_agent_node(
        {
            "changed_nodes": [
                {"name": "charge", "type": "function", "file_path": "app/services/billing.py"}
            ],
            "context_health": {"mode": "insufficient", "checks": {}, "score": 20},
            "warnings": [],
        }
    )

    assert result["generated_tests"] == []
    assert any("skipped generation" in warning for warning in result["warnings"])


def test_graph_contains_fusion_nodes() -> None:
    from app.agents.context_graph import build_context_graph

    nodes = set(build_context_graph().compile().get_graph().nodes)
    assert {"risk_ranker_agent", "context_controller_agent"} <= nodes
