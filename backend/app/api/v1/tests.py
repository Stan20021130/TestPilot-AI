from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.dependency_graph import DependencyEdge
from app.models.generated_test import GeneratedTest
from app.models.repository import Repository
from app.models.test_run import TestRun
from app.schemas.common import APIResponse, PaginatedResponse, TaskResponse
from app.schemas.test import (
    DiscoveredTest,
    GeneratedTestResponse,
    TestDiscoverRequest,
    TestDiscoverResponse,
    TestGenerateRequest,
    TestRunRequest,
    TestRunResponse,
)
from app.services.test_discovery import TestDiscovery

logger = get_logger(__name__)
router = APIRouter()


@router.post("/discover", response_model=APIResponse[TestDiscoverResponse])
async def discover_tests(
    request: TestDiscoverRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> APIResponse[TestDiscoverResponse]:
    """Discover existing tests in a repository."""
    settings = get_settings()

    repo_result = await db.execute(
        select(Repository).where(
            Repository.id == request.repository_id,
            Repository.owner_id == current_user.id,
        )
    )
    repo = repo_result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    repo_path = settings.repo_storage_path / request.repository_id

    # Coverage gaps require the persisted dependency graph: a source file is
    # only "covered" when an existing test imports it (or matches its name).
    edges = (
        (
            await db.execute(
                select(DependencyEdge).where(DependencyEdge.repository_id == request.repository_id)
            )
        )
        .scalars()
        .all()
    )
    edge_records = [
        {
            "source_node": edge.source_node,
            "target_node": edge.target_node,
            "edge_type": edge.edge_type,
        }
        for edge in edges
    ]

    report = TestDiscovery(Path(repo_path)).discover_with_coverage(edge_records=edge_records)

    return APIResponse(
        data=TestDiscoverResponse(
            total_test_files=report["total_test_files"],
            total_tests=report["total_tests"],
            frameworks_detected=report["frameworks_detected"],
            test_files=[DiscoveredTest(**item) for item in report["test_files"]],
            uncovered_modules=report["uncovered_modules"],
            coverage_gaps=report["coverage_gaps"],
        )
    )


@router.post("/generate", response_model=TaskResponse)
async def generate_tests(
    request: TestGenerateRequest,
    current_user: CurrentUser,
) -> TaskResponse:
    """Generate missing tests for a PR via AI."""
    # Test generation is part of the full PR analysis pipeline
    # This endpoint allows triggering generation independently
    return TaskResponse(
        task_id="",
        status="use_pr_analyze",
        message="Test generation runs as part of PR analysis. Use POST /pr/analyze to trigger.",
    )


@router.post("/run", response_model=TaskResponse)
async def run_tests(
    request: TestRunRequest,
    current_user: CurrentUser,
) -> TaskResponse:
    """Execute the test suite for a repository."""
    # For now, this triggers the execution agent directly via Celery
    return TaskResponse(
        task_id="",
        status="queued",
        message="Test execution is part of the PR analysis pipeline.",
    )


@router.get("/results/{run_id}", response_model=APIResponse[TestRunResponse])
async def get_test_results(
    run_id: str,
    db: DBSession,
    current_user: CurrentUser,
) -> APIResponse[TestRunResponse]:
    """Get test run results by ID."""
    result = await db.execute(select(TestRun).where(TestRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Test run not found")

    return APIResponse(data=TestRunResponse.model_validate(run))


@router.get("/generated/{pr_id}", response_model=PaginatedResponse[GeneratedTestResponse])
async def get_generated_tests(
    pr_id: str,
    db: DBSession,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedResponse[GeneratedTestResponse]:
    """Get AI-generated tests for a pull request."""
    offset = (page - 1) * page_size
    total = (
        await db.execute(
            select(func.count())
            .select_from(GeneratedTest)
            .where(GeneratedTest.pull_request_id == pr_id)
        )
    ).scalar_one()

    tests = (
        (
            await db.execute(
                select(GeneratedTest)
                .where(GeneratedTest.pull_request_id == pr_id)
                .offset(offset)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return PaginatedResponse.create(
        [GeneratedTestResponse.model_validate(t) for t in tests],
        total,
        page,
        page_size,
    )
