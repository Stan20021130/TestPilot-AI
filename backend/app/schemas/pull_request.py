"""TestPilot AI — Pull Request Pydantic Schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.schemas.common import BaseSchema


class RiskLevel(StrEnum):
    """PR risk classification levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AnalysisStatus(StrEnum):
    """PR analysis pipeline status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PRAnalyzeRequest(BaseSchema):
    """Request to manually trigger PR analysis."""

    pr_number: int = Field(..., description="GitHub PR number")
    repository_id: str = Field(..., description="Internal repository ID")
    force: bool = Field(default=False, description="Force re-analysis")


class ChangedFile(BaseSchema):
    """Represents a single changed file in a PR diff."""

    path: str
    status: str  # 'added' | 'modified' | 'deleted' | 'renamed'
    additions: int
    deletions: int
    old_path: str | None = None  # For renamed files
    # New-file line numbers added/modified in this file. Empty when the VCS
    # provider did not return a patch (binary or truncated diff).
    changed_lines: list[int] = []


class DiffSummary(BaseSchema):
    """Summary of a PR's code diff."""

    changed_files: list[ChangedFile]
    total_additions: int
    total_deletions: int
    affected_languages: list[str]
    changed_functions: list[str]
    changed_classes: list[str]
    changed_routes: list[str]


class ImpactAnalysisResult(BaseSchema):
    """Result of the impact analysis for a PR."""

    affected_modules: list[str]
    affected_services: list[str]
    affected_apis: list[str]
    affected_tests: list[str]
    downstream_dependencies: list[str]
    impact_radius: int = Field(description="Number of nodes affected in dependency graph")
    impact_level: str  # 'isolated' | 'moderate' | 'widespread'


class RiskFactor(BaseSchema):
    """A single risk factor identified in a PR."""

    factor: str
    severity: str
    description: str
    affected_path: str | None = None


class PRReviewResponse(BaseSchema):
    """The complete AI-generated PR review."""

    risk_level: RiskLevel
    risk_score: float
    summary: str
    risk_factors: list[RiskFactor]
    action_items: list[str]
    historical_context: str | None = Field(
        default=None,
        description="Insights from historical bug patterns",
    )
    full_review_body: str


class PullRequestResponse(BaseSchema):
    """Response schema for a pull request."""

    id: str
    repository_id: str
    pr_number: int
    title: str
    state: str
    author: str
    base_branch: str
    head_branch: str
    analysis_status: str
    risk_level: str | None
    risk_score: float | None
    coverage_delta: float | None
    files_changed: int
    lines_added: int
    lines_removed: int
    created_at: datetime


class DecisionResponse(BaseSchema):
    """One orchestration decision recorded during the analysis run."""

    agent: str
    choice: str
    reason: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    attempt: int | None = None
    at: str | None = None


class PullRequestDetailResponse(PullRequestResponse):
    """Detailed PR response with analysis results."""

    affected_modules: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    # Non-fatal degradations from the analysis pipeline (degraded context).
    analysis_warnings: list[str] = Field(default_factory=list)
    # Why the pipeline did what it did (declines, loop exits, fallbacks).
    analysis_decisions: list[DecisionResponse] = Field(default_factory=list)
    # Denormalised outcomes for dashboards/EvalOps.
    verify_exit_reason: str | None = None
    verify_engine: str | None = None
    verify_rounds: int | None = None
    context_mode: str | None = None
    generation_plan_summary: dict[str, Any] = Field(default_factory=dict)
    review_summary: str | None = None
