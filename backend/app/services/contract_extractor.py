"""TestPilot AI — Change contract extraction.

The generator needs to assert *behaviour*, which requires the symbol's contract:
its signature, parameters, return type, decorators and intent. None of the
context agents produced that — they produced file lists and similar snippets, so
the model had to re-derive contracts from raw code every run.

This module turns AST parse results into a compact, prompt-ready contract per
changed symbol. It is deterministic (no LLM) and intentionally single-sided:
diffing against the base revision requires fetching the old blob, which is
tracked as follow-up work.
"""

from __future__ import annotations

from typing import Any

MAX_DOCSTRING_CHARS = 200
MAX_PARAMETERS = 12


def _summarize(text: str | None, limit: int = MAX_DOCSTRING_CHARS) -> str | None:
    if not text:
        return None
    collapsed = " ".join(text.strip().split())
    return collapsed[:limit] or None


def _parameters(raw: Any) -> list[str]:
    if not raw:
        return []
    params = [str(item).strip() for item in raw if str(item).strip()]
    if len(params) > MAX_PARAMETERS:
        params = [*params[:MAX_PARAMETERS], f"... (+{len(raw) - MAX_PARAMETERS} more)"]
    return params


def build_function_contract(fn: Any, file_path: str) -> dict[str, Any]:
    """Build a contract record from an AST ``FunctionInfo``."""
    params = _parameters(getattr(fn, "parameters", None))
    return_type = getattr(fn, "return_type", None)
    is_async = bool(getattr(fn, "is_async", False))
    name = str(fn.name)

    signature = f"{'async ' if is_async else ''}def {name}({', '.join(params)})"
    if return_type:
        signature += f" -> {return_type}"

    return {
        "symbol": name,
        "type": "function",
        "file_path": file_path,
        "signature": signature,
        "parameters": params,
        "return_type": return_type,
        "is_async": is_async,
        "decorators": [str(d).strip() for d in (getattr(fn, "decorators", None) or [])],
        "docstring": _summarize(getattr(fn, "docstring", None)),
        "class_name": getattr(fn, "class_name", None),
        "public": not name.startswith("_"),
        "line_start": getattr(fn, "line_start", None),
        "line_end": getattr(fn, "line_end", None),
    }


def build_class_contract(cls: Any, file_path: str) -> dict[str, Any]:
    """Build a contract record from an AST ``ClassInfo``."""
    name = str(cls.name)
    bases = [str(b).strip() for b in (getattr(cls, "base_classes", None) or [])]
    methods = [str(m).strip() for m in (getattr(cls, "methods", None) or [])]

    signature = f"class {name}" + (f"({', '.join(bases)})" if bases else "")

    return {
        "symbol": name,
        "type": "class",
        "file_path": file_path,
        "signature": signature,
        "parameters": bases,
        "return_type": None,
        "is_async": False,
        "decorators": [str(d).strip() for d in (getattr(cls, "decorators", None) or [])],
        "docstring": _summarize(getattr(cls, "docstring", None)),
        "class_name": None,
        "public": not name.startswith("_"),
        "methods": methods[:MAX_PARAMETERS],
        "line_start": getattr(cls, "line_start", None),
        "line_end": getattr(cls, "line_end", None),
    }


def contracts_from_parse_result(
    result: Any,
    file_path: str,
    keep: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Extract contracts for every symbol (optionally filtered to ``keep``).

    Args:
        result: AST ``ParseResult`` for the file.
        file_path: Repo-relative path of the file.
        keep: Optional set of ``(type, name)`` pairs to restrict to the symbols
            that actually intersect the changed lines.

    Returns:
        List of contract dictionaries.
    """
    contracts: list[dict[str, Any]] = []

    for fn in getattr(result, "functions", None) or []:
        if keep is not None and ("function", fn.name) not in keep:
            continue
        contracts.append(build_function_contract(fn, file_path))

    for cls in getattr(result, "classes", None) or []:
        if keep is not None and ("class", cls.name) not in keep:
            continue
        contracts.append(build_class_contract(cls, file_path))

    return contracts


def render_contracts(contracts: list[dict[str, Any]], limit: int = 8) -> str:
    """Render contracts as a compact prompt block."""
    if not contracts:
        return ""

    lines: list[str] = []
    for contract in contracts[:limit]:
        lines.append(f"- {contract['signature']}  [{contract['file_path']}]")
        if contract.get("decorators"):
            lines.append(f"  decorators: {', '.join(contract['decorators'][:4])}")
        if contract.get("docstring"):
            lines.append(f"  intent: {contract['docstring']}")
        if contract.get("return_type"):
            lines.append(f"  returns: {contract['return_type']}")
        if contract.get("methods"):
            lines.append(f"  methods: {', '.join(contract['methods'][:8])}")
    return "\n".join(lines)
