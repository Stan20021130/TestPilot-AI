"""
TestPilot AI — LangGraph Agent State.

Defines the shared state TypedDict that flows through the entire
LangGraph multi-agent pipeline. Every agent reads from and writes to this
state object. Type safety is enforced via TypedDict annotations.

Diagnostic channels (``warnings`` / ``errors`` / ``completed_agents``) are
append-only and safe to write concurrently: the three context collectors run in
one superstep and each reports through them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict


def append_unique(existing: list[str], update: list[str]) -> list[str]:
    """LangGraph reducer for append-only diagnostic channels.

    ``impact_agent``, ``search_agent`` and ``test_discovery_agent`` run in the
    same superstep and all write ``warnings`` and ``completed_agents``. A plain
    channel accepts one write per step and raises ``InvalidUpdateError`` as soon
    as a second branch reports, so these keys are annotated with this reducer.

    Nodes therefore return **only their own additions**; the reducer merges the
    branches and keeps the first occurrence of each entry, so an agent that runs
    again in a feedback loop does not duplicate its diagnostics.
    """
    if not existing:
        return list(update or [])
    merged = list(existing)
    for item in update or []:
        if item not in merged:
            merged.append(item)
    return merged


# Append-only channel used for warnings, errors and the completion log.
Diagnostics = Annotated[list[str], append_unique]

# Decisions are capped: the audit trail is for explaining a run, not for
# storing an unbounded log inside every checkpoint.
MAX_DECISIONS = 200


class Decision(TypedDict, total=False):
    """One orchestration choice, with the evidence that drove it.

    Stored instead of being scattered across log lines so the PR detail can
    answer "why was no test generated / why did this loop stop" without a log
    search. ``choice`` is short (``generate`` / ``decline`` / ``repair``), the
    reason carries the human-readable explanation, and ``inputs`` keeps the
    numbers the decision was based on.
    """

    agent: str
    choice: str
    reason: str
    inputs: dict[str, Any]
    attempt: int
    at: str


def decision(
    agent: str,
    choice: str,
    reason: str,
    *,
    inputs: dict[str, Any] | None = None,
    attempt: int | None = None,
) -> Decision:
    """Build a decision record with a UTC timestamp."""
    payload: Decision = {
        "agent": agent,
        "choice": choice,
        "reason": reason,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if inputs:
        payload["inputs"] = inputs
    if attempt is not None:
        payload["attempt"] = attempt
    return payload


def append_decisions(existing: list[Decision], update: list[Decision]) -> list[Decision]:
    """Append-only reducer for the decision audit channel."""
    merged = list(existing or []) + list(update or [])
    return merged[-MAX_DECISIONS:]


Decisions = Annotated[list[Decision], append_decisions]


class ChangedFile(TypedDict):
    """A file changed in the PR diff."""

    path: str
    status: str  # 'added' | 'modified' | 'deleted' | 'renamed'
    additions: int
    deletions: int
    old_path: str | None
    # New-file line numbers added/modified (empty when the patch is unavailable)
    changed_lines: list[int]


class CodeNode(TypedDict):
    """A code symbol (function, class, route) identified in the diff."""

    name: str
    type: str  # 'function' | 'class' | 'route' | 'model'
    file_path: str
    line_start: int
    line_end: int
    language: str


class TestFile(TypedDict):
    """An existing test file discovered in the repository."""

    path: str
    framework: str
    test_count: int
    test_names: list[str]
    covers_modules: list[str]


class GeneratedTest(TypedDict):
    """An AI-generated test case."""

    function_name: str
    class_name: str | None
    file_path: str
    test_file_path: str
    content: str
    test_type: str
    test_framework: str
    language: str


class TestExecutionResult(TypedDict):
    """Result of running a test suite."""

    runner: str
    status: str
    total: int
    passed: int
    failed: int
    skipped: int
    duration_seconds: float
    coverage_percentage: float | None
    failed_tests: list[dict[str, Any]]
    logs: str


class FailureAnalysis(TypedDict):
    """AI analysis of a test failure."""

    test_name: str
    failure_message: str
    root_cause: str
    affected_code_path: str | None
    suggested_fix: str
    # 'test_defect' | 'product_defect' | 'environment' — decides whether the
    # pipeline may regenerate tests or must report the failure as-is.
    failure_origin: str
    confidence: float


class MutantResult(TypedDict, total=False):
    """Result of an adversarial mutation injected into the PR code."""

    mutant_id: str
    target_file: str
    line_number: int
    mutation_type: str  # 'operator_swap' | 'boundary_shift' | 'return_tamper'
    description: str
    original_snippet: str
    mutated_snippet: str
    status: str  # 'killed' | 'survived' | 'error'
    killer_test: str | None


class GenerationPlan(TypedDict, total=False):
    """The context phase's contract with the verify loop.

    One object answers: which files are worth testing, how strong the evidence
    is, and — when the answer is "none" — why generation was declined. The
    parent graph routes on ``declined`` instead of re-reading five state keys.
    """

    declined: bool
    decline_reason: str | None  # no_changed_symbols | context_insufficient
    context_mode: str  # full | degraded | insufficient
    context_score: int
    evidence: dict[str, bool]
    targets: list[dict[str, Any]]
    changed_symbols: int
    affected_files: int


class VerifyStep(TypedDict, total=False):
    """One action taken by the autonomous verify loop."""

    action: str
    reason: str
    status: str  # ok | error | skipped
    at: str


MAX_VERIFY_STEPS = 50


def append_steps(existing: list[VerifyStep], update: list[VerifyStep]) -> list[VerifyStep]:
    merged = list(existing or []) + list(update or [])
    return merged[-MAX_VERIFY_STEPS:]


VerifySteps = Annotated[list[VerifyStep], append_steps]


class VerifyBudget(TypedDict, total=False):
    """All verify-loop counters in one object.

    Limits and usage live together so a checkpoint carries one budget, not five
    scattered integers. The legacy flat fields remain as write-through mirrors
    for one release (old checkpoints and external callers); routing reads this
    object first.
    """

    max_rounds: int
    rounds: int
    max_generation_attempts: int
    generation_attempts: int
    max_coverage_topups: int
    coverage_attempts: int
    max_repairs: int
    repair_attempts: int
    max_mutation_attempts: int
    mutation_attempts: int


def merge_budget(left: VerifyBudget, right: VerifyBudget) -> VerifyBudget:
    """Merge partial budget updates (nodes only write the counter they used)."""
    return {**(left or {}), **(right or {})}


VerifyBudgets = Annotated[VerifyBudget, merge_budget]


class VerificationOutcome(TypedDict, total=False):
    """Why the verify loop stopped, and what it produced.

    ``exit_reason`` is a closed vocabulary: ``green``, ``no_tests``,
    ``failures_reported``, ``mutation_survived``, ``budget_exhausted``,
    ``execution_error``, ``no_progress`` or ``source_patches_proposed``.
    Review and EvalOps can aggregate it without parsing free-form text.
    """

    exit_reason: str
    engine: str  # legacy | next
    rounds: int
    generated: int
    executed: int
    passed: int
    failed: int
    mutation_score: float | None
    repaired: bool


class RiskScore(TypedDict):
    """Risk assessment for the PR."""

    level: str  # 'low' | 'medium' | 'high' | 'critical'
    score: float  # 0.0 - 10.0
    factors: list[str]
    historical_context: str | None


class PRReview(TypedDict, total=False):
    """The complete AI-generated PR review."""

    risk_level: str
    risk_score: float
    summary: str
    risk_factors: list[dict[str, str]]
    action_items: list[str]
    historical_warnings: list[str]
    full_review_body: str
    remediation_summary: str | None
    is_auto_remediated: bool
    repair_patches: list[RepairPatch] | None
    mutation_score: float | None
    mutation_summary: str | None


class RepairPatch(TypedDict):
    """An automated code or test repair patch synthesized by Repair Agent."""

    target_type: str  # 'test' | 'source_code'
    file_path: str
    original_snippet: str
    repaired_code: str
    patch_diff: str
    explanation: str
    verified: bool


class AgentState(TypedDict, total=False):
    """The shared state object for the LangGraph agent pipeline.

    This TypedDict is the single source of truth flowing between all agents.
    Each agent reads its required inputs and writes its outputs back here.

    Required fields (set at pipeline start):
        pr_id: Internal TestPilot PR UUID.
        repository_id: Internal Repository UUID.
        repo_full_name: GitHub full name (e.g., 'owner/repo').
        pr_number: GitHub PR number.
        head_sha: HEAD commit SHA.
        base_sha: Base commit SHA.
        installation_id: GitHub App installation ID.

    Populated by agents (in pipeline order):
        diff: Raw PR diff data.
        changed_files: Parsed list of changed files (with changed line numbers).
        changed_nodes: Code symbols intersecting the changed line ranges.
        dependency_graph_edges: Serialized dependency graph edges.
        affected_modules: Modules affected by the changes (flat, downstream).
        impact_details: Per-file blast radius with depth preserved.
        affected_services: High-level services affected.
        affected_apis: API endpoints affected.
        affected_tests: Existing test files that cover the changed modules.
        retrieved_context: Ranked, de-duplicated code snippets from hybrid search.
        existing_tests: Discovered test files.
        test_coverage_map: Changed file -> test files covering it.
        test_config: Detected framework, test command and config files.
        uncovered_modules: Modules lacking test coverage.
        generated_tests: AI-generated test cases.
        execution_results: Test suite execution results.
        failure_analyses: AI analyses of test failures.
        risk_score: PR risk assessment.
        review: Final AI-generated PR review.

    Audit:
        decisions: Append-only records of choices made by gates and loops.

    Control flow:
        errors: List of error messages (fatal to the current agent).
        warnings: Non-fatal degradations (degraded but completed).
        retry_count: Current retry count for the active agent.
        should_stop: Whether the pipeline should halt early.
        completed_agents: List of successfully completed agent names.
            Parallel branches append to it; there is deliberately no single
            ``current_agent`` channel, because three agents can be active in
            the same superstep and a plain channel would reject the second
            write.
    """

    # ---- Input (set at pipeline start) ----
    pr_id: str
    repository_id: str
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str
    installation_id: str | None
    # Per-run identifier: scopes scratch dirs, report files and generated test names
    run_id: str
    # Tree the agents read: PR head worktree when resolvable, else the default clone
    workspace_path: str
    checkout_mode: str  # "head" | "base"

    # ---- Diff Agent outputs ----
    diff: dict[str, Any]
    changed_files: list[ChangedFile]
    changed_nodes: list[CodeNode]
    # Current contract of each changed symbol (signature, params, return, intent)
    change_contracts: list[dict[str, Any]]

    # ---- Dependency Agent outputs ----
    dependency_graph_edges: list[dict[str, str]]

    # ---- Impact Agent outputs ----
    affected_modules: list[str]
    affected_services: list[str]
    affected_apis: list[str]
    # Per-file blast radius with traversal depth preserved (direct vs transitive)
    impact_details: list[dict[str, Any]]
    impact_radius: int
    impact_level: str  # 'isolated' | 'moderate' | 'widespread' | 'unknown'
    impact_unknown_files: list[str]
    affected_tests: list[str]

    # ---- Search Agent outputs ----
    retrieved_context: list[dict[str, Any]]
    search_metrics: dict[str, Any]

    # ---- Test Discovery Agent outputs ----
    existing_tests: list[TestFile]
    uncovered_modules: list[str]
    coverage_gaps: list[str]  # alias of uncovered_modules (kept for API compatibility)
    # changed source file -> existing test files that cover it
    test_coverage_map: dict[str, list[str]]
    test_config: dict[str, Any]

    # ---- Risk Ranker Agent outputs ----
    # Ranked answer to "where should tests be written, and why"
    test_targets: list[dict[str, Any]]
    test_history: dict[str, dict[str, Any]]

    # ---- Context Controller Agent outputs ----
    context_health: dict[str, Any]
    # Boundary contract handed to the verify loop (and to review/telemetry).
    generation_plan: GenerationPlan

    # ---- Test Generator Agent outputs ----
    generated_tests: list[GeneratedTest]
    # Generation and coverage-top-up counters live in ``budget``.

    # ---- Execution Agent outputs ----
    execution_results: TestExecutionResult
    # "selected" = only tests covering the change + generated tests, "full" = whole suite
    execution_scope: str
    selected_test_count: int
    # Hash of the current/previous failing test set, used to detect a repair
    # round that changed nothing (same signature twice → stop).
    failure_signature: str
    previous_failure_signature: str

    # ---- Verify loop control ----
    # Single source of truth for limits and usage (rounds, generation, repair,
    # mutation). Routing falls back to legacy flat keys so old checkpoints can
    # still be resumed, but no node writes them anymore.
    budget: VerifyBudgets
    verification: VerificationOutcome
    # Action ledger for the autonomous/shadow controller. In deterministic mode
    # this stays empty; decisions carry the same information.
    verify_steps: VerifySteps
    # Action chosen by the controller for the next step (autonomous mode).
    next_action: str

    # ---- Failure Analysis Agent outputs ----
    failure_analyses: list[FailureAnalysis]

    # ---- Adversarial Mutation Arena outputs ----
    mutation_results: list[MutantResult]
    mutation_score: float | None  # 0.0 - 1.0 (killed / total)
    survived_mutants: list[MutantResult]

    # ---- Repair Agent outputs ----
    repair_patches: list[RepairPatch]
    # Number of generated-test patches applied in the last repair pass. Source
    # patches are proposals only, so zero means "do not re-execute".
    repair_applied_tests: int
    is_auto_remediated: bool

    # ---- Review Agent outputs ----
    risk_score: RiskScore
    review: PRReview

    # ---- Documentation Agent outputs ----
    documentation_updates: list[dict[str, str]]

    # ---- Audit ----
    # Orchestration choices (declined generation, loop exit, fallback used).
    # Append-only with a cap, so it is safe to write from parallel branches.
    decisions: Decisions

    # ---- Control flow ----
    # All three channels are append-only and therefore safe to write from
    # parallel branches: agents write deltas, ``append_unique`` merges them.
    errors: Diagnostics
    # Non-fatal degradations (e.g. "search returned 0 results") surfaced to
    # telemetry instead of being silently swallowed.
    warnings: Diagnostics
    retry_count: int
    should_stop: bool
    completed_agents: Diagnostics
