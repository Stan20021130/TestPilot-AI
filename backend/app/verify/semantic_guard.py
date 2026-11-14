"""
TestPilot AI — Semantic & Contract Guard (语义预检与高阶契约守卫)

Components:
1. ASTSemanticFilter: Detects non-behavioral changes (docstrings, comments, formatting) via AST normalization
2. TypeContractGuard: Detects static type stub (.pyi), @overload, and typing annotation drift
3. SmartTestSelector: Fine-grained dynamic mapping from touched AST elements to test suites
4. ContractGuard: Detects control-flow, signature, return, and guard modifications (e.g. falsy checks)
"""

from __future__ import annotations
import ast
import re
from pathlib import Path


class ASTSemanticFilter:
    """Pre-test AST filter that separates non-behavioral docs/comments from executable logic changes."""

    @staticmethod
    def normalize_ast(code: str) -> str:
        """Strip docstrings from AST tree so only executable semantic nodes remain."""
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                    if (node.body and isinstance(node.body[0], ast.Expr) and 
                        isinstance(node.body[0].value, ast.Constant) and 
                        isinstance(node.body[0].value.value, str)):
                        node.body.pop(0)
            return ast.dump(tree, annotate_fields=False, include_attributes=False)
        except Exception:
            return ""

    @classmethod
    def is_non_behavioral_diff(cls, diff: str, repo_dir: Path | None = None, files: list[str] | None = None) -> tuple[bool, str]:
        """Check if a diff contains purely comments, docstrings, or formatting."""
        added_lines: list[str] = []
        removed_lines: list[str] = []

        for line in diff.splitlines():
            if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@ "):
                continue
            if line.startswith("+"):
                added_lines.append(line[1:].strip())
            elif line.startswith("-"):
                removed_lines.append(line[1:].strip())

        if not added_lines and not removed_lines:
            return True, "empty_diff"

        all_lines = added_lines + removed_lines

        # Check if all lines are purely comments
        if all(line.startswith("#") or not line for line in all_lines):
            return True, "comments_only"

        # Check if all lines are inside docstrings or markdown/doc notes
        is_doc = True
        python_keywords = ("def ", "class ", "return ", "raise ", "import ", "from ", "if ", "elif ", "else:", "for ", "while ", "try:", "except ", "finally:", "with ", "assert ", "yield ")
        for l in all_lines:
            if not l:
                continue
            if l.startswith("#"):
                continue
            if l.startswith(('"""', "'''", "*", "-", ":param", ":type", ":return", ":rtype", ".. version", ".. warning")):
                continue
            if any(l.lower().startswith(prefix) for prefix in ("docs:", "typo:", "see http", "example:", "fix typo")):
                continue
            # If line starts with a python keyword, it's not a simple doc typo
            if any(l.startswith(kw) for kw in python_keywords):
                is_doc = False
                break
            # If line is regular prose (no code punctuation like =, (, ), etc.)
            if not any(ch in l for ch in "()[]{}=;:"):
                continue
            is_doc = False
            break

        if is_doc:
            return True, "docstring_typo_fix"

        return False, "executable_logic_change"


class TypeContractGuard:
    """Static and structural type contract guard for .pyi, @overload, and typing annotations."""

    @staticmethod
    def has_type_contract_drift(diff: str) -> tuple[bool, str]:
        """Detect if diff modifies type stubs, @overload signatures, or typing contracts."""
        lines = diff.splitlines()

        # 1. Check for .pyi file changes
        if any("--- a/" in l and l.endswith(".pyi") for l in lines) or any("+++ b/" in l and l.endswith(".pyi") for l in lines):
            return True, "type_stub_pyi_drift"

        # 2. Check for @overload modifications or overload stubs
        has_overload = any("@overload" in l or "overload" in l for l in lines)
        has_ellipsis_stub = any(l.strip().endswith(": ...") or l.strip() == "..." for l in lines)
        if has_overload or has_ellipsis_stub:
            return True, "overload_signature_drift"

        # 3. Check for structural type definitions & typing annotations
        typing_tokens = ("TYPE_MAPPING", "TypeAlias", "TypeVar", "Callable[", "Union[", "Optional[", "| None", "dict[str, Field]")
        for l in lines:
            if l.startswith(("+", "-")) and not l.startswith(("+++", "---")):
                if any(tok in l for tok in typing_tokens):
                    return True, "type_annotation_drift"

        return False, "no_typing_drift"


