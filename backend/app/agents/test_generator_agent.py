"""
TestPilot AI — Test Generator Agent.

Generates high-quality, style-consistent tests using LiteLLM with
structured output via Instructor. Studies the repository's existing
test patterns before generating new ones.

Context assembly is the product here: the model is given the *actual changed
source* (read from the checkout and sliced by AST line ranges), the real style
of two existing test files, the ranked hybrid-search snippets, the files with
no covering test, and the downstream modules affected by the change. Anything
that could not be loaded is reported as a warning rather than silently
degrading the prompt.

Generates:
- Unit tests
- Integration tests
- Edge case tests
- Negative tests
- Boundary tests
- Concurrency tests
- Mock-based tests
- Property-based tests
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import instructor
import litellm
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.agents.state import AgentState, GeneratedTest, decision
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.contract_extractor import render_contracts
from app.services.pr_checkout import resolve_repo_root
from app.utils.context_pruner import prune_code_context

logger = get_logger(__name__)
settings = get_settings()

# Prompt budgeting. The generator is the most expensive call in the pipeline,
# so every block is capped independently and the total stays predictable.
MAX_CHANGED_CODE_CHARS = 6000
MAX_CODE_FILES = 5
MAX_SYMBOLS_PER_FILE = 6
MAX_SYMBOL_CHARS = 1200
MAX_STYLE_EXAMPLES = 2
MAX_STYLE_CHARS = 900
MAX_CONTEXT_SNIPPETS = 6
MAX_CONTEXT_CHARS = 4500
MAX_TARGETS_LISTED = 8
MAX_CONTRACTS_LISTED = 10


# ==============================================================================
# Structured output schema for LLM
# ==============================================================================


class SingleTest(BaseModel):
    """A single generated test case."""

    function_name: str = Field(description="Name of the test function/method")
    test_type: str = Field(description="Type: unit|integration|edge_case|negative|boundary|mock")
    content: str = Field(description="Complete test code snippet")
    description: str = Field(description="What this test validates")


class GeneratedTestFile(BaseModel):
    """A generated test file with multiple test cases."""

    test_file_path: str = Field(description="Relative path where this test file should be written")
    imports: str = Field(description="Import statements needed")
    setup_code: str = Field(description="Fixtures, setUp, or test class setup code", default="")
    tests: list[SingleTest] = Field(description="The test functions/methods")
    framework: str = Field(description="Test framework: pytest|jest|junit|go_test")
    language: str = Field(description="Programming language")


class GeneratedTestSuite(BaseModel):
    """Complete generated test suite for a PR."""

    test_files: list[GeneratedTestFile]
    coverage_rationale: str = Field(description="Explanation of what coverage is achieved")


# ==============================================================================
# Agent Node
# ==============================================================================


def test_generator_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: generate missing tests using LiteLLM + Instructor.

    Analyzes the changed code and existing tests to generate new test cases
    that follow the repository's conventions and style.

    Args:
        state: Current AgentState.

    Returns:
        Partial state with generated_tests and warnings.
    """
    start_time = time.monotonic()
    logger.info("Test generator agent started", pr_id=state.get("pr_id"))

    warnings: list[str] = []

    try:
        changed_nodes = state.get("changed_nodes", [])

        if not changed_nodes:
            logger.info("No changed nodes, skipping test generation")
            return {
                "generated_tests": [],
                "completed_agents": ["test_generator_agent"],
                "warnings": warnings,
                "decisions": [
                    decision(
                        "test_generator_agent",
                        "skip",
                        "no changed symbols to write tests for",
                    )
                ],
            }

        # The Context Controller owns the stop right: without contracts and
        # without source snippets, generating tests would be guessing.
        plan = state.get("generation_plan") or {}
        health = state.get("context_health") or {}
        mode = health.get("mode") or plan.get("context_mode")
        if plan.get("declined") or mode == "insufficient":
            decline_reason = plan.get("decline_reason") or "context_insufficient"
            warnings.append(
                "test_generator_agent: skipped generation — "
                f"{decline_reason} (context mode={mode}, checks={health.get('checks')})"
            )
            logger.warning(
                "Test generation skipped due to insufficient context",
                score=health.get("score"),
                checks=health.get("checks"),
            )
            return {
                "generated_tests": [],
                "warnings": warnings,
                "completed_agents": ["test_generator_agent"],
                "decisions": [
                    decision(
                        "test_generator_agent",
                        "decline",
                        f"{decline_reason} — refusing to guess tests",
                        inputs={
                            "score": health.get("score") or plan.get("context_score"),
                            "checks": health.get("checks") or plan.get("evidence"),
                        },
                        attempt=int(
                            (state.get("budget") or {}).get(
                                "generation_attempts",
                                state.get("generation_attempts", 0),
                            )
                            or 0
                        )
                        + 1,
                    )
                ],
            }

        budget = dict(state.get("budget") or {})
        generation_attempts = (
            int(budget.get("generation_attempts", state.get("generation_attempts", 0)) or 0) + 1
        )
        generated, generation_warnings, meta = _generate_tests(state=state)
        warnings.extend(generation_warnings)
        # A regeneration pass must not silently drop tests that already passed;
        # new definitions win on (file, function), old ones survive otherwise.
        # Tests that just failed are replaced, not preserved.
        if generation_attempts > 1 and state.get("generated_tests"):
            failed = {
                str(item.get("name", ""))
                for item in (state.get("execution_results") or {}).get("failed_tests", [])
            }
            preserved = [
                test
                for test in state["generated_tests"]
                if not any(
                    name == test.get("function_name")
                    or name.endswith(f"::{test.get('function_name')}")
                    for name in failed
                )
            ]
            generated = _merge_generated(generated, preserved)
        coverage_attempts = int(meta.get("coverage_attempts", 0) or 0)
        budget.update(
            {
                "generation_attempts": generation_attempts,
                "coverage_attempts": coverage_attempts,
            }
        )

        duration = time.monotonic() - start_time
        logger.info(
            "Test generator agent completed",
            generated_tests=len(generated),
            generation_attempt=generation_attempts,
            coverage_attempts=coverage_attempts,
            warnings=len(generation_warnings),
            duration_ms=int(duration * 1000),
        )

        return {
            "generated_tests": generated,
            "budget": budget,
            "warnings": warnings,
            "completed_agents": ["test_generator_agent"],
            "decisions": [
                decision(
                    "test_generator_agent",
                    "generate",
                    f"generated {len(generated)} test(s) on attempt {generation_attempts}"
                    + (
                        " with failure feedback from the previous attempt"
                        if state.get("failure_analyses")
                        else ""
                    )
                    + (
                        " fortified after surviving mutants"
                        if state.get("survived_mutants")
                        else ""
                    ),
                    inputs={
                        "generated": len(generated),
                        "coverage_topups": coverage_attempts,
                        "targets": len(state.get("test_targets", [])),
                    },
                    attempt=generation_attempts,
                )
            ],
        }

    except Exception as e:
        logger.exception("Test generator agent failed", error=str(e))
        return {"errors": [f"test_generator_agent: {e}"], "generated_tests": []}


