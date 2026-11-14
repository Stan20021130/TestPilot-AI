# TestPilot AI — Project Overview & Architecture

TestPilot AI is an advanced, production-grade **AI-Powered Regression Testing and Automated Code Review Platform**. It connects directly to your GitHub repository and automatically runs static analysis, AST-based impact mapping, sandboxed test execution, and generative test creation on every incoming Pull Request (PR).

---

## Key Capabilities

1. **Multi-VCS Provider Support**: Connect, index, and manage repositories across **GitHub**, **Bitbucket**, **GitLab**, **Azure DevOps**, and **Custom Git URLs** with 1-Click OAuth 2.0 and PAT authentication.
2. **Automated PR Reviews**: Generates senior-engineer-level, structured markdown feedback directly in GitHub/VCS PR comments.
3. **Impact & Dependency Mapping**: Uses Tree-sitter AST parsing to trace exactly which functions, classes, and REST endpoints are impacted by code changes.
4. **Sandbox Test Execution**: Automatically spins up isolated environments to run modified or newly-added tests.
5. **Generative Test Writing**: Uses Gemini 2.0 Flash (via LiteLLM & Instructor) to automatically author missing test suites for untested changed code lines.
6. **AI Failure Analysis**: If tests fail, an AI analysis agent inspects traceback outputs to pinpoint the precise root cause.

---

## Technology Stack & Core Infrastructure

```mermaid
graph TD
 GitHub[GitHub Webhook / PR] -->|Payload| Proxy[Nginx Reverse Proxy]
 Proxy -->|API Request| FastAPI[FastAPI Backend]
 FastAPI -->|Write metadata| DB[(PostgreSQL Database)]
 FastAPI -->|Trigger task| Redis[(Redis Broker)]
 Redis -->|Execute worker| Celery[Celery Task Workers]
 Celery -->|AST Parsing| TreeSitter[Tree-sitter Parser]
 Celery -->|Vector Embeddings| Qdrant[(Qdrant Vector DB)]
 Celery -->|LLM Prompts| LiteLLM[LiteLLM + Gemini 2.0]
 Celery -->|Sandboxed Run| TestRunner[Python Pytest Runner]
 FastAPI -->|Expose metrics| OTEL[OpenTelemetry Collector]
 OTEL -->|Metrics| Prometheus[(Prometheus)]
 Prometheus -->|Dashboard| Grafana[(Grafana)]
```

### 1. Backend Service (`backend/`)
* **FastAPI**: Asynchronous web API powering the user dashboard and webhook handlers.
* **SQLAlchemy & Asyncpg**: Async database engine for metadata tracking (repositories, PRs, test runs).
* **Alembic**: Database schema migration manager.

### 2. Async Workers (`backend/app/workers/`)
* **Celery & Redis**: Task queue and broker handling long-running background tasks (cloning repositories, building indexes, executing test suites).

### 3. Vector database (`Qdrant`)
* **Qdrant**: Stores semantic vector embeddings of code chunks (classes, functions, test definitions) using local `SentenceTransformer` models, enabling contextual codebase retrieval for LLMs.

### 4. Language Models & AI Agent Orchestration (`backend/app/agents/`)
* **LangGraph**: Framework for structuring agentic state machines.
* **LiteLLM**: Standardized LLM interface configured to route queries to **Google Gemini 2.0 Flash**.
* **Instructor**: Enforces type-safe Pydantic structured schemas on LLM outputs (e.g. PR reviews, test suite models).
* **Tree-sitter**: Multi-language AST parser (Python, Javascript, Typescript, Rust, Go, Java) used to parse codebases and map relationships.

### 5. Frontend Dashboard (`frontend/`)
* **Next.js**: Premium UI offering real-time PR health monitors, coverage deltas, failure diagnostics, and test suite logs.

---

## Project Directory Structure

```
TestPilot AI/
├── .github/workflows/ # GitHub Actions CI/CD pipelines
│ └── ci.yml # Runs Ruff, Mypy, and Pytest on commits
│ └── cd.yml # Builds and pushes Docker images to GHCR
├── backend/ # Core Backend codebase
│ ├── app/
│ │ ├── agents/ # LangGraph agent state machines & logic
│ │ ├── api/ # FastAPI routers (webhooks, auth, AI chat, dashboard)
│ │ ├── core/ # Configuration, logging, telemetry settings
│ │ ├── database/ # DB session creation and SQLAlchemy base
│ │ ├── middleware/ # Custom CORS, Request ID, and Rate Limiters
│ │ ├── models/ # SQLAlchemy ORM definitions (PR, TestRun, User)
│ │ ├── repositories/ # Data Access Object pattern wrappers
│ │ ├── schemas/ # Pydantic schemas for request/response bodies
│ │ ├── services/ # Services (AST parsing, Embedding, GitHub API)
│ │ ├── tasks/ # Celery background tasks (indexing, pipelines)
│ │ ├── utils/ # Helper utilities (Git operations, shell run)
│ │ └── workers/ # Celery initialization configuration
│ ├── alembic/ # DB migrations configuration & versions
│ ├── tests/ # Unit & integration pytest suites
│ ├── pyproject.toml # Poetry package dependencies & tools setup
│ └── Dockerfile # Docker runtime build instruction
├── frontend/ # Next.js Frontend Dashboard code
│ ├── src/
│ │ ├── app/ # Next.js App router pages (dashboard, repos, PRs)
│ │ ├── components/ # Shared dashboard UI widgets
│ │ └── lib/ # API clients and helpers
│ └── package.json # NPM dependencies configuration
├── infra/ # Orchestration infrastructure scripts
│ ├── nginx/ # Nginx proxy routing configuration & TLS config
│ ├── prometheus/ # Metrics scrape configurations
│ └── grafana/ # Grafana data sources & dashboards configurations
├── docker-compose.yml # Local multi-service launcher configuration
└── .env # Configuration environment variables
```

