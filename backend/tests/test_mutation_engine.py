"""Tests for Adversarial Mutation Engine."""

from pathlib import Path

from app.services.mutation_engine import MutationEngine


def test_generate_mutants_compare_and_binop():
    code = """def calculate_discount(price: float, is_vip: bool) -> float:
    if price > 100:
        discount = price * 0.1
        return discount
    return 0.0
"""
    mutants = MutationEngine.generate_mutants(code, "pricing.py", max_mutants=5)
    assert len(mutants) >= 2
    types = [m.mutation_type for m in mutants]
    assert "operator_swap" in types

    gt_mutant = next(m for m in mutants if ">" in m.original_snippet)
    assert ">=" in gt_mutant.mutated_snippet
    assert "pricing.py" in gt_mutant.file_path


def test_generate_mutants_filtered_by_changed_lines():
    code = """def foo(x):
    if x == 1:
        return 10
    if x > 5:
        return 20
    return 0
"""
    # Only line 4 is changed (`x > 5`)
    mutants = MutationEngine.generate_mutants(code, "foo.py", changed_lines={4}, max_mutants=5)
    assert len(mutants) >= 1
    for m in mutants:
        assert m.line_number == 4
        assert ">" in m.original_snippet


def test_evaluate_mutant_killed_and_survived(tmp_path: Path):
    source_file = tmp_path / "calc.py"
    source_file.write_text(
        """def add(a: int, b: int) -> int:
    return a + b
""",
        encoding="utf-8",
    )

    test_file = tmp_path / "test_calc.py"
    # Strong test: asserts add(2, 3) == 5. If + becomes -, it fails (killed!)
    test_file.write_text(
        """from calc import add

def test_add():
    assert add(2, 3) == 5
""",
        encoding="utf-8",
    )

    mutants = MutationEngine.generate_mutants(source_file.read_text(), "calc.py", max_mutants=2)
    assert len(mutants) >= 1
    add_mutant = next(m for m in mutants if "+" in m.original_snippet)

    result = MutationEngine.run_mutant_in_sandbox(
        repo_root=tmp_path,
        mutant=add_mutant,
        test_command=["python", "-m", "pytest", "-q"],
    )
    assert result["status"] == "killed"
    # Ensure original content is restored
    assert (
        source_file.read_text(encoding="utf-8")
        == """def add(a: int, b: int) -> int:
    return a + b
"""
    )
