"""TestPilot AI — Test target risk ranking.

This is the fusion step the three context agents never performed: it turns
"files affected" + "tests covering them" + "recent test history" + "contract
risk" into one ranked list of *where to write tests next*, with the reason for
each rank.

Pure and deterministic — no LLM, no I/O — so the ranking can be unit tested and
audited independently of generation quality.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.test_history import normalize_path
from app.utils.path_classifier import classify_path

# Structural risk by architectural layer.
KIND_WEIGHT: dict[str, int] = {
    "api": 15,
    "service": 10,
    "model": 8,
    "module": 4,
    "test": 0,
}

PRIORITY_CRITICAL = 70
PRIORITY_HIGH = 50
PRIORITY_MEDIUM = 30

MAX_SYMBOLS_LISTED = 8


def _priority_for(score: int) -> str:
    if score >= PRIORITY_CRITICAL:
        return "critical"
    if score >= PRIORITY_HIGH:
        return "high"
    if score >= PRIORITY_MEDIUM:
        return "medium"
    return "low"


def rank_test_targets(
    *,
    changed_nodes: Sequence[Mapping[str, Any]],
    impact_details: Sequence[Mapping[str, Any]],
    coverage_map: Mapping[str, list[str]],
    uncovered: Sequence[str] = (),
    history: Mapping[str, Mapping[str, Any]] | None = None,
    contracts: Sequence[Mapping[str, Any]] = (),
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Rank the changed files by how much they need a test.

    Signals, in rough order of weight:

    - no existing test covers the file (+40)
    - the file has direct dependents in the import graph (+25 / +10 transitive)
    - architectural layer: api (+15), service (+10), model (+8)
    - a public symbol changed (+10)
    - the covering test failed recently (+20) or is flaky (+10) or slow (+5)

    Args:
        changed_nodes: Symbols identified by the Diff Agent.
        impact_details: ``[{file, depth, kind}]`` from the Impact Agent.
        coverage_map: Changed file -> covering test files.
        uncovered: Changed files with no covering test.
        history: ``test_file -> {failures, flaky, slow_tests, ...}``.
        contracts: Change contracts from the Diff Agent.
        limit: Maximum number of targets to return.

    Returns:
        Ranked target dictionaries with score, priority, symbols, tests and
        human-readable reasons.
    """
    history = history or {}

    nodes_by_file: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for node in changed_nodes:
        nodes_by_file[normalize_path(node.get("file_path"))].append(node)

    contracts_by_file: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for contract in contracts:
        contracts_by_file[normalize_path(contract.get("file_path"))].append(contract)

    tests_by_file: dict[str, list[str]] = {
        normalize_path(path): list(tests) for path, tests in coverage_map.items()
    }
    uncovered_set = {normalize_path(path) for path in uncovered}

    direct_dependents = sum(1 for entry in impact_details if entry.get("depth") == 1)
    transitive_dependents = sum(1 for entry in impact_details if entry.get("depth", 0) > 1)

    candidates = set(nodes_by_file) | uncovered_set | set(tests_by_file)
    candidates.discard("")

    targets: list[dict[str, Any]] = []

    for file_path in candidates:
        if classify_path(file_path) == "test":
            continue

        score = 0
        reasons: list[str] = []

        kind = classify_path(file_path)
        score += KIND_WEIGHT.get(kind, KIND_WEIGHT["module"])
        if kind != "module":
            reasons.append(f"{kind} layer change")

        nodes = nodes_by_file.get(file_path, [])
        if nodes:
            score += min(10, 2 * len(nodes))
            reasons.append(f"{len(nodes)} changed symbol(s)")

        covering_tests = tests_by_file.get(file_path, [])
        if file_path in uncovered_set or not covering_tests:
            score += 40
            reasons.append("no existing test covers this file")

        if direct_dependents:
            score += 25
            reasons.append(f"{direct_dependents} direct dependent module(s)")
        elif transitive_dependents:
            score += 10
            reasons.append(f"{transitive_dependents} transitive dependent module(s)")

        file_contracts = contracts_by_file.get(file_path, [])
        if any(contract.get("public") for contract in file_contracts):
            score += 10
            reasons.append("public symbol contract changed")

        file_history: dict[str, Any] = {}
        for test_file in covering_tests:
            stats = history.get(normalize_path(test_file))
            if not stats:
                continue
            file_history[test_file] = dict(stats)
            if stats.get("failures"):
                score += 20
                reasons.append(f"covering test failed in {stats['failures']} recent result(s)")
                break
        if covering_tests:
            flaky = sum(
                int(history.get(normalize_path(test), {}).get("flaky", 0))
                for test in covering_tests
            )
            slow = sum(
                int(history.get(normalize_path(test), {}).get("slow_tests", 0))
                for test in covering_tests
            )
            if flaky:
                score += 10
                reasons.append(f"{flaky} flaky test(s) in this area")
            if slow:
                score += 5
                reasons.append(f"{slow} slow test(s) in this area")

        score = min(score, 100)

        targets.append(
            {
                "file": file_path,
                "score": score,
                "priority": _priority_for(score),
                "kind": kind,
                "symbols": [str(node.get("name", "")) for node in nodes][:MAX_SYMBOLS_LISTED],
                "tests": covering_tests,
                "reasons": reasons,
                "history": file_history,
            }
        )

    targets.sort(key=lambda item: (-item["score"], item["file"]))
    return targets[:limit]