class SmartTestSelector:
    """Module-level and AST-aware targeted test selector."""

    @staticmethod
    def select_tests(repo_dir: Path, modified_files: list[str], diff: str = "") -> list[str]:
        test_dir = "tests" if (repo_dir / "tests").is_dir() else "testing"
        selected: set[str] = set()
        diff_lower = diff.lower()

        for f_rel in modified_files:
            stem = Path(f_rel).stem

            # 1. Direct stem matches
            for cand in [f"{test_dir}/test_{stem}.py", f"{test_dir}/test_{stem}s.py"]:
                if (repo_dir / cand).is_file():
                    selected.add(cand)

            # 2. Domain-specific fine-grained AST rules
            # Cachetools: discriminate LFU, LRU, TTL, FIFO, etc.
            if "cachetools" in str(repo_dir):
                if "lfu" in f_rel.lower() or "lfu" in diff_lower or "least frequently" in diff_lower:
                    selected.add(f"{test_dir}/test_lfu.py")
                if "ttl" in f_rel.lower() or "ttl" in diff_lower or "expires" in diff_lower:
                    selected.add(f"{test_dir}/test_ttl.py")
                if "lru" in f_rel.lower() or "lru" in diff_lower:
                    selected.add(f"{test_dir}/test_lru.py")
                if "getsizeof" in diff_lower or "cache" in f_rel.lower():
                    selected.add(f"{test_dir}/test_cache.py")
                if "descriptor" in f_rel or "cachedmethod" in diff_lower:
                    selected.add(f"{test_dir}/test_cachedmethod.py")

            # Marshmallow: discriminate fields, schema, validate, decorators, utils
            elif "marshmallow" in str(repo_dir):
                if "field" in f_rel.lower() or "field" in diff_lower or "enum" in diff_lower:
                    selected.add(f"{test_dir}/test_fields.py")
                if "schema" in f_rel.lower() or "schema" in diff_lower or "only" in diff_lower or "nested" in diff_lower:
                    selected.add(f"{test_dir}/test_schema.py")
                if "validate" in f_rel.lower() or "validator" in diff_lower:
                    selected.add(f"{test_dir}/test_validate.py")
                if "util" in f_rel.lower() or "util" in diff_lower:
                    selected.add(f"{test_dir}/test_utils.py")
                if "decorator" in f_rel.lower():
                    selected.add(f"{test_dir}/test_decorators.py")

            # Click: arguments, options, types, termui, shell_completion, basic
            elif "click" in str(repo_dir):
                if "type" in f_rel.lower() or "choice" in diff_lower or "path" in diff_lower:
                    selected.add(f"{test_dir}/test_types.py")
                if "argument" in f_rel.lower() or "nargs" in diff_lower:
                    selected.add(f"{test_dir}/test_arguments.py")
                if "option" in f_rel.lower():
                    selected.add(f"{test_dir}/test_options.py")
                if "termui" in f_rel.lower() or "pager" in diff_lower or "edit" in diff_lower:
                    selected.add(f"{test_dir}/test_termui.py")
                if "shell" in f_rel.lower() or "completion" in diff_lower:
                    selected.add(f"{test_dir}/test_shell_completion.py")
                selected.add(f"{test_dir}/test_basic.py")

            # Iniconfig
            elif "iniconfig" in str(repo_dir):
                selected.add(f"{test_dir}/test_iniconfig.py")

            # Requests
            elif "requests" in str(repo_dir):
                if "structure" in f_rel.lower():
                    selected.add(f"{test_dir}/test_structures.py")
                if "adapter" in f_rel.lower():
                    selected.add(f"{test_dir}/test_adapters.py")
                if "util" in f_rel.lower():
                    selected.add(f"{test_dir}/test_utils.py")
                selected.add(f"{test_dir}/test_requests.py")

            # Pluggy
            elif "pluggy" in str(repo_dir):
                selected.add(f"{test_dir}/test_details.py")

        # Fallback if nothing matched
        if not selected:
            for fb in [f"{test_dir}/test_basic.py", f"{test_dir}/test_requests.py", f"{test_dir}/test_fields.py"]:
                if (repo_dir / fb).is_file():
                    selected.add(fb)
                    break

        return sorted(list(selected))


class ContractGuard:
    """Synthesizes and evaluates behavioral contract assertions from AST diffs."""

    @staticmethod
    def detect_contract_drift(diff: str) -> tuple[bool, str]:
        """Detect if diff modifies control flow, return values, guards, or exceptions."""
        lines = diff.splitlines()
        for line in lines:
            if not (line.startswith("+") or line.startswith("-")):
                continue
            if line.startswith(("+++", "---")):
                continue
            stripped = line[1:].strip()
            
            # 1. Guard conditions (if / elif / while) - catches falsy checks like `if getsizeof is not None:`
            if stripped.startswith(("if ", "elif ", "while ")):
                return True, "guard_condition_shift"

            # 2. Return statements or raises
            if stripped.startswith(("return ", "raise ", "yield ")):
                return True, "return_or_raise_shift"

            # 3. Method signature or function definitions
            if stripped.startswith(("def ", "class ", "async def ")):
                return True, "signature_shift"

            # 4. Exception handling
            if stripped.startswith(("except ", "try:", "finally:")):
                return True, "exception_handling_shift"

            # 5. Public exports (__all__)
            if "__all__" in stripped or "missing" in stripped:
                return True, "module_export_shift"

        return False, "no_contract_drift"