# ==============================================================================
# Context assembly
# ==============================================================================


def _repo_root(state: AgentState) -> Path:
    return resolve_repo_root(state)


def _read_repo_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _load_changed_code(
    state: AgentState,
    changed_nodes: Sequence[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """Read and slice the actual source of the changed symbols.

    Args:
        state: Agent state (for the repository checkout path).
        changed_nodes: Symbols identified by the Diff Agent.

    Returns:
        ``(code_block, warnings)`` — a prompt-ready block of real source code.
    """
    warnings: list[str] = []
    repo_root = _repo_root(state)
    if not repo_root.exists():
        return "", [
            "test_generator_agent: repository checkout not found — prompt has no source code"
        ]

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for node in changed_nodes:
        grouped.setdefault(str(node.get("file_path", "")), []).append(node)

    blocks: list[str] = []
    used_chars = 0

    for file_path, nodes in list(grouped.items())[:MAX_CODE_FILES]:
        source = _read_repo_file(repo_root / file_path)
        if not source:
            warnings.append(f"test_generator_agent: could not read changed file {file_path}")
            continue

        lines = source.splitlines()
        symbol_blocks: list[str] = []

        for node in nodes[:MAX_SYMBOLS_PER_FILE]:
            start = int(node.get("line_start") or 1)
            end = int(node.get("line_end") or start)
            start = max(1, start)
            end = min(len(lines), max(start, end))
            snippet = "\n".join(lines[start - 1 : end])[:MAX_SYMBOL_CHARS]
            if not snippet.strip():
                continue
            symbol_blocks.append(
                f"  # {node.get('type', 'symbol')}: {node.get('name', '')} "
                f"(lines {start}-{end})\n{snippet}"
            )

        if not symbol_blocks:
            continue

        block = f"### FILE: {file_path}\n" + "\n\n".join(symbol_blocks)
        if used_chars + len(block) > MAX_CHANGED_CODE_CHARS:
            remaining = MAX_CHANGED_CODE_CHARS - used_chars
            if remaining > 200:
                blocks.append(block[:remaining])
            break
        blocks.append(block)
        used_chars += len(block)

    return "\n\n".join(blocks), warnings


def _load_style_examples(
    state: AgentState,
    existing_tests: Sequence[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """Load snippets of existing tests so the model copies real conventions."""
    if not existing_tests:
        return "", []

    repo_root = _repo_root(state)
    if not repo_root.exists():
        # Fall back to metadata-only examples (path, framework, test names).
        header = "\n".join(
            f"- {t['path']} ({t['framework']}): {', '.join(t['test_names'][:5])}"
            for t in existing_tests[:3]
        )
        return header, []

    blocks: list[str] = []
    for test_file in existing_tests[:MAX_STYLE_EXAMPLES]:
        content = _read_repo_file(repo_root / str(test_file["path"]))
        if not content:
            continue
        blocks.append(
            f"### {test_file['path']} ({test_file.get('framework', 'unknown')})\n"
            f"{content[:MAX_STYLE_CHARS]}"
        )
    return "\n\n".join(blocks), []


def _render_context(retrieved_context: Sequence[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Rank, de-duplicate, prune and budget the retrieved snippets."""
    if not retrieved_context:
        return "", {"baseline_chars": 0, "pruned_chars": 0, "reduction_percent": 0.0}

    seen: set[tuple[str, str]] = set()
    ranked: list[dict[str, Any]] = []
    for item in sorted(
        retrieved_context, key=lambda entry: float(entry.get("score", 0.0)), reverse=True
    ):
        file_path = str(item.get("file_path", ""))
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        key = (file_path, content[:120])
        if key in seen:
            continue
        seen.add(key)
        ranked.append(
            {
                "file_path": file_path,
                "content": content,
                "relevance_score": float(item.get("score", 0.0)),
                "symbol": item.get("symbol") or item.get("function_name"),
            }
        )
        if len(ranked) >= MAX_CONTEXT_SNIPPETS:
            break

    pruned, stats = prune_code_context(ranked, max_char_limit=MAX_CONTEXT_CHARS)

    blocks: list[str] = []
    used = 0
    # prune_code_context preserves order and length but drops extra keys, so
    # pair it back with the ranked entry to keep the symbol label.
    for item, original in zip(pruned, ranked, strict=False):
        symbol = f" ({original.get('symbol')})" if original.get("symbol") else ""
        block = f"### {item['file_path']}{symbol}\n{item['content']}"
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        used += len(block)

    return "\n\n".join(blocks), stats


def _render_targets(targets: Sequence[Mapping[str, Any]]) -> str:
    """Render ranked test targets as a prompt block."""
    lines: list[str] = []
    for target in targets[:MAX_TARGETS_LISTED]:
        reasons = "; ".join(str(item) for item in target.get("reasons", [])[:3])
        tests = target.get("tests") or []
        covering = f" | covered by: {', '.join(tests[:3])}" if tests else " | no covering test"
        lines.append(
            f"- [{target.get('priority', 'low').upper()}] {target.get('file')} "
            f"(score {target.get('score', 0)}) — {reasons}{covering}"
        )
    return "\n".join(lines)


def _build_prompt(
    *,
    changed_summary: str,
    changed_code: str,
    contracts_block: str,
    style_block: str,
    context_block: str,
    targets_block: str,
    test_config: Mapping[str, Any],
    feedback_block: str = "",
) -> str:
    """Assemble the generation prompt from the pre-computed context blocks."""
    framework = test_config.get("framework") or "pytest"
    test_command = test_config.get("test_command") or "unknown"
    config_files = ", ".join(test_config.get("config_files", []) or []) or "none detected"
    conftest = "yes" if test_config.get("has_conftest") else "no"
    fixtures = "yes" if test_config.get("has_fixtures") else "no"
    feedback_section = (
        "\n## Feedback From The Previous Attempt\n"
        "The previous suite failed in the sandbox. Fix these specific mistakes; do not\n"
        "repeat them, and do not change tests that already passed.\n"
        f"{feedback_block}\n"
        if feedback_block
        else ""
    )

    return f"""You are an expert software engineer generating production-quality tests.

## Changed Code (verbatim from the repository)
{changed_code or "Source code could not be loaded — rely only on the contracts below."}

## Symbol Contracts (signature and intent of what changed)
{contracts_block or "Contracts unavailable — derive them from the code above."}

## Changed Symbols
{changed_summary}

## Test Priorities (highest risk first)
{targets_block or "No ranked targets available; cover the changed symbols above."}
{feedback_section}
## Existing Test Style (follow these conventions exactly)
{style_block or "No existing tests found. Use pytest with appropriate fixtures."}

## Relevant Code Context (ranked by similarity)
{context_block or "No additional context available."}

## Repository Test Conventions
- Framework: {framework}
- Test command: {test_command}
- Config files: {config_files}
- conftest.py present: {conftest}
- Shared fixtures directory present: {fixtures}

## Instructions
1. Work through "Test Priorities" in order; each entry explains why it matters.
2. Assert the behaviour promised by "Symbol Contracts": parameters, return type,
   raised errors and documented intent. Do not test internals the contract does
   not promise.
3. Follow the EXACT testing style and conventions of the existing tests.
4. Do NOT invent APIs, fixtures, or imports that are not visible above. Use only
   the real signatures and behaviour shown in "Changed Code" / "Symbol Contracts".
5. Include these test types where appropriate:
   - Happy path (normal flow)
   - Edge cases (empty input, None, zero, empty list)
   - Negative cases (invalid input, should raise exceptions)
   - Boundary values (min/max values)
   - Mock dependencies where appropriate
6. Each test must be independent and idempotent.
7. Use descriptive test names that explain the scenario.
8. Add docstrings to each test explaining what it validates.
"""


def _resolve_source_file(
    test_file_path: str,
    content: str,
    changed_nodes: Sequence[Mapping[str, Any]],
) -> str:
    """Attribute a generated test file to the changed source it targets.

    The previous implementation attributed every generated test to
    ``changed_nodes[0]["file_path"]``, which mislabels multi-file PRs.
    """
    if not changed_nodes:
        return "unknown"

    test_stem = Path(test_file_path).stem.lower()
    for node in changed_nodes:
        source_stem = Path(str(node.get("file_path", ""))).stem.lower()
        if source_stem and source_stem in test_stem:
            return str(node["file_path"])

    for node in changed_nodes:
        name = str(node.get("name", ""))
        if name and re.search(rf"\b{re.escape(name)}\b", content):
            return str(node["file_path"])

    return str(changed_nodes[0]["file_path"])


MAX_TOPUP_TARGETS = 3


def _build_feedback_block(state: AgentState) -> str:
    """Turn previous test-defect failures AND survived mutants into feedback."""
    feedback_parts: list[str] = []
    analyses = state.get("failure_analyses") or []
    test_defects = [a for a in analyses if a.get("failure_origin") == "test_defect"]
    if test_defects:
        feedback_parts.append("### Previous Test Failures (Fix these test defects):")
        feedback_parts.extend(
            f"- `{analysis.get('test_name')}`: {analysis.get('root_cause')} "
            f"→ {analysis.get('suggested_fix')}"
            for analysis in test_defects[:5]
        )

    survived = state.get("survived_mutants") or []
    if survived:
        feedback_parts.append(
            "### Adversarial Mutants Survived (Strengthen assertions to catch these synthetic bugs):"
        )
        for m in survived[:5]:
            feedback_parts.append(
                f"- Line {m.get('line_number')} in `{m.get('target_file')}` ({m.get('description')}): "
                f"Original `{m.get('original_snippet')}` was mutated to `{m.get('mutated_snippet')}`, "
                "but your assertions passed! Add targeted assertions for this boundary condition."
            )

    return "\n".join(feedback_parts)


def _missing_critical_targets(
    generated: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    limit: int = MAX_TOPUP_TARGETS,
) -> list[dict[str, Any]]:
    """Critical/high targets that received no generated test at all."""
    covered = {str(test.get("file_path", "")) for test in generated}
    covered |= {str(test.get("test_file_path", "")) for test in generated}
    covered_stems = {Path(item).stem.lower() for item in covered if item}

    missing: list[dict[str, Any]] = []
    for target in targets:
        if target.get("priority") not in {"critical", "high"}:
            continue
        file_path = str(target.get("file", ""))
        if file_path and any(file_path in item for item in covered):
            continue
        if Path(file_path).stem.lower() in covered_stems:
            continue
        missing.append(dict(target))
    return missing[:limit]


def _merge_generated(
    primary: Sequence[GeneratedTest],
    extra: Sequence[GeneratedTest],
) -> list[GeneratedTest]:
    """Union of two suites, de-duplicated by (file, function)."""
    seen = {(test.get("test_file_path"), test.get("function_name")) for test in primary}
    merged = list(primary)
    for test in extra:
        key = (test.get("test_file_path"), test.get("function_name"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(test)
    return merged


def _call_llm(
    client: Any,
    prompt: str,
    changed_nodes: Sequence[Mapping[str, Any]],
) -> list[GeneratedTest]:
    """Run one structured generation call and flatten the suite."""
    result, _ = client.chat.completions.create_with_completion(
        model=settings.litellm_default_model,
        messages=[{"role": "user", "content": prompt}],
        response_model=GeneratedTestSuite,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        api_key=settings.gemini_api_key or settings.openai_api_key or None,
        api_base=settings.openai_api_base or None,
    )

    generated: list[GeneratedTest] = []
    for test_file in result.test_files:
        for test in test_file.tests:
            generated.append(
                GeneratedTest(
                    function_name=test.function_name,
                    class_name=None,
                    file_path=_resolve_source_file(
                        test_file.test_file_path, test.content, changed_nodes
                    ),
                    test_file_path=test_file.test_file_path,
                    content=f"{test_file.imports}\n\n{test_file.setup_code}\n\n{test.content}",
                    test_type=test.test_type,
                    test_framework=test_file.framework,
                    language=test_file.language,
                )
            )
    return generated


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _generate_tests(
    state: AgentState,
) -> tuple[list[GeneratedTest], list[str], dict[str, Any]]:
    """Generate tests using LiteLLM with Instructor structured output.

    Two bounded behaviours live here:

    - **feedback** — on a regeneration pass the previous test-defect failures
      are injected into the prompt as corrections;
    - **coverage top-up** — if a critical target received no test, one focused
      pass runs for just those targets and the suites are merged.

    Returns:
        ``(generated_tests, warnings, meta)`` where meta carries
        ``coverage_attempts``.
    """
    warnings: list[str] = []
    changed_nodes = state.get("changed_nodes", [])
    existing_tests = state.get("existing_tests", [])
    retrieved_context = state.get("retrieved_context", [])
    test_config = state.get("test_config", {})
    contracts = list(state.get("change_contracts", []))
    targets = list(state.get("test_targets", []))
    meta: dict[str, Any] = {
        "coverage_attempts": int(
            (state.get("budget") or {}).get(
                "coverage_attempts",
                state.get("coverage_attempts", 0),
            )
            or 0
        ),
    }

    try:
        client = instructor.from_litellm(litellm.completion)
    except ImportError as e:
        logger.error("instructor or litellm not available", error=str(e))
        warnings.append("test_generator_agent: LLM client unavailable — no tests generated")
        return [], warnings, meta

    changed_code, code_warnings = _load_changed_code(state, changed_nodes)
    warnings.extend(code_warnings)
    style_block, _ = _load_style_examples(state, existing_tests)
    context_block, context_stats = _render_context(retrieved_context)
    contracts_block = render_contracts(contracts, limit=MAX_CONTRACTS_LISTED)
    targets_block = _render_targets(targets)
    feedback_block = _build_feedback_block(state)

    if not changed_code:
        warnings.append(
            "test_generator_agent: prompt contains no source code — assertions rely on "
            "the extracted contracts"
        )

    changed_summary = "\n".join(
        f"- {node['type'].upper()}: {node['name']} in {node['file_path']}"
        for node in changed_nodes[:20]
    )

    prompt = _build_prompt(
        changed_summary=changed_summary,
        changed_code=changed_code,
        contracts_block=contracts_block,
        style_block=style_block,
        context_block=context_block,
        targets_block=targets_block,
        test_config=test_config,
        feedback_block=feedback_block,
    )

    try:
        generated = _call_llm(client, prompt, changed_nodes)
    except Exception as e:
        logger.warning("LLM generation failed", error=str(e))
        warnings.append(f"test_generator_agent: LLM generation failed ({e}) — no tests generated")
        return [], warnings, meta

    logger.info(
        "LLM test generation succeeded",
        count=len(generated),
        model=settings.litellm_default_model,
        prompt_chars=len(prompt),
        context_reduction=context_stats.get("reduction_percent"),
        targets=len(targets),
        contracts=len(contracts),
        feedback=bool(feedback_block),
    )

    # ---- Coverage top-up for critical targets that got nothing -------------
    missing = _missing_critical_targets(generated, targets)
    max_topups = int(
        (state.get("budget") or {}).get(
            "max_coverage_topups",
            state.get("max_coverage_topups", 1),
        )
        or 0
    )

    if missing and meta["coverage_attempts"] < max_topups:
        focus_block = _render_targets(missing)
        focus_prompt = _build_prompt(
            changed_summary=changed_summary,
            changed_code=changed_code,
            contracts_block=contracts_block,
            style_block=style_block,
            context_block=context_block,
            targets_block=focus_block,
            test_config=test_config,
            feedback_block=(
                "Focused top-up pass: only the targets listed above need tests — they "
                "received no coverage in the previous pass."
            ),
        )
        try:
            extra = _call_llm(client, focus_prompt, changed_nodes)
            generated = _merge_generated(generated, extra)
            meta["coverage_attempts"] += 1
            warnings.append(
                "test_generator_agent: coverage top-up generated "
                f"{len(extra)} test(s) for {len(missing)} uncovered critical target(s)"
            )
            logger.info(
                "Coverage top-up completed",
                targets=len(missing),
                generated=len(extra),
            )
        except Exception as e:
            warnings.append(f"test_generator_agent: coverage top-up failed ({e})")

    return generated, warnings, meta
