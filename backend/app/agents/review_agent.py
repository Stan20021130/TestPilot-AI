"""TestPilot AI — Review Agent.

Synthesizes all agent outputs into a structured, actionable GitHub PR review.
Incorporates risk scoring, failure analysis, coverage deltas, and historical
bug patterns to produce a review like a senior engineer would write.
"""

from __future__ import annotations

import time
from typing import Any

import instructor
import litellm
from pydantic import BaseModel, Field

from app.agents.state import AgentState, PRReview, RiskScore, decision
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class PRReviewOutput(BaseModel):
    """Structured PR review from LLM."""

    risk_level: str = Field(description="Overall risk: low|medium|high|critical")
    risk_score: float = Field(description="Numeric risk score 0.0-10.0", ge=0.0, le=10.0)
    summary: str = Field(description="2-3 sentence executive summary")
    risk_factors: list[dict[str, str]] = Field(description="List of risk factors with severity")
    action_items: list[str] = Field(description="Ordered list of recommended actions")
    historical_warnings: list[str] = Field(
        description="Warnings from historical bug patterns", default_factory=list
    )
    remediation_summary: str | None = Field(
        description="Summary of autonomous remediation and sandbox verification", default=None
    )
    is_auto_remediated: bool = Field(
        description="Whether failures were auto-repaired and verified in sandbox", default=False
    )
    markdown_review: str = Field(description="Complete Markdown-formatted review body for GitHub")


def review_agent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: generate the final AI PR review.

    Args:
        state: Current AgentState with all prior agent outputs.

    Returns:
        Partial state with risk_score and review.
    """
    start_time = time.monotonic()
    logger.info("Review agent started", pr_id=state.get("pr_id"))

    try:
        review_output = _generate_review(state)

        risk_score = RiskScore(
            level=review_output.risk_level,
            score=review_output.risk_score,
            factors=[f["factor"] for f in review_output.risk_factors],
            historical_context="\n".join(review_output.historical_warnings) or None,
        )

        review = PRReview(
            risk_level=review_output.risk_level,
            risk_score=review_output.risk_score,
            summary=review_output.summary,
            risk_factors=review_output.risk_factors,
            action_items=review_output.action_items,
            historical_warnings=review_output.historical_warnings,
            full_review_body=review_output.markdown_review,
            remediation_summary=review_output.remediation_summary,
            is_auto_remediated=review_output.is_auto_remediated
            or state.get("is_auto_remediated", False),
            repair_patches=state.get("repair_patches"),
            mutation_score=state.get("mutation_score"),
            mutation_summary=(
                f"{int((state.get('mutation_score') or 0) * 100)}% kill rate "
                f"({len(state.get('survived_mutants') or [])} survived)"
                if state.get("mutation_results")
                else None
            ),
        )

        duration = time.monotonic() - start_time
        logger.info(
            "Review agent completed",
            risk_level=review_output.risk_level,
            risk_score=review_output.risk_score,
            duration_ms=int(duration * 1000),
        )

        return {
            "risk_score": risk_score,
            "review": review,
            "completed_agents": ["review_agent"],
            "decisions": [
                decision(
                    "review_agent",
                    "review",
                    f"risk {review_output.risk_level} ({review_output.risk_score}/10); "
                    f"{len(review_output.action_items)} action item(s)",
                    inputs={
                        "risk_level": review_output.risk_level,
                        "risk_score": review_output.risk_score,
                        "action_items": len(review_output.action_items),
                    },
                )
            ],
        }

    except Exception as e:
        logger.exception("Review agent failed", error=str(e))
        return {
            "errors": [f"review_agent: {e}"],
            "decisions": [
                decision(
                    "review_agent",
                    "fallback",
                    f"review generation failed, emitting a degraded review: {e}",
                )
            ],
            "risk_score": RiskScore(
                level="unknown", score=0.0, factors=[], historical_context=None
            ),
            "review": PRReview(
                risk_level="unknown",
                risk_score=0.0,
                summary="Review generation failed.",
                risk_factors=[],
                action_items=[],
                historical_warnings=[],
                full_review_body="⚠️ TestPilot AI review generation encountered an error.",
            ),
        }


def _generate_review(state: AgentState) -> PRReviewOutput:
    """Generate the PR review using LiteLLM or rule-based fallback."""
    try:
        client = instructor.from_litellm(litellm.completion)
        return _llm_review(client, state)
    except Exception:
        return _rule_based_review(state)


def _llm_review(client: Any, state: AgentState) -> PRReviewOutput:
    """Generate review using LLM with structured output."""
    # Compile all context for the review
    changed_files = state.get("changed_files", [])
    affected_modules = state.get("affected_modules", [])
    affected_apis = state.get("affected_apis", [])
    impact_details = state.get("impact_details", [])
    impact_level = state.get("impact_level", "unknown")
    impact_unknown_files = state.get("impact_unknown_files", [])
    affected_tests = state.get("affected_tests", [])
    exec_results = state.get("execution_results", {})
    failure_analyses = state.get("failure_analyses", [])
    generated_tests = state.get("generated_tests", [])
    coverage_gaps = state.get("coverage_gaps", [])
    repair_patches = state.get("repair_patches", [])
    is_auto_remediated = state.get("is_auto_remediated", False)
    repair_attempts = int(
        (state.get("budget") or {}).get(
            "repair_attempts",
            state.get("repair_attempts", 0),
        )
        or 0
    )

    direct = [
        entry["file"]
        for entry in impact_details
        if entry.get("depth") == 1 and entry.get("kind") != "test"
    ]
    transitive = [
        entry["file"]
        for entry in impact_details
        if entry.get("depth", 0) > 1 and entry.get("kind") != "test"
    ]
    blast_radius_block = (
        f"- Impact level: {impact_level}\n"
        f"- Direct dependents ({len(direct)}): {', '.join(direct[:8]) or 'none'}\n"
        f"- Transitive dependents ({len(transitive)}): {', '.join(transitive[:8]) or 'none'}"
    )
    unknown_block = (
        "\n".join(f"- {path}" for path in impact_unknown_files[:8])
        if impact_unknown_files
        else "None — every changed file is present in the dependency graph."
    )
    covering_tests_block = (
        "\n".join(f"- {path}" for path in affected_tests[:10])
        or "No existing test covers the changed modules."
    )

    context_health = state.get("context_health") or {}
    test_targets = state.get("test_targets", [])
    targets_block = (
        "\n".join(
            f"- [{target['priority'].upper()}] {target['file']} (score {target['score']}): "
            f"{'; '.join(target.get('reasons', [])[:3])}"
            for target in test_targets[:8]
        )
        or "No ranked targets."
    )
    context_block = (
        f"- Mode: {context_health.get('mode', 'unknown')}\n"
        f"- Health score: {context_health.get('score', 0)}/100\n"
        f"- Checks: {context_health.get('checks', {})}\n"
        f"- Supplementary retrieval attempts: {context_health.get('retrieval_attempts', 0)}"
    )

    failures_summary = (
        "\n".join([f"- {fa['test_name']}: {fa['root_cause']}" for fa in failure_analyses[:5]])
        or "No test failures."
    )

    remediation_summary = (
        f"- Auto-Remediation Status: {'✅ Verified in Sandbox (Pass)' if is_auto_remediated else '⚠️ Fix Patches Synthesized'}\n"
        f"- Repair Cycles: {repair_attempts}\n"
        f"- Patches: {len(repair_patches)} patch(es) generated for {', '.join({p['file_path'] for p in repair_patches}) or 'N/A'}"
        if repair_patches
        else "No automated remediation needed (all tests passing or no failures)."
    )

    prompt = f"""You are a senior software engineer reviewing a Pull Request.
