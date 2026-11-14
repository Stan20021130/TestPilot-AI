"""
TestPilot AI — Diff Agent.

Reads the GitHub PR diff, extracts changed files, and identifies
specific code symbols (functions, classes, routes) that were modified.

Uses:
- GitHub API to get the diff
- AST parser to identify changed symbols precisely
- Git line-range analysis to know which functions were touched

Output state keys:
- diff: raw diff metadata
- changed_files: list of ChangedFile
- changed_nodes: list of CodeNode (functions/classes modified)
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.state import AgentState, ChangedFile, CodeNode, decision
from app.core.logging import get_logger
from app.services.ast_parser import ASTParser
from app.services.contract_extractor import contracts_from_parse_result
from app.services.github_service import GitHubService
from app.services.pr_checkout import resolve_repo_root
from app.utils.patch_parser import lines_intersect

logger = get_logger(__name__)

# Upper bound on symbols per changed file, to keep the fan-out prompt bounded.
MAX_NODES_PER_FILE = 40


def diff_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: extract and analyze the PR diff.

    Fetches the PR from GitHub, enumerates changed files, and uses
    the AST parser to identify which specific functions, classes, and
    API routes were modified within the changed line ranges.

    Args:
        state: Current AgentState.

    Returns:
        Partial state update with diff, changed_files, and changed_nodes.
    """
    start_time = time.monotonic()
    logger.info(
        "Diff agent started",
        pr_id=state.get("pr_id"),
        repo=state.get("repo_full_name"),
        pr_number=state.get("pr_number"),
    )

    try:
        github_service = GitHubService()
        pr = github_service.get_pull_request(
            repo_full_name=state["repo_full_name"],
            pr_number=state["pr_number"],
            installation_id=state.get("installation_id"),
        )

        diff_summary = github_service.extract_diff_summary(pr)

        changed_files: list[ChangedFile] = [
            ChangedFile(
                path=f.path,
                status=f.status,
                additions=f.additions,
                deletions=f.deletions,
                old_path=f.old_path,
                changed_lines=list(f.changed_lines),
            )
            for f in diff_summary.changed_files
        ]

        # Identify changed code nodes using AST
        changed_nodes, change_contracts = _identify_changed_nodes(state, changed_files)

        duration = time.monotonic() - start_time
        logger.info(
            "Diff agent completed",
            changed_files=len(changed_files),
            changed_nodes=len(changed_nodes),
            contracts=len(change_contracts),
            duration_ms=int(duration * 1000),
        )

        return {
            "diff": {
                "total_additions": pr.additions,
                "total_deletions": pr.deletions,
                "files_changed": pr.changed_files,
                "affected_languages": diff_summary.affected_languages,
            },
            "changed_files": changed_files,
            "changed_nodes": changed_nodes,
            # Current contract of each changed symbol (signature, params, return,
            # decorators, intent) — what the generator should assert against.
            "change_contracts": change_contracts,
            "completed_agents": ["diff_agent"],
            "decisions": [
                decision(
                    "diff_agent",
                    "analyze" if changed_nodes else "no_symbols",
                    f"{len(changed_files)} changed file(s), "
                    f"{len(changed_nodes)} symbol(s) intersect the changed lines",
                    inputs={
                        "changed_files": len(changed_files),
                        "changed_nodes": len(changed_nodes),
                        "contracts": len(change_contracts),
                    },
                )
            ],
        }

    except Exception as e:
        logger.exception("Diff agent failed", error=str(e))
        return {
            "errors": [f"diff_agent: {e}"],
            "changed_files": [],
            "changed_nodes": [],
            "change_contracts": [],
        }


def _filter_nodes_by_changed_lines(
    nodes: list[CodeNode],
    changed_lines: list[int],
) -> tuple[list[CodeNode], bool]:
    """Keep only the symbols that intersect the touched line ranges.

    Args:
        nodes: Every symbol the AST parser found in the file.
        changed_lines: New-file line numbers touched by the PR.

    Returns:
        ``(kept_nodes, used_fallback)``. ``used_fallback`` is True when the
        diff carried no line information (binary/truncated patch) or when the
        change only touched module-level code, in which case every symbol in
        the file is retained — a module-level edit can affect all of them.
    """
    if not changed_lines:
        return list(nodes)[:MAX_NODES_PER_FILE], True

    touched = set(changed_lines)
    kept = [
        node for node in nodes if lines_intersect(node["line_start"], node["line_end"], touched)
    ]
    if kept:
        return kept[:MAX_NODES_PER_FILE], False
    return list(nodes)[:MAX_NODES_PER_FILE], True


def _identify_changed_nodes(
    state: AgentState,
    changed_files: list[ChangedFile],
) -> tuple[list[CodeNode], list[dict]]:
    """Identify specific code symbols modified in the PR.

    For each changed file, runs AST parsing and cross-references
    the changed line ranges to determine which functions/classes
    were actually modified (not just nearby code).

    Args:
        state: Current AgentState (contains repository path info).
        changed_files: List of changed files from the diff.

    Returns:
        ``(changed_nodes, change_contracts)`` where contracts describe the
        current signature/intent of every modified symbol.
    """
    parser = ASTParser()
    changed_nodes: list[CodeNode] = []
    change_contracts: list[dict] = []

    repo_path = resolve_repo_root(state)
    fallback_files = 0

    for changed_file in changed_files:
        if changed_file["status"] == "deleted":
            continue

        file_path = repo_path / changed_file["path"]
        if not file_path.exists():
            continue

        result = parser.parse_file(file_path)
        if result is None:
            continue

        file_nodes: list[CodeNode] = []

        for fn in result.functions:
            file_nodes.append(
                CodeNode(
                    name=fn.name,
                    type="function",
                    file_path=changed_file["path"],
                    line_start=fn.line_start,
                    line_end=fn.line_end,
                    language=result.language,
                )
            )

        for cls in result.classes:
            file_nodes.append(
                CodeNode(
                    name=cls.name,
                    type="class",
                    file_path=changed_file["path"],
                    line_start=cls.line_start,
                    line_end=cls.line_end,
                    language=result.language,
                )
            )

        for route in result.routes:
            file_nodes.append(
                CodeNode(
                    name=f"{route.method} {route.path}",
                    type="route",
                    file_path=changed_file["path"],
                    line_start=route.line,
                    line_end=route.line,
                    language=result.language,
                )
            )

        kept, used_fallback = _filter_nodes_by_changed_lines(
            file_nodes, changed_file.get("changed_lines", [])
        )
        if used_fallback and file_nodes:
            fallback_files += 1
            logger.debug(
                "Symbol-level filtering fell back to whole-file granularity",
                file=changed_file["path"],
                symbols=len(file_nodes),
                changed_lines=len(changed_file.get("changed_lines", [])),
            )
        changed_nodes.extend(kept)

        keep_pairs = {(node["type"], node["name"]) for node in kept}
        change_contracts.extend(
            contracts_from_parse_result(result, changed_file["path"], keep=keep_pairs)
        )

    if fallback_files:
        logger.info(
            "Changed-node detection used file granularity fallback",
            files=fallback_files,
            total_nodes=len(changed_nodes),
        )

    return changed_nodes, change_contracts
