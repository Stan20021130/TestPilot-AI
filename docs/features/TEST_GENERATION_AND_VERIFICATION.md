# Test Generation & Verification Architecture

TestPilot AI does not rely solely on static analysis or unverified LLM output. Instead, it executes generated tests inside an isolated sandbox (for supported frameworks such as `pytest`, `jest`, `go test`, and `mvn test`) and analyzes actual runtime execution results.

---

## 1. Detecting Pass vs. Fail (`execution_agent`)

During execution, TestPilot AI collects multiple execution signals directly from the operating system and test runners:

### Subprocess Exit Codes
- **Exit code `0`**: Test suite completed successfully.
- **Non-zero exit code**: One or more tests failed or the execution process crashed.

### Structured Test Reports
When supported by the test framework, TestPilot AI executes tests with machine-readable reporting enabled:
- **Pytest**: `python -m pytest --json-report --json-report-file=/tmp/testpilot_report.json`
- **Jest**: `npx jest --json --outputFile=/tmp/testpilot_jest.json`

These reports are parsed to extract:
- Total number of passed, failed, skipped, and errored tests.
- Individual test function names and execution durations.
- Failure messages and metadata.

### Coverage Metrics (Optional)
When coverage collection is enabled (`pytest-cov`, `jest --coverage`), TestPilot AI records line and branch coverage metrics to evaluate how thoroughly the generated tests exercise the codebase.

### Selective Execution (Regression Subset)
`execution_agent` does not always run the whole suite. The Impact and Test Discovery agents resolve which existing test files cover the changed modules (`affected_tests` / `test_coverage_map`), and only those files plus the newly generated tests are executed:

- **pytest / jest**: the resolved test paths are passed explicitly to the runner.
- **go test**: each test file is mapped to its package (`./pkg/...`).
- **maven**: unsupported for path-level selection, so the full suite runs.

Selection is deliberately conservative. TestPilot AI falls back to a full run when the feature flag `TEST_SELECTION_ENABLED` is false, when the runner cannot accept paths, when the resolved files are missing on disk, or when no covering test resolves at all. A subset that silently misses the regression is worse than a slow run. The chosen scope is recorded in `execution_scope` (`selected` / `full`) and surfaced in the PR review.

Execution is triggered when new tests were generated **or** when existing tests cover the change — a selective regression run is useful even if the generator produced nothing.

### Symbol Contracts and the Context Gate
Generation is driven by two artifacts rather than raw snippets alone:

- **Symbol contracts** (`app/services/contract_extractor.py`): signature, parameters, return type, decorators and documented intent of each changed symbol, extracted deterministically from the AST.
- **Ranked test targets** (`app/services/risk_ranker.py`): changed files scored by coverage gap, direct dependents, architectural layer, public-contract risk and recent test history (failures, flakiness, slow tests).

Before the LLM is called, `context_controller_agent` evaluates four evidence checks — contracts, real source snippets, impact, coverage baseline — and assigns `context_health.mode`:

| Mode | Meaning | Behaviour |
| :--- | :--- | :--- |
| `full` | all four checks pass | normal generation |
| `degraded` | one or more missing | generation proceeds, warning recorded |
| `insufficient` | no contracts *and* no source | **generation skipped**, reason surfaced in the review |

When source snippets are thin, the controller runs a second, impact-aware retrieval pass (querying how the affected modules are used) before giving up — something the parallel search branch cannot do, because Impact has not finished at that point.

### Runtime Logs and Stack Traces
For failing tests, TestPilot AI captures:
- Failed test name and file location.
- Exception type (e.g., `AssertionError`, `ModuleNotFoundError`, `TypeError`).
- Complete stack trace.
- Standard output (`stdout`) and error streams (`stderr`).

---

## 2. Auto-Healing Loop (`failure_analysis_agent`)

When a test fails, `execution_agent` forwards a structured failure report to `failure_analysis_agent`:

```json
{
 "test_name": "test_auth_header",
 "error": "ModuleNotFoundError: No module named 'app.utils.crypto'",
 "stack_trace": "File 'tests/test_auth.py', line 12...",
 "source_code": "from app.utils.crypto import hash_token"
}
```

### The Self-Healing Workflow:
1. **Root Cause Analysis**: Identifies the likely root cause from the stack trace and source code (e.g., outdated import path, missing mock, incorrect fixture).
2. **Failure Triage**: every analysis is classified as `test_defect`, `product_defect` or `environment`. This decides *which* recovery path is allowed:

   | Origin | Recovery | Rationale |
   | :--- | :--- | :--- |
   | `test_defect` (strict majority, budget left) | **regenerate** the suite with the failure feedback | the test was wrong, so the test should change |
   | `product_defect` | **repair** (patch) or report as-is | never regenerate away a real regression |
   | `environment` | repair / report | not a code problem |

3. **Bounded Regeneration**: a regeneration pass re-prompts the generator with the failing tests' root causes and suggested fixes. It is capped by `MAX_GENERATION_ATTEMPTS` (default 2 passes) and only happens when test defects dominate — a single product defect blocks it.
4. **Coverage Top-Up**: after the first pass, `critical`/`high` targets that received **no** generated test trigger one focused generation pass (`MAX_COVERAGE_TOPUPS`, default 1) restricted to those files; results are merged and de-duplicated.
5. **Patch Repair**: mechanical problems go to `repair_agent`, which patches failing tests in place (imports/setup are preserved when only a body is returned) or proposes a source patch for review, then re-executes in the sandbox (bounded by `max_repairs`, default 2).
6. **Adversarial Mutation Arena**: once the suite is green, `mutation_agent` injects AST mutants (operator swaps, boundary shifts, return tampers) into the changed code and re-runs the suite against them. Survived mutants prove the assertions are too weak, so the generator gets one fortification pass (`MAX_MUTATION_ATTEMPTS`, default 1; `0` disables the arena).
7. **Stop, don't hide**: when the budget is exhausted the failing tests survive into the review with their diagnostics — a red result is information, not something to optimise away.

---

## 3. Graceful Fallback Mechanism & Retry Limits

If a generated test cannot be automatically repaired after the bounded attempts (e.g., due to a real pre-existing bug in the PR source code, missing external service credentials, or complex multi-layered mock requirements), TestPilot AI handles the failure gracefully:

1. **Warning Label Flag**: The test is tagged with a `Generation Warning / Unverified` status in the PR review comment and UI dashboard.
2. **Failure Analysis Diagnostic Report**: The system outputs a structured Failure Analysis Report detailing:
   - Root Cause analysis
   - Affected code path
   - PR Regression Classification (whether the failure was introduced by the PR changes)
   - Confidence score (0.0 - 1.0)
3. **Actionable Fix Suggestions**: The unverified test code is displayed alongside a suggested fix recommendation, empowering developers to review the edge case and decide whether to update their source code logic or adjust the test fixture manually.