Write a comprehensive, actionable code review based on the analysis below.

## PR Analysis Results

### Changed Files ({len(changed_files)} files)
{chr(10).join([f"- {f['path']} (+{f['additions']}/-{f['deletions']})" for f in changed_files[:20]])}

### Affected Modules ({len(affected_modules)} modules)
{chr(10).join(affected_modules[:10])}

### Blast Radius (reverse dependency traversal)
{blast_radius_block}

### Changed Files Absent From The Dependency Graph (impact unknown, not "safe")
{unknown_block}

### Existing Tests Covering The Change
{covering_tests_block}

### Context Health
{context_block}

### Test Priorities (risk-ranked)
{targets_block}

### Affected APIs
{chr(10).join(affected_apis[:10]) or "None detected"}

### Test Results
- Verify exit: {(state.get("verification") or {}).get("exit_reason", "unknown")}
- Execution scope: {state.get("execution_scope", "full")} ({state.get("selected_test_count", 0)} test files selected)
- Total: {exec_results.get("total", "N/A")}
- Passed: {exec_results.get("passed", "N/A")}
- Failed: {exec_results.get("failed", "N/A")}
- Coverage: {exec_results.get("coverage_percentage", "N/A")}%

### Test Failures
{failures_summary}

### Autonomous Remediation & Fix Patches
{remediation_summary}

### Generated Tests
{len(generated_tests)} new tests generated for uncovered code.

### Coverage Gaps
{chr(10).join(coverage_gaps[:5]) or "None identified"}

