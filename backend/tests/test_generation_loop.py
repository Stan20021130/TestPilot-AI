"""Tests for the bounded generation loop: triage, feedback, coverage top-up."""

from __future__ import annotations

from app.agents import test_generator_agent


def _analysis(origin: str, name: str = "test_x") -> dict:
    return {
        "test_name": name,
        "failure_message": "boom",
        "root_cause": "wrong assumption",
        "affected_code_path": "tests/test_x.py",
        "suggested_fix": "use charge()",
        "failure_origin": origin,
        "confidence": 0.8,
    }


# ==============================================================================
# Feedback + coverage top-up
# ==============================================================================


def test_feedback_only_lists_test_defects() -> None:
    block = test_generator_agent._build_feedback_block(
        {
            "failure_analyses": [
                _analysis("test_defect", "test_charge"),
                _analysis("product_defect", "test_refund"),
            ]
        }
    )

    assert "test_charge" in block
    assert "test_refund" not in block


def test_feedback_empty_when_no_test_defects() -> None:
    assert (
        test_generator_agent._build_feedback_block(
            {"failure_analyses": [_analysis("product_defect")]}
        )
        == ""
    )


def test_missing_critical_targets_ignores_covered_and_low_priority() -> None:
    targets = [
        {"file": "app/api/billing.py", "priority": "critical"},
        {"file": "app/services/refund.py", "priority": "high"},
        {"file": "app/utils/text.py", "priority": "low"},
    ]
    generated = [{"file_path": "app/api/billing.py", "test_file_path": "tests/test_billing.py"}]

    missing = test_generator_agent._missing_critical_targets(generated, targets)

    assert [target["file"] for target in missing] == ["app/services/refund.py"]


def test_merge_generated_deduplicates_by_file_and_function() -> None:
    primary = [{"test_file_path": "tests/test_a.py", "function_name": "test_one"}]
    extra = [
        {"test_file_path": "tests/test_a.py", "function_name": "test_one"},
        {"test_file_path": "tests/test_a.py", "function_name": "test_two"},
    ]

    merged = test_generator_agent._merge_generated(primary, extra)

    assert len(merged) == 2
    assert {test["function_name"] for test in merged} == {"test_one", "test_two"}


def test_prompt_contains_feedback_section_when_present() -> None:
    prompt = test_generator_agent._build_prompt(
        changed_summary="- FUNCTION: charge",
        changed_code="def charge(): ...",
        contracts_block="- def charge()",
        style_block="",
        context_block="",
        targets_block="- [CRITICAL] app/api/billing.py",
        test_config={},
        feedback_block="- `test_charge`: unknown API → use charge()",
    )

    assert "Feedback From The Previous Attempt" in prompt
    assert "unknown API" in prompt


def test_regeneration_merges_instead_of_replacing(monkeypatch) -> None:
    """A second pass must not drop tests that already passed."""
    existing = {
        "function_name": "test_keep",
        "class_name": None,
        "file_path": "app/services/billing.py",
        "test_file_path": "tests/test_billing.py",
        "content": "def test_keep():\n    assert True\n",
        "test_type": "unit",
        "test_framework": "pytest",
        "language": "python",
    }
    fresh = {
        **existing,
        "function_name": "test_new",
        "content": "def test_new():\n    assert True\n",
    }
    monkeypatch.setattr(
        test_generator_agent,
        "_generate_tests",
        lambda **_: ([fresh], [], {"coverage_attempts": 0}),
    )

    result = test_generator_agent.test_generator_agent_node(
        {
            "changed_nodes": [
                {"name": "charge", "type": "function", "file_path": "app/services/billing.py"}
            ],
            "context_health": {"mode": "full"},
            "generated_tests": [existing],
            "generation_attempts": 1,
            "warnings": [],
        }
    )

    names = {test["function_name"] for test in result["generated_tests"]}
    assert names == {"test_keep", "test_new"}


def test_regeneration_replaces_failed_tests_and_keeps_passing_ones(monkeypatch) -> None:
    def _test(name: str, content: str) -> dict:
        return {
            "function_name": name,
            "class_name": None,
            "file_path": "app/services/billing.py",
            "test_file_path": "tests/test_billing.py",
            "content": content,
            "test_type": "unit",
            "test_framework": "pytest",
            "language": "python",
        }

    old_keep = _test("test_keep", "old keep")
    old_fail = _test("test_fail", "old failing")
    new_fail = _test("test_fail", "new failing")
    monkeypatch.setattr(
        test_generator_agent,
        "_generate_tests",
        lambda **_: ([new_fail], [], {"coverage_attempts": 0}),
    )

    result = test_generator_agent.test_generator_agent_node(
        {
            "changed_nodes": [
                {"name": "charge", "type": "function", "file_path": "app/services/billing.py"}
            ],
            "context_health": {"mode": "full"},
            "generated_tests": [old_keep, old_fail],
            "generation_attempts": 1,
            "execution_results": {"failed_tests": [{"name": "tests/test_billing.py::test_fail"}]},
            "warnings": [],
        }
    )

    by_name = {test["function_name"]: test for test in result["generated_tests"]}
    assert set(by_name) == {"test_keep", "test_fail"}
    assert by_name["test_keep"]["content"] == "old keep"
    assert by_name["test_fail"]["content"] == "new failing"


def test_prompt_omits_feedback_section_by_default() -> None:
    prompt = test_generator_agent._build_prompt(
        changed_summary="- FUNCTION: charge",
        changed_code="def charge(): ...",
        contracts_block="- def charge()",
        style_block="",
        context_block="",
        targets_block="",
        test_config={},
    )

    assert "Feedback From The Previous Attempt" not in prompt
