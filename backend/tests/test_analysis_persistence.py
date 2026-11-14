"""Persistence and maintenance tests for analysis warnings and re-indexing.

Warnings are the pipeline's only signal that context collection degraded, so
they must survive into the database and reach the EvalOps dashboard.
"""

from __future__ import annotations

import json
import uuid

from app.models.agent_run import AgentRun
from app.models.pull_request import PullRequest
from app.services.evalops_collector import EvalOpsCollector, count_analysis_warnings


class _SharedSession:
    """Context manager yielding an existing session (no commit/close)."""

    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc_info) -> bool:
        return False


def test_count_analysis_warnings_handles_bad_payloads() -> None:
    assert count_analysis_warnings(None) == 0
    assert count_analysis_warnings("") == 0
    assert count_analysis_warnings("not-json") == 0
    assert count_analysis_warnings('{"a": 1}') == 0
    assert count_analysis_warnings('["w1", "w2"]') == 2


async def test_store_analysis_results_persists_warnings(
    db_session, test_pull_request, monkeypatch
) -> None:
    from app.tasks import pr_pipeline

    monkeypatch.setattr(pr_pipeline, "get_session", lambda: _SharedSession(db_session))

    # Detach the fixture object: the pipeline runs against its own session in
    # production, and a populated identity map would force a synchronous
    # refresh during UPDATE ... synchronize_session.
    pr_id = test_pull_request.id
    db_session.expunge_all()

    state = {
        "warnings": ["search_agent: 0 code snippets retrieved", "impact_agent: graph empty"],
        "risk_score": {"level": "medium", "score": 4.2, "factors": ["f"]},
        "affected_modules": ["app/services/billing.py"],
        "decisions": [
            {
                "agent": "context_controller_agent",
                "choice": "degraded",
                "reason": "context score 50/100",
                "inputs": {"score": 50},
            }
        ],
        "verification": {
            "exit_reason": "green",
            "engine": "next",
            "rounds": 2,
            "passed": 3,
            "failed": 0,
        },
        "context_health": {"mode": "degraded"},
        "generation_plan": {
            "declined": False,
            "context_score": 50,
            "targets": [{"file": "app/services/billing.py"}],
            "changed_symbols": 2,
        },
    }

    await pr_pipeline._store_analysis_results(pr_id, state)

    db_session.expire_all()
    stored = await db_session.get(PullRequest, pr_id)

    assert stored.analysis_status == "completed"
    assert json.loads(stored.analysis_warnings) == state["warnings"]
    assert json.loads(stored.analysis_decisions) == state["decisions"]
    assert json.loads(stored.affected_modules) == ["app/services/billing.py"]
    assert stored.verify_exit_reason == "green"
    assert stored.verify_engine == "next"
    assert stored.verify_rounds == 2
    assert stored.context_mode == "degraded"
    plan_summary = json.loads(stored.generation_plan_summary)
    assert plan_summary["declined"] is False
    assert plan_summary["targets"] == 1
    assert plan_summary["changed_symbols"] == 2


async def test_store_analysis_results_persists_empty_warning_list(
    db_session, test_pull_request, monkeypatch
) -> None:
    from app.tasks import pr_pipeline

    monkeypatch.setattr(pr_pipeline, "get_session", lambda: _SharedSession(db_session))

    pr_id = test_pull_request.id
    db_session.expunge_all()

    await pr_pipeline._store_analysis_results(
        pr_id, {"warnings": [], "risk_score": {}, "affected_modules": []}
    )

    db_session.expire_all()
    stored = await db_session.get(PullRequest, pr_id)
    assert json.loads(stored.analysis_warnings) == []
    assert json.loads(stored.analysis_decisions) == []


async def test_evalops_reports_context_warning_rate(
    db_session, test_pull_request, test_repository, test_user
) -> None:
    warned = test_pull_request
    warned.analysis_warnings = json.dumps(["search degraded"])
    warned.verify_exit_reason = "green"
    warned.context_mode = "full"

    clean = PullRequest(
        id=str(uuid.uuid4()),
        repository_id=test_repository.id,
        pr_number=43,
        github_pr_id="99997",
        title="Test PR: docs only",
        state="open",
        author="test-dev",
        base_branch="main",
        head_branch="docs/typo",
        head_sha="aaa111",
        base_sha="bbb222",
        analysis_status="completed",
        analysis_warnings=json.dumps([]),
        verify_exit_reason="budget_exhausted",
        context_mode="insufficient",
    )
    db_session.add(clean)
    db_session.add(
        AgentRun(
            pull_request_id=warned.id,
            agent_name="search_agent",
            status="completed",
            total_tokens=1200,
            duration_ms=800,
        )
    )
    await db_session.flush()

    metrics = await EvalOpsCollector().get_latest_metrics(db_session)

    assert metrics.is_sample_data is False
    assert metrics.context_warning_rate == 50.0
    assert metrics.verify_green_rate == 50.0
    assert metrics.verify_budget_exhaustion_rate == 50.0
    assert metrics.context_declined_rate == 50.0


async def test_reindex_all_dispatches_indexed_repositories(
    db_session, test_repository, monkeypatch
) -> None:
    from app.tasks import indexing

    monkeypatch.setattr(indexing, "get_session", lambda: _SharedSession(db_session))
    dispatched: list[dict] = []
    monkeypatch.setattr(
        indexing.index_repository, "delay", lambda **kwargs: dispatched.append(kwargs)
    )

    result = await indexing._reindex_all(access_token="tok", force=True, dispatch=True)

    assert result["dispatched"] >= 1
    assert any(item["repository_id"] == test_repository.id for item in result["repositories"])
    assert dispatched[0]["repository_id"] == test_repository.id
    assert dispatched[0]["force_reindex"] is True
    assert dispatched[0]["access_token"] == "tok"