---

## The AI Agent Pipeline (LangGraph Workflow)

When a GitHub PR webhook is received, Celery initiates the **PR Analysis Graph**. It has 11 agents across a 6-node main graph (planner → context → produce → verify → review → documentation): the context phase collects and fuses evidence into a `GenerationPlan`; `produce` generates tests; the verify engine executes, diagnoses, repairs and probes them.

```mermaid
stateDiagram-v2
 [*] --> PlannerAgent
 PlannerAgent --> DiffAgent
 DiffAgent --> DependencyAgent : 有变更符号
 DiffAgent --> ReviewAgent : 无变更符号
 DependencyAgent --> ImpactAgent
 DependencyAgent --> SearchAgent
 DependencyAgent --> TestDiscoveryAgent
 ImpactAgent --> RankerAgent
 SearchAgent --> RankerAgent
 TestDiscoveryAgent --> RankerAgent
 RankerAgent --> ControllerAgent
 ControllerAgent --> GeneratorAgent : generation_plan
 GeneratorAgent --> VerifyEngine : 有测试
 GeneratorAgent --> ReviewAgent : 无测试
 VerifyEngine --> ReviewAgent : verification
 ReviewAgent --> DocumentationAgent
 DocumentationAgent --> [*]
```

1. **`PlannerAgent`**: Validates the run contract (PR, head SHA, workspace) before anything expensive starts.
2. **`DiffAgent`**: Parses the PR patch, maps changed lines to Tree-sitter symbols, and extracts symbol contracts.
3. **`DependencyAgent`**: Loads the import graph so impact can be traversed instead of guessed.
4. **`ImpactAgent`**: Reverse-BFS over the graph, producing layered blast radius (direct / transitive), affected APIs and covering tests.
5. **`SearchAgent`**: Embeds each changed symbol and retrieves ranked, de-duplicated code from Qdrant.
6. **`TestDiscoveryAgent`**: Maps changed files to existing tests and detects the repository's test framework and command.
7. **`RiskRankerAgent`**: Fuses impact, coverage, history and contracts into ranked test targets with reasons.
8. **`ContextControllerAgent`**: Scores context sufficiency (full / degraded / insufficient) and runs an impact-aware second retrieval pass.
9. **`TestGeneratorAgent`**: Writes tests against real contracts and source, with bounded feedback and coverage top-ups.
10. **`ReviewAgent`**: Aggregates risk, context health and verify evidence into the PR comment.
11. **`DocumentationAgent`**: Flags API documentation drift for changed routes.

The verify phase is a contracts-first engine (`app/verify/`), not an agent graph. Its tools are `execute`, `repair_tests`, `propose_source_patch` and `probe_mutations`; `finish` is control flow. The controller owns the round/repair/mutation budgets, the ledger and the final verdict.

---

## ️ Database Schema & Entities

The platform maintains metadata persistently in a PostgreSQL schema:

* **Users**: Stores administrative user accounts.
* **Repositories**: Tracks registered GitHub repositories, full names, default branches, and index statuses.
* **PullRequests**: Tracks active PR numbers, head SHAs, branch names, risk levels, and risk scores.
* **TestRuns**: Records execution statistics (passed/failed/skipped counts, coverage percentages, timestamps, raw log outputs).

---

## Local Setup & Run Checklist

### Prerequisites
* Docker Desktop installed and running.
* GitHub Developer App credentials configured (if syncing real webhook triggers).
* Gemini API Key (configured inside `.env`).

### Run Services Locally
1. Clone the repository.
2. Configure `.env` using `.env.example` as a baseline.
3. Start the entire platform via Docker Compose:
 ```bash
 docker-compose up -d
 ```
4. Verify the backend service health:
 ```bash
 curl http://localhost:8000/health
 ```
5. Open the frontend dashboard in your browser at `http://localhost:3000`.
