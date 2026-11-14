"""TestPilot AI — Adversarial Mutation Engine.

Generates high-leverage AST mutations (operator swaps, boundary shifts, return value
tampers) against PR changes to measure actual test efficacy and assertion depth
(Mutation Score).
"""

from __future__ import annotations

import ast
import copy
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.agents.state import MutantResult
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MutantCandidate:
    """A single synthetic AST mutation."""

    mutant_id: str
    file_path: str  # relative or str path
    line_number: int
    mutation_type: str
    description: str
    original_snippet: str
    mutated_snippet: str
    mutated_source: str


class _SingleNodeReplacer(ast.NodeTransformer):
    """Replaces a single AST node at target (lineno, col_offset)."""

    def __init__(self, target_node: ast.AST, replacement_node: ast.AST) -> None:
        self.target_node = target_node
        self.replacement_node = replacement_node

    def generic_visit(self, node: ast.AST) -> ast.AST:
        if node is self.target_node:
            return self.replacement_node
        return super().generic_visit(node)


class MutationEngine:
    """Discovers and evaluates AST mutations for changed code."""

    @classmethod
    def generate_mutants(
        cls,
        source_code: str,
        file_path: str,
        changed_lines: set[int] | None = None,
        max_mutants: int = 5,
    ) -> list[MutantCandidate]:
        """Scan Python code and generate up to `max_mutants` discrete AST mutations.

        Args:
            source_code: Full content of the source file.
            file_path: Path to the file being mutated.
            changed_lines: If provided, only mutate nodes overlapping these lines.
            max_mutants: Maximum number of mutants to return.
        """
        try:
            tree = ast.parse(source_code)
        except Exception as e:
            logger.warning("Failed to parse AST for mutation", file=file_path, error=str(e))
            return []

        candidates: list[MutantCandidate] = []
        mutant_idx = 1

        for node in ast.walk(tree):
            if mutant_idx > max_mutants:
                break

            lineno = getattr(node, "lineno", None)
            if lineno is None:
                continue

            # Prioritize code modified in the PR
            if changed_lines and lineno not in changed_lines:
                continue

            # --- Mutation Rule 1: Comparison Operator Swaps ---
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                op = node.ops[0]
                new_op = None
                desc = ""
                if isinstance(op, ast.Gt):
                    new_op = ast.GtE()
                    desc = "Boundary shift: swapped `>` to `>=`"
                elif isinstance(op, ast.GtE):
                    new_op = ast.Gt()
                    desc = "Boundary shift: swapped `>=` to `>`"
                elif isinstance(op, ast.Lt):
                    new_op = ast.LtE()
                    desc = "Boundary shift: swapped `<` to `<=`"
                elif isinstance(op, ast.LtE):
                    new_op = ast.Lt()
                    desc = "Boundary shift: swapped `<=` to `<`"
                elif isinstance(op, ast.Eq):
                    new_op = ast.NotEq()
                    desc = "Equality inversion: swapped `==` to `!=`"
                elif isinstance(op, ast.NotEq):
                    new_op = ast.Eq()
                    desc = "Equality inversion: swapped `!=` to `==`"

                if new_op is not None:
                    mutated = cls._apply_node_replacement(
                        tree,
                        node,
                        ast.Compare(left=node.left, ops=[new_op], comparators=node.comparators),
                    )
                    if mutated:
                        candidates.append(
                            MutantCandidate(
                                mutant_id=f"mut_{mutant_idx:03d}",
                                file_path=file_path,
                                line_number=lineno,
                                mutation_type="operator_swap",
                                description=desc,
                                original_snippet=ast.unparse(node),
                                mutated_snippet=ast.unparse(
                                    ast.Compare(
                                        left=node.left,
                                        ops=[new_op],
                                        comparators=node.comparators,
                                    )
                                ),
                                mutated_source=mutated,
                            )
                        )
                        mutant_idx += 1
                        continue

            # --- Mutation Rule 2: Binary Operator Swaps (+ <-> -, * <-> /) ---
            if isinstance(node, ast.BinOp):
                new_op = None
                desc = ""
                if isinstance(node.op, ast.Add):
                    new_op = ast.Sub()
                    desc = "Arithmetic tamper: swapped `+` to `-`"
                elif isinstance(node.op, ast.Sub):
                    new_op = ast.Add()
                    desc = "Arithmetic tamper: swapped `-` to `+`"
                elif isinstance(node.op, ast.Mult):
                    new_op = ast.FloorDiv()
                    desc = "Arithmetic tamper: swapped `*` to `//`"

                if new_op is not None:
                    mutated = cls._apply_node_replacement(
                        tree,
                        node,
                        ast.BinOp(left=node.left, op=new_op, right=node.right),
                    )
                    if mutated:
                        candidates.append(
                            MutantCandidate(
                                mutant_id=f"mut_{mutant_idx:03d}",
                                file_path=file_path,
                                line_number=lineno,
                                mutation_type="boundary_shift",
                                description=desc,
                                original_snippet=ast.unparse(node),
                                mutated_snippet=ast.unparse(
                                    ast.BinOp(left=node.left, op=new_op, right=node.right)
                                ),
                                mutated_source=mutated,
                            )
                        )
                        mutant_idx += 1
                        continue

            # --- Mutation Rule 3: Return Tamper (return val -> return None) ---
            if isinstance(node, ast.Return) and node.value is not None:
                # If it's already None, change to True
                if isinstance(node.value, ast.Constant) and node.value.value is None:
                    replacement = ast.Return(value=ast.Constant(value=True))
                    desc = "Return tamper: swapped `return None` to `return True`"
                else:
                    replacement = ast.Return(value=ast.Constant(value=None))
                    desc = "Return tamper: forced return to `None`"

                mutated = cls._apply_node_replacement(tree, node, replacement)
                if mutated:
                    candidates.append(
                        MutantCandidate(
                            mutant_id=f"mut_{mutant_idx:03d}",
                            file_path=file_path,
                            line_number=lineno,
                            mutation_type="return_tamper",
                            description=desc,
                            original_snippet=ast.unparse(node),
                            mutated_snippet=ast.unparse(replacement),
                            mutated_source=mutated,
                        )
                    )
                    mutant_idx += 1
                    continue

        return candidates

    @classmethod
    def _apply_node_replacement(
        cls,
        original_tree: ast.AST,
        target_node: ast.AST,
        replacement_node: ast.AST,
    ) -> str | None:
        """Deep copy tree, replace target node, and unparse to source."""
        try:
            tree_copy = copy.deepcopy(original_tree)
            # Find the matching node in the copied tree by (lineno, col_offset, node type)
            target_lineno = getattr(target_node, "lineno", None)
            target_col = getattr(target_node, "col_offset", None)
            target_type = type(target_node)

            match = None
            for n in ast.walk(tree_copy):
                if (
                    type(n) is target_type
                    and getattr(n, "lineno", None) == target_lineno
                    and getattr(n, "col_offset", None) == target_col
                ):
                    match = n
                    break

            if match is None:
                return None

            replacer = _SingleNodeReplacer(match, replacement_node)
            mutated_tree = replacer.visit(tree_copy)
            ast.fix_missing_locations(mutated_tree)
            return ast.unparse(mutated_tree)
        except Exception as e:
            logger.debug("Failed to apply node replacement", error=str(e))
            return None

    @classmethod
    def run_mutant_in_sandbox(
        cls,
        repo_root: Path,
        mutant: MutantCandidate,
        test_command: list[str] | None = None,
        timeout: int = 45,
    ) -> MutantResult:
        """Inject mutant into repo, execute tests, record kill/survival, and restore."""
        target_file = repo_root / mutant.file_path
        if not target_file.exists():
            return MutantResult(
                mutant_id=mutant.mutant_id,
                target_file=mutant.file_path,
                line_number=mutant.line_number,
                mutation_type=mutant.mutation_type,
                description=mutant.description,
                original_snippet=mutant.original_snippet,
                mutated_snippet=mutant.mutated_snippet,
                status="error",
                killer_test=None,
            )

        original_content = target_file.read_text(encoding="utf-8")
        cmd = test_command or ["python", "-m", "pytest", "-q", "--tb=line"]

        try:
            # 1. Inject mutation into file
            target_file.write_text(mutant.mutated_source, encoding="utf-8")

            # 2. Run test suite
            res = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            # If tests fail, mutant was KILLED (tests correctly caught the injected bug!)
            if res.returncode != 0:
                # Extract first failing test line if possible
                killer = None
                for line in res.stdout.splitlines() + res.stderr.splitlines():
                    if "FAILED" in line or "Error" in line:
                        killer = line.strip()
                        break
                return MutantResult(
                    mutant_id=mutant.mutant_id,
                    target_file=mutant.file_path,
                    line_number=mutant.line_number,
                    mutation_type=mutant.mutation_type,
                    description=mutant.description,
                    original_snippet=mutant.original_snippet,
                    mutated_snippet=mutant.mutated_snippet,
                    status="killed",
                    killer_test=killer or "Test failed as expected",
                )
            else:
                # Tests passed despite the bug — mutant SURVIVED (weak tests!)
                return MutantResult(
                    mutant_id=mutant.mutant_id,
                    target_file=mutant.file_path,
                    line_number=mutant.line_number,
                    mutation_type=mutant.mutation_type,
                    description=mutant.description,
                    original_snippet=mutant.original_snippet,
                    mutated_snippet=mutant.mutated_snippet,
                    status="survived",
                    killer_test=None,
                )
        except subprocess.TimeoutExpired:
            # Timeout means infinite loop or hanging bug introduced by mutation -> killed!
            return MutantResult(
                mutant_id=mutant.mutant_id,
                target_file=mutant.file_path,
                line_number=mutant.line_number,
                mutation_type=mutant.mutation_type,
                description=mutant.description,
                original_snippet=mutant.original_snippet,
                mutated_snippet=mutant.mutated_snippet,
                status="killed",
                killer_test="Execution timed out on mutant",
            )
        except Exception:
            return MutantResult(
                mutant_id=mutant.mutant_id,
                target_file=mutant.file_path,
                line_number=mutant.line_number,
                mutation_type=mutant.mutation_type,
                description=mutant.description,
                original_snippet=mutant.original_snippet,
                mutated_snippet=mutant.mutated_snippet,
                status="error",
                killer_test=None,
            )
        finally:
            # 3. Always restore original content
            target_file.write_text(original_content, encoding="utf-8")
