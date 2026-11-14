# TestPilot AI

[![CD Build Status](https://github.com/Shikhaar/TestPilot-AI/actions/workflows/cd.yml/badge.svg?branch=main)](https://github.com/Shikhaar/TestPilot-AI/actions/workflows/cd.yml)
[![CI Check Status](https://github.com/Shikhaar/TestPilot-AI/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Shikhaar/TestPilot-AI/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/Shikhaar/TestPilot-AI/pulls)

**TestPilot AI** is an autonomous regression testing platform that analyzes code changes, maps their dependency impact, retrieves relevant code context, generates tests, executes them in isolated environments, diagnoses failures, and creates pull requests — end to end, without manual intervention.

**One-command setup:** `docker compose up -d`

---

## Key Capabilities

- AST-based change and dependency analysis (Tree-sitter)
- 3-layer hybrid codebase RAG (dense vectors + keyword + disk scan)
- Stateful multi-agent test generation (11 LangGraph agents + a contracts-first verify engine)
- Sandboxed test execution (PyTest, Jest, JUnit, Go Test)
- Selective regression execution (only tests covering the changed modules)
- Automated failure diagnosis with root-cause reporting
- Multi-VCS support (GitHub, Bitbucket, GitLab, Azure DevOps)
- 4-category EvalOps telemetry (Pass@1/N, repair rate, token cost)
- GitHub PR automation with structured review comments

---

## Why TestPilot?

Traditional testing requires developers to manually identify affected areas, write tests for every change, and interpret failure output. This is slow, inconsistent, and scales poorly across large codebases.

TestPilot closes this loop by combining static code analysis, dependency graphs, semantic retrieval, and LLM agents into a single automated pipeline. The result is regression coverage that runs on every change, not just when someone has time to write tests.

---

## Dashboard

![TestPilot AI Dashboard](docs/images/dashboard.png)

> Automated regression testing, vector AST indexing, and risk impact analysis dashboard showing repository health metrics, live AI agent telemetry, and quality token analytics.

---

## How It Works

```
Developer opens PR or pushes a commit
           |
           v
  Analyze changed files with Tree-sitter AST
           |
           v
  Map upstream/downstream dependency graph
           |
           v
  Calculate regression blast radius
           |
           v
  Retrieve relevant code context via hybrid RAG
           |
           v
  Generate unit tests matching repo style (LLM)
           |
           v
  Execute tests in isolated sandbox environment
           |
           v
  Diagnose failures, generate root-cause report
           |
           v
  Post structured review comment + open PR
```

---

## Key Engineering Decisions

**Why Tree-sitter?**
Deterministic, language-agnostic AST parsing. Change analysis is based on actual syntax structure rather than LLM interpretation, which keeps the impact graph reliable and auditable.

**Why LangGraph?**
Stateful orchestration with conditional branching, retries, and failure recovery. Each agent node is independently testable and observable, making the pipeline debuggable rather than a black box.

**Why separate phases rather than one prompt?**
Each phase — planning, diff parsing, dependency traversal, retrieval, generation, verification — has distinct inputs, outputs, and failure modes. Separating them makes the system easier to observe, retry selectively, and extend.

**Why Qdrant?**
Dense vector retrieval over repository code with 384-dimensional embeddings. Combined with PostgreSQL keyword search and a disk scanner, this gives three independent retrieval strategies that complement each other.

**Why Celery + Redis?**
Test generation and sandboxed execution are expensive and long-running. Moving them out of the synchronous API path keeps the backend responsive and allows the frontend to stream real-time progress updates.

**Why sandboxed execution?**
Generated tests are untrusted code. Each run executes in its own scratch directory with run-scoped file names, a scrubbed environment (no LLM keys, database URLs or VCS tokens), POSIX resource limits, and optionally a network-isolated container (`SANDBOX_MODE=docker`). Isolation is a first-class part of the verify engine's runner, not a side effect of running a subprocess.

---

## System Architecture

```
              Next.js 16 Frontend
                      |
                 FastAPI Backend
                /     |      \
        LangGraph   Qdrant   Celery + Redis
         Agents      RAG     Async Workers
            |
       Test Sandbox
    (PyTest / Jest / JUnit)
```

Full component topology with database and telemetry layers is documented in [System Architecture](docs/architecture.md).

```mermaid
graph TD
    Client[Next.js 16 Frontend] -->|REST / WebSockets| FastAPI[FastAPI Backend]

    FastAPI -->|VCS Layer| VCS[GitHub · Bitbucket · GitLab · Azure DevOps]
    FastAPI -->|Async Tasks| Redis[Redis Broker]
    Redis -->|Dispatch| Celery[Celery Worker]

    FastAPI -->|Query/Write| PG[(PostgreSQL)]
    Celery -->|Query/Write| PG

    FastAPI -->|3-Layer Search| Qdrant[(Qdrant Vector DB)]
    Celery -->|Upsert Chunks| Qdrant

    FastAPI -->|Telemetry| EvalOps[EvalOps Collector]

    Celery -->|Execute| LangGraph[LangGraph Agent Engine]
    LangGraph -->|Sandbox| Sandbox[Test Runner]
```

---

## The 11 LangGraph Agents

The agents are separated by responsibility to keep planning, static analysis, retrieval, generation, review and documentation independently testable and observable.

| # | Agent | Responsibility |
| :--- | :--- | :--- |
| 1 | **Planner** | Validates pipeline requests and normalizes execution state |
| 2 | **Diff** | Parses Git diffs, maps changed line ranges to Tree-sitter AST nodes, and extracts symbol contracts |
| 3 | **Dependency** | Queries the static import graph to build upstream/downstream call graph |
| 4 | **Impact** | Traverses the graph to calculate layered regression blast radius |
| 5 | **Search** | Retrieves globally ranked code snippets from symbol-level vector chunks |
| 6 | **Test Discovery** | Indexes existing tests, resolves test-to-source coverage, and detects framework config |
| 7 | **Risk Ranker** | Fuses blast radius, coverage and test history into ranked test targets |
| 8 | **Context Controller** | Scores context sufficiency, retries retrieval impact-aware, and can decline generation |
| 9 | **Test Generator** | Synthesizes unit tests from contracts, ranked targets and repository style via LLM |
| 10 | **Review** | Aggregates risk metrics, context health, repair and mutation results into PR review comments |
| 11 | **Documentation** | Detects API documentation drift and generates spec updates |

The verify phase is **not** an agent graph. `app/verify/` is a contracts-first engine with a deterministic controller and four tools:

| Tool | Responsibility |
| :--- | :--- |
| `execute` | Runs the selected tests in the sandbox and parses JUnit XML |
| `repair_tests` | Repairs generated tests only (source patches are proposals, never applied) |
| `propose_source_patch` | Produces a source patch for review |
| `probe_mutations` | Injects AST mutants and measures whether the tests kill them |

---

## Engineering Metrics

| Metric | Value |
| :--- | :--- |
| LangGraph agent nodes | 11 |
| Code search layers | 3 (dense vector · keyword · disk scan) |
| EvalOps telemetry categories | 4 (Pass@1/N · repair rate · token cost · latency) |
| VCS providers supported | 5 (GitHub · Bitbucket · GitLab · Azure DevOps · PAT) |
| Embedding dimensions | 384-dim dense vectors (Sentence-Transformers) |
| Test frameworks supported | PyTest · Jest/Vitest · JUnit · Go Test |

---

## Empirical Benchmark: 100 Real GitHub Revert PRs

To validate TestPilot AI against real-world production failures, we evaluated the platform across **100 real historical PRs** that were merged and subsequently **reverted in production** across 6 core open-source repositories (`click`, `marshmallow`, `pluggy`, `requests`, `cachetools`, `iniconfig`).

### Benchmark Highlights (Zero-Test-Vacuum Scenario)
- **76.0% (76/100) Live Adversarial Defect Intercept**: Synthesized standalone zero-leakage pytest suites from scratch that reproduced the exact regression in physical sandboxes.
- **13.0% (13/100) AST Semantic Fast-Path**: Safely bypassed non-behavioral documentation and formatting commits in `< 1ms` with 0 Token cost.
- **9.0% (9/100) Static Contract Guards**: Intercepted `.pyi` / `@overload` type drift and falsy control flow traps before execution.
- **98.0% (98/100) Combined Safety Gate Coverage**: Only 2 residual escapes across 100 real incident PRs.
- **Execution Speed**: 3-concurrency physical Git Worktree isolation completed all 100 PRs in **581.08s (~9.6 mins)**.

Detailed evaluation reports and data:
- [100 PR Live LLM 0-to-1 Adversarial Benchmark](docs/benchmarks/eval_100_live_llm_zero_to_one.md)
- [100 PR Multi-Gate Multi-Repo Benchmark Report](docs/benchmarks/eval_100_real_github_reverts_optimized.md)
- [Zero-to-One Synthesis Case Studies (RPR-097 & RPR-071)](docs/benchmarks/eval_live_llm_zero_to_one_report.md)

---

## Tech Stack

**AI & Intelligence**
LangGraph · Tree-sitter · LiteLLM · Instructor · Sentence-Transformers · Qdrant RAG

**Backend**
Python 3.12 · FastAPI · Async SQLAlchemy 2 · Alembic · Pydantic v2 · Poetry

**Async Infrastructure**
Celery 5 · Redis 7 · Subprocess sandboxing

**Observability**
Prometheus · Grafana · OpenTelemetry · Celery Flower · EvalOps

**Frontend**
Next.js 16 · React 19 · TailwindCSS · CSS Modules

**Storage**
PostgreSQL 16 · Qdrant Vector DB

---

## Quickstart

```bash
git clone https://github.com/Shikhaar/TestPilot-AI.git
cd TestPilot-AI
cp .env.example .env
docker compose up -d
docker compose exec backend alembic upgrade head
```

Open `http://localhost:3000`.

See the [Developer Setup Guide](docs/setup.md) for local development, environment variables, and service endpoints.

---

## Engineering & System Documentation

| Document | Description | Key Concepts |
| :--- | :--- | :--- |
| [**System Architecture**](docs/architecture.md) | Decoupled platform topology & component interactions | Next.js 16, FastAPI, PostgreSQL, Qdrant, Celery |
| [**Project Overview**](docs/PROJECT_OVERVIEW.md) | Comprehensive platform overview & system capabilities | End-to-end PR review pipeline & AST impact mapping |
| [**Multi-VCS Provider Architecture**](docs/features/VCS_PROVIDERS.md) | Multi-VCS integration & authentication model | GitHub, Bitbucket, GitLab, Azure DevOps, PAT tokens |
| [**EvalOps Telemetry Engine**](docs/features/EVALOPS_AND_TELEMETRY.md) | Quality benchmarks, repair metrics & token analytics | Pass@1/Pass@N, repair success rate, token cost |
| [**3-Layer Code Search Engine**](docs/features/CODE_SEARCH_AND_INDEXING.md) | Parallel multi-layer code retrieval architecture | Dense vectors, PostgreSQL ILIKE, disk scanner |
| [**Test Generation & Verification**](docs/features/TEST_GENERATION_AND_VERIFICATION.md) | Multi-agent test synthesis & self-healing verification | Exit codes, pytest/jest JSON reports, self-healing |
| [**GitHub OAuth & Session Security**](docs/features/GITHUB_OAUTH_AND_SECURITY.md) | Enterprise authentication & security model | GitHub OAuth 2.0, JWT, zero password storage |
| [**Monitoring & Telemetry**](docs/features/MONITORING_AND_TELEMETRY.md) | Queue monitoring & graceful fallback UI | Celery latency, Prometheus metrics, fallback banners |
| [**Developer Setup Guide**](docs/setup.md) | Containerized and local development guide | Docker Compose, env vars, alembic migrations |
| [**Implementation Roadmap**](docs/plans/01_INITIAL_IMPLEMENTATION_PLAN.md) | Chronological development roadmap & milestones | Phase-by-phase implementation logs |
| [**Release v1.1.0 Record**](docs/plans/05_RELEASE_V1_1_MULTIVCS_AND_PARALLEL_LANGGRAPH.md) | Release v1.1.0 features & test results | Multi-VCS, secured webhooks, parallel LangGraph |
| [**Version 2.0.0 Roadmap**](docs/plans/06_VERSION2_ROADMAP.md) | v2.0 enterprise roadmap & milestones | Docker sandbox, auto-remediation, RBAC |
| [**Context Agents Optimization Report**](docs/plans/07_CONTEXT_AGENTS_OPTIMIZATION_REPORT.md) | Audit & remediation of the impact/search/discovery context pipeline | Retrieval correctness, layered blast radius, coverage mapping, selective execution |

---

## License

Distributed under the **MIT License**. See `LICENSE` for details.