## Your Task
Produce a thorough, constructive PR review. Be specific about risks.
Format the markdown_review as a GitHub PR comment with sections:
🔍 Overview, ⚠️ Risk Assessment, 🧪 Test Coverage, 🔥 Failures Found,
🛠️ Autonomous Remediation (Auto-Fix) (if patches exist), ✅ Generated Tests, 📋 Action Items.
"""

    result, _ = client.chat.completions.create_with_completion(
        model=settings.litellm_default_model,
        messages=[{"role": "user", "content": prompt}],
        response_model=PRReviewOutput,
        max_tokens=8192,
        temperature=0.1,
        api_key=settings.gemini_api_key or settings.openai_api_key or None,
        api_base=settings.openai_api_base or None,
    )
    return result


def _rule_based_review(state: AgentState) -> PRReviewOutput:
    """Generate a review using rule-based heuristics when LLM is unavailable."""
    changed_files = state.get("changed_files", [])
    exec_results = state.get("execution_results", {})
    state.get("failure_analyses", [])
    affected_modules = state.get("affected_modules", [])

    failed = exec_results.get("failed", 0)
    total = exec_results.get("total", 0)
    coverage = exec_results.get("coverage_percentage")

    # Calculate risk score
    risk_score = 0.0
    risk_factors = []

    if failed > 0:
        risk_score += min(failed * 1.5, 5.0)
        risk_factors.append({"factor": f"{failed} test(s) failing", "severity": "high"})

    if len(changed_files) > 20:
        risk_score += 2.0
        risk_factors.append({"factor": "Large PR with many changed files", "severity": "medium"})

    if coverage is not None and coverage < settings.coverage_threshold:
        risk_score += 1.5
        risk_factors.append(
            {"factor": f"Coverage below threshold ({coverage:.1f}%)", "severity": "medium"}
        )

    if any("auth" in m.lower() or "payment" in m.lower() for m in affected_modules):
        risk_score += 2.0
        risk_factors.append({"factor": "Auth/payment modules affected", "severity": "high"})

    impact_unknown_files = state.get("impact_unknown_files", [])
    if impact_unknown_files:
        risk_score += 1.0
        risk_factors.append(
            {
                "factor": f"Impact unknown for {len(impact_unknown_files)} changed file(s) "
                "missing from the dependency graph",
                "severity": "medium",
            }
        )

    context_health = state.get("context_health") or {}
    if context_health.get("mode") == "insufficient":
        risk_score += 2.0
        risk_factors.append(
            {
                "factor": "Insufficient context — no tests were generated for this change",
                "severity": "high",
            }
        )

    affected_tests = state.get("affected_tests", [])
    if affected_modules and not affected_tests:
        risk_score += 1.0
        risk_factors.append(
            {
                "factor": "No existing test covers the affected modules",
                "severity": "medium",
            }
        )

    risk_score = min(risk_score, 10.0)

    if risk_score >= 7.0:
        risk_level = "critical"
    elif risk_score >= 5.0:
        risk_level = "high"
    elif risk_score >= 2.5:
        risk_level = "medium"
    else:
        risk_level = "low"

    action_items = []
    if failed > 0:
        action_items.append(f"Fix {failed} failing test(s) before merging")
    if coverage is not None and coverage < settings.coverage_threshold:
        action_items.append(f"Improve test coverage to at least {settings.coverage_threshold}%")

    repair_patches = state.get("repair_patches", [])
    is_auto_remediated = state.get("is_auto_remediated", False)
    repair_attempts = int(
        (state.get("budget") or {}).get(
            "repair_attempts",
            state.get("repair_attempts", 0),
        )
        or 0
    )

    remediation_summary = None
    if repair_patches:
        status_text = (
            "Verified in Sandbox (Self-Healed)" if is_auto_remediated else "Patches Generated"
        )
        remediation_summary = f"Remediation status: {status_text}. {len(repair_patches)} patch(es) synthesized across {repair_attempts} cycle(s)."

    markdown_review = _build_markdown_review(
        state, risk_level, risk_score, risk_factors, action_items
    )

    return PRReviewOutput(
        risk_level=risk_level,
        risk_score=round(risk_score, 1),
        summary=f"PR changes {len(changed_files)} files affecting {len(affected_modules)} modules. "
        f"Risk level: {risk_level.upper()}. "
        f"Tests: {exec_results.get('passed', 0)}/{total} passing.",
        risk_factors=risk_factors,
        action_items=action_items,
        historical_warnings=[],
        remediation_summary=remediation_summary,
        is_auto_remediated=is_auto_remediated,
        markdown_review=markdown_review,
    )


def _build_markdown_review(
    state: AgentState,
    risk_level: str,
    risk_score: float,
    risk_factors: list[dict[str, str]],
    action_items: list[str],
) -> str:
    """Build a formatted Markdown review body."""
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "💀"}.get(risk_level, "⚪")
    exec_results = state.get("execution_results", {})
    changed_files = state.get("changed_files", [])
    repair_patches = state.get("repair_patches", [])
    is_auto_remediated = state.get("is_auto_remediated", False)
    impact_details = state.get("impact_details", [])
    affected_tests = state.get("affected_tests", [])

    direct = [
        entry["file"]
        for entry in impact_details
        if entry.get("depth") == 1 and entry.get("kind") != "test"
    ]
    transitive = [
        entry["file"]
        for entry in impact_details
        if entry.get("depth", 0) > 1 and entry.get("kind") != "test"
    ]

    lines = [
        "# 🤖 TestPilot AI Analysis",
        "",
        f"## {risk_emoji} Risk Assessment: `{risk_level.upper()}` ({risk_score:.1f}/10.0)",
        "",
        "### Changed Files",
        *[f"- `{f['path']}` (+{f['additions']}/-{f['deletions']})" for f in changed_files[:15]],
        "",
        "### 💥 Blast Radius",
        f"- Direct dependents: **{len(direct)}**",
        *[f"  - `{path}`" for path in direct[:8]],
        f"- Transitive dependents: **{len(transitive)}**",
        *[f"  - `{path}`" for path in transitive[:8]],
        "",
        "### 🧪 Existing Tests Covering The Change",
        *([f"- `{path}`" for path in affected_tests[:10]] or ["- None detected"]),
        "",
        "### 🧭 Context Health & Test Priorities",
        f"- Mode: `{(state.get('context_health') or {}).get('mode', 'unknown')}` "
        f"(score {(state.get('context_health') or {}).get('score', 0)}/100)",
        *[
            f"- **{target['priority'].upper()}** `{target['file']}` — "
            f"{'; '.join(target.get('reasons', [])[:2])}"
            for target in state.get("test_targets", [])[:5]
        ],
        "",
        "### Risk Factors",
        *[f"- **{rf['severity'].upper()}**: {rf['factor']}" for rf in risk_factors],
        "",
        "### Test Results",
        f"- 🧾 Verify exit: `{(state.get('verification') or {}).get('exit_reason', 'unknown')}`",
        f"- 🎯 Scope: `{state.get('execution_scope', 'full')}`"
        f" ({state.get('selected_test_count', 0)} test files selected)",
        f"- ✅ Passed: {exec_results.get('passed', 'N/A')}",
        f"- ❌ Failed: {exec_results.get('failed', 'N/A')}",
        f"- ⏭️ Skipped: {exec_results.get('skipped', 'N/A')}",
        f"- 📊 Coverage: {exec_results.get('coverage_percentage', 'N/A')}%",
        "",
    ]

    # Adversarial Mutation Combat Report
    mutation_results = state.get("mutation_results") or []
    survived_mutants = state.get("survived_mutants") or []
    if mutation_results:
        killed = len(mutation_results) - len(survived_mutants)
        pct = (state.get("mutation_score") or 0) * 100
        verdict = (
            "🛡️ High Verification (All Mutants Killed)"
            if pct == 100
            else "⚠️ Weak Assertions (Mutants Survived)"
        )
        lines += [
            "### ⚔️ Adversarial Mutation Arena",
            f"- **Verdict**: `{verdict}`",
            f"- **Mutation Kill Rate**: **{pct:.1f}%** ({killed}/{len(mutation_results)} synthetic bugs killed)",
        ]
        if survived_mutants:
            lines.append("- **Survived Mutants (Weak Assertions)**:")
            for m in survived_mutants[:3]:
                lines.append(
                    f"  - `{m.get('target_file')}:{m.get('line_number')}`: {m.get('description')} "
                    f"(`{m.get('original_snippet')}` → `{m.get('mutated_snippet')}`)"
                )
        lines.append("")

    # Autonomous Remediation section
    if repair_patches:
        status_badge = (
            "✅ Sandbox Verified (All Tests Passed)"
            if is_auto_remediated
            else "⚠️ Auto-Fix Patches Proposed"
        )
        lines += [
            "### 🛠️ Autonomous Remediation (Auto-Fix)",
            f"- **Status**: `{status_badge}`",
            f"- **Total Patches**: {len(repair_patches)}",
            "",
        ]
        for idx, patch in enumerate(repair_patches[:3], 1):
            lines += [
                f"#### Patch #{idx}: `{patch.get('file_path')}` ({patch.get('target_type')})",
                f"> **Rationale**: {patch.get('explanation')}",
                "",
                "```diff",
                patch.get("patch_diff", patch.get("repaired_code", "")),
                "```",
                "",
            ]

        pr_num = state.get("pr_number", "PR")
        lines += [
            "**Recommended Action:**",
            "```bash",
            "# Pull or apply the automated fix branch",
            f"git checkout -b testpilot/fix-{pr_num}",
            "```",
            "",
        ]

    if action_items:
        lines += ["### 📋 Action Items", *[f"- [ ] {item}" for item in action_items], ""]

    lines += ["---", "*Generated by [TestPilot AI](https://github.com/testpilot-ai)*"]

    return "\n".join(lines)
