# 前置信息链路优化报告

| 项目 | 内容 |
| :--- | :--- |
| 对象 | Impact / Search / Test Discovery 三个并行上下文采集 Agent |
| 日期 | 2026-11-11 |
| 范围 | 上游 diff 粒度、向量索引、三个 Agent 交付物、生成器与评审的消费侧、执行选择、可观测性、融合层与充分性门控 |
| 状态 | 第 1–8 节（链路修复）、第 9 节（v2 范式）、第 10 节（v3 运行时）、第 11 节（v4 分析对象）、第 12 节（v5 生成回路）、第 13 节（v6 编排）、第 14 节（v7 审计与续跑）、第 15 节（v8 验证子图）、第 16 节（v9 上下文子图）、第 17 节（v10 收尾）、第 18 节（v11 受限自主）、第 19 节（v12 新引擎骨架）均已实现并通过单元测试；存量环境需执行一次数据库迁移 + 一次索引重建 |

---

## 1. 摘要

三个 Agent 的**分工是对的**（确定性依赖图 / 语义检索 / 测试资产，三维正交，并行扇出，状态键所有权隔离），但**交付物在信息密度上远低于三个独立 Agent 应有的水平**，并且其中一条主链路实际处于静默失效状态。

本次优化做了四件事：

1. **修复失效链路**：Search 从未生成查询向量，且过滤器以 dict 形式传给 qdrant-client（该版本对 `Filter` 开启 `extra=forbid`，直接校验失败），异常被 `except` 吞掉 → 线上检索结果恒为空；索引侧 `payload["content"]` 存的是 `function foo in app/x.py` 这类标签而非代码。
2. **提升输入精度**：diff 解析保留 hunk 行号，`changed_nodes` 从“改动文件里的全部符号”收紧为“命中改动行的符号”。
3. **重构交付物形态**：Impact 交付分层影响面（direct/transitive + 深度 + 结构分类 + 受影响测试 + 未知文件）；Discovery 交付 `test → source` 覆盖映射与框架配置；两者都显式区分“无影响”与“不知道”。
4. **打通消费闭环**：生成器真正使用检索上下文、现有测试风格、无覆盖清单与受影响模块；评审展示爆炸半径与覆盖本次变更的测试；执行侧按受影响测试做选择性回归。

验证结果：第一轮新增 **34 个单元测试**、全量 **53 passed**；第 9 节的 v2 演进另增 **16 个用例**，全量 **69 passed**。2 个失败为既有环境问题（未配置 webhook secret、本地无 Redis），与被改动模块无关。

---

## 2. 问题定义

### 2.1 P0 —— 链路失效

| # | 问题 | 证据 | 后果 |
| :--- | :--- | :--- | :--- |
| P0-1 | Search 把原始文本作为 `query_text` 传给 `qdrant.search()`，且**全程未调用 embedding 服务** | `app/agents/search_agent.py:50-75` | collection 是 384 维向量，没有向量可检索 |
| P0-2 | `query_filter` 以 dict 传入；qdrant-client 1.20 对 `models.Filter` 启用 `extra=forbid` | 本地复现：`ValidationError: query_filter Extra inputs are not permitted` | 请求在到达服务端前即失败 |
| P0-3 | P0-1/2 的异常被 `except Exception` 吞掉，只打一条 warning，节点仍标记 `completed` | `search_agent.py:72-76` | 生成器长期收到 `No additional context available.`，且监控无感 |
| P0-4 | 索引 payload 存标签而非源码 | `app/tasks/indexing.py`（优化前 325-385 行） | 即使检索成功，模型也拿不到可写断言的实现 |

### 2.2 P1 —— 信息密度不足

| # | 问题 | 证据 |
| :--- | :--- | :--- |
| P1-1 | `changed_files` 无 hunk 信息；`_identify_changed_nodes` 把改动文件里**所有**函数/类/路由都当作“变更” | `diff_agent.py`（优化前 148-185） |
| P1-2 | Impact 把 per-file 映射 `flatten` 成 `set`，深度与方向信息丢失 | `impact_agent.py`（优化前 53-55） |
| P1-3 | `affected_services` / `affected_apis` 靠整路径子串分桶 | `app/api/v1/sessions.py` 不会被识别为 API；`utils/rapid.py` 会被误判 |
| P1-4 | `from_edge_records` 只恢复边不恢复节点属性，且隐式建节点 → 无边的变更文件不在图中，`get_dependents` 返回空集 | 把“不知道”报成“没有影响” |
| P1-5 | Discovery 用文件名 stem 猜覆盖，`coverage_gaps` 是 `uncovered_modules[:20]` | 字段冗余；API 侧 `uncovered_modules` 更是硬编码 `[]` |
| P1-6 | 生成器接收 `uncovered_modules` 却从不使用；`affected_modules` 完全没进 prompt；`context_pruner` 无任何调用方 | 参数与死代码 |
| P1-7 | 生成的测试全部记在 `changed_nodes[0]["file_path"]` 名下 | 多文件 PR 归属错误 |
| P1-8 | 三个 Agent 无任何单测 | `backend/tests/` 仅有 repair / VCS / webhook |

---

## 3. 优化方案

### 3.1 上游：把“文件级”收紧到“符号级”

- 新增 [`app/utils/patch_parser.py`](../../backend/app/utils/patch_parser.py)：解析 `@@ -a,b +c,d @@`，输出新文件行号，处理 `\ No newline`、多 hunk、无 patch（二进制/截断）。
- `ChangedFile` 增加 `changed_lines`（[`schemas/pull_request.py`](../../backend/app/schemas/pull_request.py)、[`agents/state.py`](../../backend/app/agents/state.py)），由 GitHub 适配器填充。
- [`diff_agent.py`](../../backend/app/agents/diff_agent.py)：`_filter_nodes_by_changed_lines()` 只保留与改动行相交的符号；**显式回退**——无行信息或仅模块级改动时保留整文件符号，避免注释改动导致空管线。

### 3.2 索引：payload 存真实源码

[`tasks/indexing.py`](../../backend/app/tasks/indexing.py)：

- 每个 AST 结果只读一次文件，按 `line_start/line_end` 切片函数体/类体（上限 1500 字符）写入 `payload["content"]`，并附带行号字段。
- embedding 文本 = 标签 + 源码片段（截断 2000 字符），使向量语义覆盖实现而非仅名字。
- `file_chunk` 分支由 500 字符提升到 1500 字符。

### 3.3 Search：真实检索 + 排序 + 可观测

[`search_agent.py`](../../backend/app/agents/search_agent.py) 重写为：

1. 每个变更符号一条 query（上限 10 条），用 `embedding_service.generate_embedding()` 生成向量；
2. `query_points`（新版 API）/ `search`（旧版回退）双路径；
3. `models.Filter` + `models.FieldCondition` 构造仓库过滤；
4. 按 `(file_path, content[:160])` 去重，保留最高分；全局按分数降序；丢弃短于 40 字符的标签型内容；上限 12 条；
5. 输出 `search_metrics`（queries / failed_queries / raw_hits / unique_hits / kept / last_error / duration_ms）；
6. 降级写入 `warnings`——“全部查询失败”与“0 条结果”分别成条，不再静默。

### 3.4 Impact：分层、结构化、区分“未知”

[`impact_agent.py`](../../backend/app/agents/impact_agent.py)：

| 交付物 | 含义 |
| :--- | :--- |
| `impact_details` | `[{file, depth, kind}]`，按 (深度, 路径) 排序 |
| `affected_modules` | 扁平并集（保持向后兼容），**排除测试文件** |
| `affected_services` / `affected_apis` | 基于目录段与文件名后缀的结构化分类（[`path_classifier.py`](../../backend/app/utils/path_classifier.py)） |
| `affected_tests` | 覆盖变更及其直接依赖的现有测试文件 |
| `impact_unknown_files` | 不在依赖图中的变更文件——**未知不等于无影响** |
| `impact_radius` / `impact_level` | 数值与等级（isolated / moderate / widespread / unknown） |

配套修复：`DependencyGraphBuilder.get_dependents_with_depth()`（最短路距离）、`is_known()`、`from_edge_records` 显式建节点并恢复 `is_test`。

### 3.5 Discovery：覆盖映射 + 框架配置

[`test_discovery_agent.py`](../../backend/app/agents/test_discovery_agent.py)：

- `_map_tests_to_sources()` 三路信号：图边 `test → source`、命名约定（`test_foo` / `foo_test` / `foo.test`）、`covers_modules` 声明；
- 交付 `test_coverage_map`（变更文件 → 覆盖它的测试文件）；
- `_detect_test_config()`：框架、测试命令、配置文件、`conftest.py`、fixtures 目录（限定两级目录，避免与大范围 `rglob` 叠加）；
- `uncovered_modules` 仅针对**变更文件**计算“无任何覆盖测试”，`coverage_gaps` 作为其前 20 条兼容别名；
- 仓库未 clone 时写 warning，而不是返回空列表冒充“无缺口”。

[`services/test_discovery.py`](../../backend/app/services/test_discovery.py) 与 [`api/v1/tests.py`](../../backend/app/api/v1/tests.py)：`/tests/discover` 改为查询依赖边后调用 `discover_with_coverage()`，返回真实缺口（上限 200）与 `TestDiscovered` schema，删除动态建类写法。

### 3.6 Generator：把交付物真正喂进 prompt

[`test_generator_agent.py`](../../backend/app/agents/test_generator_agent.py) 的上下文块按预算拼装：

| 块 | 来源 | 预算 |
| :--- | :--- | :--- |
| 变更源码（按符号切片） | 仓库 checkout | 6000 字符 / 5 文件 / 每文件 6 符号 |
| 现有测试风格 | 2 个测试文件正文 | 每份 900 字符 |
| 检索上下文 | Search（排序 + 去重 + `prune_code_context`） | 6 条 / 4500 字符 |
| 仓库测试约定 | Discovery `test_config` | 框架、命令、配置、conftest、fixtures |
| 无覆盖文件 | Discovery `uncovered_modules` | 10 条 |
| 下游受影响模块 | Impact `affected_modules` | 10 条 |

同时修复测试归属（`_resolve_source_file()`：先按测试路径 stem、再按符号名匹配），并在缺少源码/检索为空时写入 warning。

### 3.7 Execution：选择性回归

[`execution_agent.py`](../../backend/app/agents/execution_agent.py)：

- `_select_test_paths()` = 覆盖本次变更的既有测试 ∪ 生成的测试；
- `_build_command()` 注入路径：pytest / jest 直接追加，go test 映射为 `./pkg/...`，maven 不支持则全量；
- **保守回退**：开关关闭、runner 不支持、文件不存在、一条覆盖测试都没解析到 → 全量执行；
- 上限 `max_selected_test_files`（默认 200）只裁剪既有测试，生成的测试永不截断；
- 结果写入 `execution_scope` / `selected_test_count`，进入评审。

[`graph.py`](../../backend/app/agents/graph.py)：`should_run_execution` 改为“有新测试 **或** 有覆盖测试”即执行——否则生成器无产出时，回归子集永远不会跑。

### 3.8 评审与可观测性

- [`review_agent.py`](../../backend/app/agents/review_agent.py)：新增 Blast Radius（直接/传递）、覆盖本次变更的现有测试、执行范围；新增两条风险因子（影响面未知、受影响模块无测试覆盖）。
- `AgentState.warnings` 贯穿全链路；`_store_analysis_results` 落库并输出 `impact_level` / `impact_radius`。
- 新增列 `pull_requests.analysis_warnings`（迁移 `8b1f4d2c7a90`），详情接口返回；EvalOps 增加 `context_warning_rate`（近 7 个 PR 中含告警的占比）。
- 新增 [`scripts/reindex_vectors.py`](../../backend/scripts/reindex_vectors.py) 与 Celery 任务 `reindex_all_repositories`，用于存量仓库按新 payload 格式重建。

---

## 4. 变更清单

### 新增（6）

| 文件 | 作用 |
| :--- | :--- |
| `backend/app/utils/patch_parser.py` | unified diff hunk 解析、行区间相交判定 |
| `backend/app/utils/path_classifier.py` | 结构化路径分类（api / service / model / test / module） |
| `backend/alembic/versions/8b1f4d2c7a90_add_analysis_warnings.py` | `analysis_warnings` 列，含 downgrade |
| `backend/scripts/reindex_vectors.py` | 存量仓库重建索引 CLI |
| `backend/tests/test_context_agents.py` | 29 个用例 |
| `backend/tests/test_analysis_persistence.py` | 5 个用例 |

### 修改（26）

| 层 | 文件 |
| :--- | :--- |
| 状态与协议 | `agents/state.py`、`schemas/pull_request.py`、`core/config.py` |
| 上游 | `services/github_service.py`、`agents/diff_agent.py`、`agents/graph.py` |
| 上下文 Agent | `agents/impact_agent.py`、`agents/search_agent.py`、`agents/test_discovery_agent.py` |
| 消费侧 | `agents/test_generator_agent.py`、`agents/execution_agent.py`、`agents/review_agent.py` |
| 服务/任务 | `services/dependency_graph_builder.py`、`services/impact_analyzer.py`、`services/test_discovery.py`、`services/evalops_collector.py`、`tasks/indexing.py`、`tasks/pr_pipeline.py` |
| API | `api/v1/ai.py`、`api/v1/tests.py`、`api/v1/pull_requests.py` |
| 模型/文档 | `models/pull_request.py`、`README.md`、`docs/setup.md`、`docs/features/EVALOPS_AND_TELEMETRY.md`、`docs/features/TEST_GENERATION_AND_VERIFICATION.md` |

---

## 5. 验证

### 5.1 单元测试（新增 34 个）

| 领域 | 覆盖点 |
| :--- | :--- |
| hunk 解析 | 新文件行号、`No newline`、空 patch、区间相交、逆序区间 |
| 符号过滤 | 命中保留、无行信息回退、模块级改动回退 |
| Impact | 直接/传递深度、测试文件不计入半径、`affected_tests`、未知文件告警、空图 → `unknown` |
| Search | 排序 + 去重（重复内容保留最高分）、失败上报、`models.Filter` 类型断言 |
| Discovery | 测试→源码映射、真实缺口、`test_config`（pytest / conftest）、仓库缺失告警 |
| Generator | prompt 含真实代码与缺口、上下文排序去重、测试归属、源码切片 |
| 执行 | 选择集、特征开关、文件缺失回退、maven 回退、上限不裁剪生成测试、pytest/go 命令拼装、路由 |
| 持久化 | warning JSON 容错、落库（含空列表）、EvalOps 50% 比率、重建派发 |

### 5.2 全量与迁移

```
53 passed, 2 failed        # tests/（2 个失败为既有环境问题）
alembic upgrade head       # 2c5589a68ebf -> 8b1f4d2c7a90
alembic downgrade -1       # 列删除，可回滚
ruff check                 # 新增文件全部通过
```

2 个失败原因：`test_github_adapter_parse` 依赖未配置的 `github_webhook_secret` 且用例不带签名头；`test_idempotency_service_lock` 依赖 localhost Redis。二者均不经过被改动模块。

### 5.3 索引 payload 的端到端断言

`test_indexing_stores_source_snippets_in_payload` 直接运行 `_generate_and_store_embeddings()`（fake Qdrant + 无模型模式），断言写入的 `payload["content"]` 含 `return amount * 2`，而不是标签字符串。

---

## 6. 兼容性与风险

| 项 | 说明 | 处置 |
| :--- | :--- | :--- |
| 数据库迁移 | 新增 `analysis_warnings` 列，非空模型字段 | 部署前执行 `alembic upgrade head` |
| 索引重建 | 存量 Qdrant 点位仍是标签 payload，不会原地迁移 | 执行 `python scripts/reindex_vectors.py` |
| 执行行为变化 | 有覆盖测试但无生成测试的 PR 现在也会跑回归子集 | 由 `TEST_SELECTION_ENABLED` / `max_selected_test_files` 控制，可关停 |
| 接口增量 | PR 详情新增 `analysis_warnings`（默认空列表） | 向后兼容 |
| 历史数据 | 旧 PR 该列为 `NULL`，按 0 条告警统计 | 不回溯填充 |
| 依赖新鲜度 | 选择性执行依赖依赖图；图过期时 `affected_tests` 为空 | 自动回退全量，不会漏跑 |
| 语言覆盖 | maven 不支持路径级筛选 | 回退全量执行 |

---

## 7. 收益评估

**已可直接验证的：**

- 检索结果从“几乎必然为空”变为可用，并具备排序、去重与失败可见性；
- 生成器 prompt 内含真实源码与真实测试风格（有单测断言），不再是符号名列表 + `No additional context available.`；
- 影响面从扁平路径列表变为可区分直接/传递、可追溯未知文件的结构化数据；
- 覆盖率缺口从“接口恒返回空”变为真实计算结果；
- 执行侧在可解析覆盖关系时只跑相关测试，且范围对评审可见。

**尚未量化、需要生产数据才能回答的：**

- 生成测试的 Pass@1 提升幅度；
- 选择性执行带来的平均耗时下降；
- `context_warning_rate` 的基线水平与告警分布。

这三项建议在接入真实仓库后，用 EvalOps 的 7-PR 时间序列观测，而不是在本次报告中估算。

---

## 8. 遗留与建议

1. **告警前端化**：`analysis_warnings` 目前只到 DB / EvalOps / 日志，尚未出现在 PR 评论正文与前端详情中。
2. **Search 的第三层**：Agent 路径仍只有稠密向量，README 所称的 3-layer（向量 / 关键词 / 磁盘）只实现在 `/ai/search` 接口。若要对齐，需要在 Agent 内复用关键词与磁盘扫描，并对三路结果做融合排序。
3. **选择粒度到测试函数**：当前是文件级；对超大测试文件可进一步按 `test_coverage_map` + node id 选择（pytest `-k` / jest `-t`）。
4. **Discovery 的历史维度**：可接入 `TestRun` 数据标记 flaky 测试，避免把不稳定用例纳入选择性回归。
5. **既有 lint 债务**：`vcs_base.py` 的 `UP042`、`test_repair_agent.py` 的未使用导入，与本次改动无关，建议单独清理。

---

## 9. 范式演进（v2）：从检索管线到信息合成

第 2 节的三条诊断指向同一个结论：**三个 Agent 是按数据源切的，不是按问题切的**；管线只负责"取回"，不负责"判断够不够"。v2 在不推翻现有采集器的前提下，补上了判断层。

### 9.1 新拓扑

```
dependency
   ├─ impact        ┐
   ├─ search        ├→ risk_ranker → context_controller → test_generator → …
   └─ test_discovery┘
```

- **Risk Ranker**：把"受影响面 + 覆盖关系 + 测试历史 + 契约风险"相乘，输出排好序的测试目标（带分数、优先级、理由）。此前这个乘法没人做，被留给了生成器 prompt。
- **Context Controller**：四项证据检查（契约 / 源码 / 影响面 / 覆盖基线），给出 0–100 健康分与 `full | degraded | insufficient` 三态；当源码上下文不足时，在 fan-in 之后用**受影响模块**发起第二轮检索——这是并行采集器自己在拓扑上做不到的事。

### 9.2 为什么是"融合"而不是"合并"

建议原文写的是"合并 Impact + Discovery 为 Risk Ranker"。实现时改成**新增融合节点**，理由：两者都是零成本的确定性扫描，合并会把他们串行化、拉长关键路径，而真正的价值在"乘积"这一结论产物。采集仍并行，判断集中在一个节点。

### 9.3 停止权（stop right）

`context_health.mode == "insufficient"`（既无契约也无源码）时，生成器**不生成**，只写告警。同时移除了 LLM 失败时的 `pass` 桩回退——桩会制造"有产出"的假象，而执行侧现在会跑受影响测试子集，兜底价值不依赖桩。这是本次唯一一处"减少产出"的行为变更，属有意为之。

### 9.4 评分模型

| 信号 | 权重 |
| :--- | :--- |
| 该文件无任何覆盖测试 | +40 |
| 存在直接依赖方（深度 1） / 仅传递依赖 | +25 / +10 |
| 架构层级：api / service / model / module | +15 / +10 / +8 / +4 |
| 变更包含 public 符号 | +10 |
| 覆盖它的测试近期失败 / flaky / 慢 | +20 / +10 / +5 |

优先级：≥70 critical，≥50 high，≥30 medium，其余 low。

### 9.5 新增件与验证

| 文件 | 作用 |
| :--- | :--- |
| `app/services/contract_extractor.py` | 从 AST 抽取签名/参数/返回/装饰器/意图，渲染为 prompt 块 |
| `app/services/risk_ranker.py` | 纯函数排序器（无 I/O，可独立审计） |
| `app/services/test_history.py` | `test_runs`/`test_results` 聚合：失败、flaky、慢测试 |
| `app/services/context_retrieval.py` | 检索原语（向量、过滤、去重排序），首轮与次轮共用 |
| `app/agents/risk_ranker_agent.py` | 融合节点 |
| `app/agents/context_controller_agent.py` | 充分性评估 + 二次检索 + 模式判定 |

验证：新增 16 个用例（契约、历史聚合、排序、控制器、门控、图接线），全量 **69 passed**，仍是那 2 个既有环境失败。

### 9.6 这一版关闭了第 8 节的哪些项

- ✅ 历史与运行时证据（`TestRun` 已接入排序）
- ✅ 充分性判断与"拒绝生成"
- ✅ 采集器之间缺失的交叉（由融合节点补齐）
- ⬜ 告警在 PR 评论正文/前端展示
- ⬜ Search 的第三层（关键词 / 磁盘扫描仍只在 `/ai/search` 接口）
- ⬜ 选择粒度到测试函数（当前仍是文件级）

### 9.7 v2 的已知边界

1. **契约是单侧的**：只抽取当前 revision 的签名，没有和 base 版本做 diff。真正的"契约变更"需要拉取旧 blob，是下一步。
2. **历史冷启动**：新仓库没有 `test_runs`，排序退化为纯静态信号（会有告警提示）。
3. **第二轮检索只在源码缺失时触发**：若首轮拿到了"量大但无关"的片段，不会重试——判断相关性需要交叉编码器或 LLM 复核，成本更高，暂未纳入。

---

## 10. 运行时硬化（v3）

第 9 节解决了“信息是否可信”，本节解决“运行是否安全”。架构审视的结论是：智力层可以交付，物理层还是原型级。

### 10.1 已修（P0）

| 问题 | 原状 | 现状 |
| :--- | :--- | :--- |
| 跨运行污染 | 报告写死 `/tmp/testpilot_report.json`；生成的测试写同一路径 | 每次运行独立 scratch 目录 + 运行级文件后缀（`test__run_<id>.py`），并发同仓库 PR 不再互相覆盖 |
| 工作区竞争 | 索引/分析/修复同时改写同一 checkout | `repository_lock`：按仓库加锁、可移植 `mkdir` 实现、超时可配置、崩溃后按 `stale_after` 回收 |
| 生成代码泄密 | 子进程继承全部环境变量（LLM key、DATABASE_URL、JWT） | 环境白名单 + 前缀/键黑名单，`HOME`/`TMPDIR` 指向运行目录 |
| 生成代码失控 | 无任何资源限制 | POSIX `RLIMIT_CPU/AS/NOFILE/FSIZE/CORE`；`SANDBOX_MODE=docker` 时走 `--network=none`、只读根、非 root 容器 |
| 静默落库 | PG 连不上就写本地 SQLite，服务仍 healthy | 默认 fail-fast；只有显式 `ALLOW_SQLITE_FALLBACK=true` 才降级 |
| 假健康 | `/health` 恒 200 | 依赖不健康返回 503 `degraded`，逐依赖状态 |
| WS 越权 | 任意 `pr_id` 可订阅 | 必须带 `?token=`，并校验 PR / 仓库归属 |
| 启动期 DDL | 每次启动 `ALTER TABLE` | 删除；schema 只由 Alembic 拥有 |
| 重试重复评论 | 每次重试 `db.add(ReviewComment)` | 改为 upsert，一次分析只保留一条评论 |

### 10.2 新增配置

`SANDBOX_MODE`（subprocess|docker）、`SANDBOX_MEMORY_MB`、`SANDBOX_CPU_SECONDS`、`SANDBOX_DOCKER_IMAGE`、`ALLOW_SQLITE_FALLBACK`、`REPOSITORY_LOCK_TIMEOUT_SECONDS`。

### 10.3 验证

新增 17 个用例（环境清洗、rlimit、docker 命令、运行目录唯一性、锁互斥与过期回收、健康检查 503/200、WS 鉴权四态、重试幂等），全量 **86 passed**，仍是那 2 个既有环境失败。

### 10.4 审视中新发现的问题（未修）

1. **分析的不是 PR head**：`diff_agent` / `test_discovery_agent` / 生成器读取的是 `repo_storage_path/<repository_id>`，即**默认分支的克隆**，而 PR 的 head 从未被 checkout 或 fetch。于是 `changed_nodes` 的行号、源码切片、覆盖率映射都可能对应旧代码。→ **已在第 11 节修复（per-run worktree）**。
2. **队列未隔离**：indexing（CPU 密集 embedding，软限 30 分钟）与 pr_pipeline 仍是同一个 worker 池（concurrency=4），大仓库索引会饿死 PR 分析。
3. **客户端/服务端版本偏斜**：compose 固定 Qdrant `1.13.0`，客户端约束 `>=1.13,<2`（实装 1.20）。

---

## 11. 分析对象修正（v4）

第 10 节留下的头号问题：**管线分析的不是 PR 的代码**。索引克隆停在默认分支，而 `diff_agent`、`test_discovery_agent`、生成器全都读那个目录——行号、源码切片、覆盖率映射可能对应 PR 从未触碰的版本。

### 11.1 做法

新增 [`app/services/pr_checkout.py`](../../backend/app/services/pr_checkout.py)：

1. 若 `head_sha` 已在本地对象库（`git cat-file -e <sha>^{commit}`）→ 直接建 worktree；
2. 否则按 provider 依次尝试 fetch：`refs/pull/<n>/head`（GitHub/Azure）、`refs/merge-requests/<n>/head`（GitLab）、`refs/pull-requests/<n>/from`（Bitbucket）；再退化为直接 fetch SHA；
3. `git worktree add --detach <sibling>/.<repo>-pr-<run_id> <sha>`——worktree 建在仓库**外面**，不污染索引克隆；
4. token 通过 `GIT_CONFIG_COUNT/KEY/VALUE` 传 `http.extraheader`，**不写进 `.git/config`、不拼进 URL**；
5. 解析失败 → 回退默认分支并写 warning（“行号与源码可能与 PR 不一致”），仍然是“不假装知道”。

管线侧：`pr_pipeline` 在仓库锁内完成 checkout，把 `workspace_path` / `checkout_mode` 写进 state；所有读文件的 Agent 统一走 `resolve_repo_root(state)`；运行结束后 `release_pr_checkout()` 在 `finally` 中清理 worktree。

### 11.2 验证

新增 5 个用例（真实 git 仓库）：worktree 内容确为 head 而非 base、无法解析时回退并告警、checkout 缺失降级、`resolve_repo_root` 优先级、`diff_agent` 通过 `workspace_path` 读到 PR 侧源码。全量 **91 passed**。

### 11.3 仍未覆盖

- **token 来源有限**：只接 `GITHUB_TOKEN` 环境变量；GitHub App 安装 token 尚未接进 checkout，私有仓库在多租户场景下可能走回退路径（会告警，不会静默）。
- 队列隔离（indexing 独占 worker）与 Qdrant 客户端/服务端版本偏斜仍未处理。

---

## 12. 生成回路（v5）

最后补的一处结构性缺口：整条链路只有**一次**生成机会。失败之后只有补丁级修复（`repair_agent`，最多 2 轮），没有第二次“设计”测试的机会。

### 12.1 为什么要克制

盲目重生成会制造一个更糟的失败模式：**用重采样把真实回归藏起来**。所以回路的第一件事不是“再来一次”，而是**归因**。

`failure_analysis_agent` 现在输出 `failure_origin ∈ {test_defect, product_defect, environment}`，并在 prompt 里明确“存疑时选 product_defect”。未知值在 `normalize_failure_origin()` 里也归到 `product_defect`——安全侧默认。

### 12.2 两条回路

| 触发 | 动作 | 上限 |
| :--- | :--- | :--- |
| `test_defect` 占严格多数 | 回生成器，带失败反馈（根因 + 建议修法）重新生成整包 | `MAX_GENERATION_ATTEMPTS=2` |
| 首轮后 `critical`/`high` 目标无任何生成测试 | 针对这些目标做一次聚焦补位，与首轮结果按 `(文件, 函数)` 去重合并 | `MAX_COVERAGE_TOPUPS=1` |
| 其余（product / environment / 预算耗尽） | 走 `repair_agent` 打补丁，或原样进入评审 | `max_repairs=2` |

严格多数的定义是 `test_defects > product_defects` **且** `test_defects > 样本数/2`：一个测试缺陷 + 一个产品缺陷 → 不重生成。

### 12.3 顺手修掉的一处

`_apply_test_repair` 原本用模型返回的片段**整体替换** `content`（`imports + setup + body`），很可能把 imports 抹掉、把“断言错”变成“NameError”。现在拆出 `_split_test_prefix()`：模型只给函数体时保留原 imports/setup，给了完整文件才整体替换。

### 12.4 验证

新增 15 个用例（归因路由四态、反馈只取 test_defect、缺失关键目标识别、合并去重、feedback 段落开关、repair 前缀保留、in-place 更新），全量 **106 passed**，仍是那 2 个既有环境失败。

### 12.5 仍未做

- **生成级 k 次采样**（best-of-k / self-consistency）：成本线性上升，且与“先归因再重试”的收益重叠，暂不做。
- **跨 PR 的失败记忆**：同一模式在不同 PR 反复出现时没有提前约束 prompt。

---

## 13. 编排修复（v6）

前五版都在补“信息”和“运行时”，没人检查过图本身能不能跑通。结论是：**不能**。

### 13.1 P0：并行扇出写了同一个普通通道

`dependency_agent` 一次连到 `impact_agent` / `search_agent` / `test_discovery_agent`，三者在**同一 superstep** 执行。它们都返回：

- `warnings`
- `completed_agents`
- `current_agent`

LangGraph 的普通通道每步只接受一次写入，第二次写入直接抛 `InvalidUpdateError: At key 'warnings': Can receive only one value per step`。也就是说，任何有变更符号的 PR 都会在扇出后立即失败；Celery 会当成任务异常重试三次，每次重跑 checkout 与前置阶段。之前所有 Agent 单测都是直接调用节点函数，绕过了通道合并，所以没有暴露。

### 13.2 修法：append-only 通道 + 节点只写增量

[`app/agents/state.py`](../../backend/app/agents/state.py)：

```python
def append_unique(existing, update): ...   # 保序去重
Diagnostics = Annotated[list[str], append_unique]
```

`warnings` / `errors` / `completed_agents` 改为 `Diagnostics`；所有节点只返回**自己新增的条目**，不再复制整份列表（否则 reducer 会把旧条目重复并入）。

`current_agent` 被删除：并行分支下“当前 Agent”没有唯一答案，它也没有任何消费者（`AgentPipelineStatusResponse` 未被使用）。进度应看 `completed_agents`。

### 13.3 节点级兜底

[`graph.py`](../../backend/app/agents/graph.py) 的 `_guarded()` 包住每个节点：未捕获异常写入 `errors`（`<agent>: unhandled <Type>: <msg>`）、记 `logger.exception`，图继续走。该节点**不会**被标记 completed——完成清单必须诚实。

这层不是用来吞领域错误（各 Agent 自己已经 try/except），而是防止一个编程错误让整次分析报废并触发昂贵重试。

### 13.4 预算进入 state

`max_repair_attempts`（默认 2）与 `max_mutation_attempts`（默认 1，0 关闭红队）加入 `Settings`，由 `pr_pipeline` 写入初始 state，路由函数因此只依赖 state，可直接单测。

顺手修掉 `int(state.get("max_mutation_attempts", 1) or 1)`：`0 or 1 == 1`，会把“关闭”变成“开启”。改为仅对 `None` 取默认。

### 13.5 验证

新增 [`tests/test_orchestration.py`](../../backend/tests/test_orchestration.py) 11 个用例：

| 用例 | 断言 |
| :--- | :--- |
| 并行合并 | 三个采集器的 warning 全部保留，13 个节点各完成一次，无 `InvalidUpdateError` |
| 节点崩溃 | 注入 `RuntimeError` 后图仍到 END，错误可见，崩溃节点不计 completed |
| 拓扑快照 | 编译后的图恰有 15 个 Agent 节点 |
| 路由门控 | diff 提前终止、无测试跳过执行、上下文不足、mutation/repair/regeneration 预算边界 |

结果：Agent/编排相关 96 passed；全量中仅既有环境用例失败（webhook secret）。`ruff check app/ tests/` 通过。

### 13.6 文档同步

README、`docs/architecture.md`、`docs/PROJECT_OVERVIEW.md` 的节点数与拓扑统一为 **15 个 Agent**（此前写 13/14，且 architecture 图里没有 Mutation 分支）。

---

## 14. 决策审计与断点续跑（v7 / Phase 1）

第 13 节修好了“图能不能跑”；这一节处理“跑挂了会怎样”。此前 Celery 的重试语义是**整图重跑**：已经完成的 diff、检索、生成、执行全部重算，LLM 调用重复付费；而 Agent 的 `errors` 只落库，review 仍以“正常完成”的口吻输出。两件事都不符合一次生产级编排应有的行为。

### 14.1 决策审计通道

新增 `AgentState.decisions`（`app/agents/state.py`）：

```python
Decision = {agent, choice, reason, inputs, attempt, at}
append_decisions(existing, update)  # 只追加，上限 200 条
```

与 `warnings` 的区别：warning 描述“哪里降级了”，decision 描述“为什么走了这条边”。记录点覆盖真正的分叉：

| Agent | 记录内容 |
| :--- | :--- |
| planner | `proceed` / `stop`（缺哪些输入） |
| diff | `analyze` / `no_symbols`（文件数、符号数、契约数） |
| risk_ranker | `rank` / `no_targets`（top 目标与优先级） |
| context_controller | `full` / `degraded` / `insufficient`（分数、四项检查、检索轮次） |
| test_generator | `generate`（数量、第几轮、是否带失败反馈/变异反馈）/ `decline` / `skip` |
| execution | `run`（选择范围、通过/失败数）/ `error` |
| failure_analysis | `triage`（test/product/environment 各多少） |
| repair | `repair` / `no_patch` / `budget_exhausted` / `no_failures` / `error` |
| mutation | `mutate`（kill 比）/ `skip` / `no_mutants` |
| review | `review`（风险等级、行动项数）/ `fallback`（生成失败，降级评审） |

持久化：`pull_requests.analysis_decisions`（迁移 `9d4c1a7e6b20`），PR 详情接口返回结构化的 `analysis_decisions`；`_store_analysis_results` 与 warnings 同事务写入。

### 14.2 断点续跑

新增 `app/agents/checkpointing.py` 与 `app/agents/runner.py`：

- **稳定的 thread**：`sha256(pr_id, head_sha)`，重试落到同一 thread；新 commit 才是新 thread。
- **后端**：`memory`（默认，测试/本地，进程内）、`postgres`（`PostgresSaver` + psycopg，跨进程续跑）、`off`。Postgres 不可用时降级 memory 并告警，**不会**让 worker 启动失败。
- **续跑条件**：`snapshot.next` 非空且 `workspace_path` 仍存在；worktree 缺失就不假装能续跑，直接重新开始。
- **已完成的 thread 不复用**：否则 append-only 的 warnings/decisions 会把两次分析的审计混在一起，新输入走新的 thread 后缀。
- **worktree 跨重试保留**：异常且还有重试次数时不释放 checkout，最后一次尝试或成功时才释放。没有树的 checkpoint 等于没有 checkpoint。
- **稳定 run_id**：`stable_run_id(pr_id, head_sha)` 替代随机 id，让 worktree 路径在重试间保持一致。
- **同任务锁回收**：`repository_lock(owner_id=self.request.id)`；上一次尝试崩溃留下的锁在重试时立即回收，不必等 1 小时的 `stale_after` 窗口——否则“能续跑”会被锁直接挡死。
- **续跑失败可回退**：checkpoint 损坏或与当前图不兼容时，记录告警并从头开始，而不是让同一个坏 thread 把重试卡死。
- **memory 后端回收**：运行结束后丢弃该 thread，避免长驻 worker 把每次分析都留在内存里；Postgres 历史保留供排查。

配置：`CHECKPOINT_BACKEND`、`CHECKPOINT_DSN`；compose 给 `celery-worker` 显式设为 `postgres`，Docker 镜像通过 `backend/requirements.txt` 安装 `langgraph-checkpoint-postgres` 与 `psycopg[binary]`。

**依赖说明**：`pyproject.toml`/`poetry.lock` 仍锁在 LangGraph 0.2 系列，而本地验证环境是 1.2.7；本次只更新了 Docker 实际使用的 `requirements.txt`（约束 `>=2.0.21,<4`，可与 0.2 的 checkpoint 2.x 或 1.x 的 4.x 配对）。若要让 Poetry 开发环境也具备持久化能力，需要先重新生成 lock，再把两项依赖加入 `pyproject.toml`——这一步没有在本次执行，避免破坏 CI 的 `poetry install`。

### 14.3 故障注入验证

新增 [`tests/test_checkpoint_resume.py`](../../backend/tests/test_checkpoint_resume.py) 10 个用例：

| 用例 | 断言 |
| :--- | :--- |
| 决策记录 | 字段齐全、UTC 时间戳、inputs/attempt 保留 |
| 审计上限 | 只追加、超过 200 条丢最旧 |
| thread 稳定 | 同 PR 同 head 相同，跨 head/PR 不同 |
| DSN 转换 | `postgresql+asyncpg://` → libpq |
| 后端工厂 | memory 返回 saver；off 返回 None |
| 驱动缺失 | postgres 后端在缺少 psycopg 时降级 memory，不阻断 worker |
| **中断续跑** | 在 generator 后打断，resume 从 execution 继续；generator 调用次数仍为 1 |
| 完成态 thread | 新分析使用新 thread，不复用旧审计 |
| memory 回收 | 完成后的 thread 可被删除 |
| 可续跑判定 | 无 pending / worktree 丢失 均拒绝 |
| Postgres 往返（opt-in） | 真实 PostgresSaver 建表 → 中断 → 续跑 → 删 thread |

验证：10 个默认编排用例 + 1 个 `CHECKPOINT_TEST_DSN` 集成用例（本机用临时 Postgres 16 容器实跑通过）+ 持久化 decisions 断言 + `stable_run_id` 与锁 owner 回收用例；相关编排/持久化/运行时用例全部通过，`ruff check app/ tests/` 通过。

### 14.4 已知边界

1. **版本矩阵仍需在真实镜像里 smoke 一次**：本机用 `checkpoint-postgres 3.2 / checkpoint 4.3 / LangGraph 1.2.7` 在临时 Postgres 16 容器上跑通了建表、中断、续跑、删 thread；而 `requirements.txt` 仍锁 `langgraph<0.3`，Docker 会解析到 `checkpoint-postgres 2.x`（构造函数、`setup()`、`from_conn_string` 与 3.x 一致，但未实跑）。要么在镜像里跑一次 smoke，要么把 LangGraph/checkpoint 的 pin 一起升级。
2. **Postgres checkpoint 保留策略**：memory 后端运行结束即回收；Postgres 的已完成 thread 会持续累积，尚未接入按 `created_at` 的定期清理（可作为 Celery beat 任务）。
3. **决策前端化**：接口已返回，PR 评论与前端详情尚未展示。
4. **原子性**：`_store_analysis_results` 失败会触发重试，此时图已完成，下一次会新建 thread 重跑分析；把“落库”从可重试路径里拆出去是下一步。

---

## 15. 验证回路子图（v8 / Phase 2）

第 13 节把图修通了，第 14 节让它可恢复；这一节处理**可读性**：主图不该长得像循环本身。

此前 `generator / execution / failure / repair / mutation` 是 5 个顶层节点、4 条条件边、4 个预算字段，分别挂在 `graph.py` 里。后果有三个：主拓扑要读完整个循环才能看懂；任何一条回路加条件都要改主图；评审无法回答“这次为什么停”。

### 15.1 拓扑

新增 [`app/agents/verify_graph.py`](../../backend/app/agents/verify_graph.py)：

```
generate → execute → triage → {regenerate | repair} → execute
                   └→ (green) mutation → generate（加固断言）
所有出口 → verify_finalize
```

> 第 18 节把这 5 个 Agent 节点进一步收敛为控制器工具，图里只剩 `observe / decide / act / settle`。

- 主图从 15 个平铺节点降为 **11 个节点**，验证回路作为一个 `verify` 节点嵌入；
- `verify_finalize` 是**唯一出口**，写 `verification = {exit_reason, rounds, generated, executed, passed, failed, mutation_score, repaired}`；
- `exit_reason` 是闭集：`green` / `no_tests` / `failures_reported` / `mutation_survived` / `budget_exhausted` / `execution_error`；
- 退出原因同时写一条 `decisions`（`agent=verify`），评审正文展示 `Verify exit`。

路由与节点包装拆到 [`routing.py`](../../backend/app/agents/routing.py) 与 [`node_guard.py`](../../backend/app/agents/node_guard.py)，`graph.py` 重新导出旧名字，测试与调用方不感知迁移。

### 15.2 全局轮次预算

`verify_rounds` 在每次 execution 时 +1，路由层统一检查 `verify_rounds < max_verify_rounds`（默认 3，`MAX_VERIFY_ROUNDS`）：

- 通过后不再进入 mutation；
- 失败分析仍会运行（诊断是红跑最有价值的产物），但不再开启新一轮 regeneration/repair；
- repair 补丁不会在预算耗尽后重跑。

这是**兜底**：原来的 `generation_attempts` / `repair_attempts` / `mutation_attempts` 保留为子预算，即使某一个被配错，整条回路也不会无限循环。

### 15.3 验证

新增 [`tests/test_verify_subgraph.py`](../../backend/tests/test_verify_subgraph.py) 6 个用例：

| 用例 | 断言 |
| :--- | :--- |
| green | 通过后退出 `green`，generator/execution/mutation 均完成 |
| no_tests | 无生成测试且无覆盖测试 → `no_tests`，不执行 |
| failures | 失败 → `failures_reported`，经 failure/repair 后退出，不重复跑同一失败套件 |
| budget | `verify_rounds=3/3` 时执行仍完成，但退出 `budget_exhausted`，不再开新一轮 |
| mutation | 存活变异 + 生成预算耗尽 → `mutation_survived` |
| decision | 退出原因写入 decisions |

同步调整：

- 编排测试的节点统计改为「主图 + 子图并集 = 15 个 Agent」；
- 断点续跑测试改为对 verify 子图做 `interrupt_after`，并新增**嵌入式崩溃**用例：在 `execution_agent` 抛 `KeyboardInterrupt`（绕过节点 guard，等价于 worker 被杀），重入主图后 generator 调用次数仍为 1、execution 重试 1 次，说明子图内部进度确实写进了父图 checkpoint；
- 子图中断时父快照的 `values` 可能是空的（状态在子图命名空间里），续跑判定因此改为「有 pending 节点 + 确定性 worktree 存在」，否则会在最贵的阶段悄悄退化成从头重跑；
- `test_mutation_arena` 等旧路由用例保持通过（`graph.py` 重新导出）。

相关编排 / 检查点 / 持久化 / 运行时用例 **53 passed, 1 skipped**，`ruff check` 通过。

### 15.4 已知边界

1. **子预算尚未合并成一个对象**：`generation_attempts` / `repair_attempts` / `mutation_attempts` 仍是扁平字段，全局轮次预算只做上限兜底；把它们收进一个 `VerifyBudget` 是下一步的可选重构。
2. **exit_reason 未单独落库**：目前通过 `analysis_decisions` 持久化；若要给 EvalOps 做聚合，建议加 `verification_exit_reason` 列。
3. **子图 checkpoint 命名空间**：LangGraph 会为子图 checkpoint 增加命名空间；跨版本升级时旧 thread 的兼容性需在真实环境验证一次。

---

## 16. 上下文子图与 GenerationPlan（v9 / Phase 3）

第 15 节之后，主图还剩 7 个上下文节点平铺在外面。它们不是独立的阶段，而是同一个问题：**“接下来该测什么，证据够不够”**。

### 16.1 上下文子图

新增 [`app/agents/context_graph.py`](../../backend/app/agents/context_graph.py)：`diff → dependency → [impact ‖ search ‖ discovery] → ranker → controller`。主图因此变成 **5 个节点**：

```
planner → context → (verify | review | END) → review → documentation
```

子图的唯一出口是 context controller，它现在写一个 [`GenerationPlan`](../../backend/app/agents/state.py)：

| 字段 | 含义 |
| :--- | :--- |
| `declined` / `decline_reason` | 是否拒绝生成；`no_changed_symbols` 或 `context_insufficient` |
| `context_mode` / `context_score` / `evidence` | 四项证据检查与 0–100 分 |
| `targets` | 排序后的测试目标 |
| `changed_symbols` / `affected_files` | 计划依据的规模 |

父图只读 `should_start_verify(state)`：`declined` 且无覆盖测试 → 直接 review；`declined` 但有覆盖测试 → 仍进 verify（generator 跳过、回归子集照跑）；`should_stop` → END。生成器也优先读 plan 的 `declined`，不再重复判断 context_health。

### 16.2 VerifyBudget

`generation_attempts` / `repair_attempts` / `mutation_attempts` / `rounds` 收进一个 `budget` 对象（`VerifyBudget`），带 `merge_budget` reducer，节点只返回自己用掉的那一项：

```python
{"budget": {"repair_attempts": 2}}
```

路由统一用 `_budget_value()` 读取，**budget 优先，扁平字段兜底**（旧 checkpoint 与外部调用）。`pr_pipeline` 初始化时写入上限；扁平字段保留为 deprecated mirror，计划在一版后删除。

### 16.3 验证

新增 [`tests/test_context_subgraph.py`](../../backend/tests/test_context_subgraph.py) 4 个用例：

- 无变更符号 → `declined=true`、`decline_reason=no_changed_symbols`；
- 证据齐全 → plan 带 targets、100 分、affected_files；
- `should_start_verify` 四态（stop / declined+无覆盖 / declined+有覆盖 / 正常）；
- 上下文子图最终产出 plan。

[`tests/test_verify_subgraph.py`](../../backend/tests/test_verify_subgraph.py) 增加 budget 优先级用例：`budget.rounds=3` 覆盖扁平的 `verify_rounds=0`，退出 `budget_exhausted`。

相关编排 / 上下文 / 检查点 / 持久化 / 运行时用例 **58 passed, 1 skipped**，`ruff check` 通过。

### 16.4 已知边界

1. **扁平 mirror 仍存在**：`verify_rounds` 等字段还在写，删除它们需要同步旧 checkpoint 的反序列化策略；当前以 budget 为权威、mirror 为兼容。
2. **plan 未单独落库**：`generation_plan` 是运行期契约，退出原因可通过 decisions/日志追溯；若要给 EvalOps 做聚合，建议和 `verification.exit_reason` 一起加列。
3. **上下文子图的崩溃续跑未单独测试**：verify 子图的嵌入式崩溃已覆盖；context 侧节点多为确定性扫描，重跑成本可接受。

---

## 17. 收尾：结果落库、保留策略与 mirror 清理（v10 / Phase 4）

### 17.1 结果落库

`pull_requests` 新增四列（迁移 `b7e2f4a91c30`）：

| 列 | 来源 | 用途 |
| :--- | :--- | :--- |
| `verify_exit_reason` | `verification.exit_reason` | EvalOps 聚合 green / budget_exhausted / failures_reported |
| `verify_rounds` | `verification.rounds` | 轮次分布、预算调参 |
| `context_mode` | `context_health.mode` | 上下文质量 |
| `generation_plan_summary` | plan 摘要（declined / reason / score / targets / symbols） | 解释“为什么没测” |

PR 详情接口返回同名字段（plan 摘要为结构化对象），`_store_analysis_results` 与 warnings/decisions 同一事务写入。

EvalOps 新增三个比率（近 7 个 PR）：`verify_green_rate`、`verify_budget_exhaustion_rate`、`context_declined_rate`。

### 17.2 Checkpoint 保留策略

LangGraph 自己不清理 checkpoint。新增 `analysis_checkpoints` 表（迁移 `c4a8e1d72f90`）记录 `thread_id / pr_id / completed_at`；分析成功或最终失败时写入（幂等 upsert）。

新增 [`app/tasks/maintenance.py`](../../backend/app/tasks/maintenance.py)：

- Celery beat 每 24h 运行 `prune_checkpoints`；
- 删除早于 `CHECKPOINT_RETENTION_DAYS`（默认 7）的 thread，用 `PostgresSaver.delete_thread()`，再删 bookkeeping 行；
- 若 backend 不是 postgres，或 factory 降级到 memory，**拒绝执行**——否则会删掉 registry 行却留下真正的 checkpoint 行。

### 17.3 删除扁平 mirror

`AgentState` 不再声明 `generation_attempts`、`repair_attempts`、`mutation_attempts`、`verify_rounds` 等 10 个字段；节点只写 `budget`，任务只初始化 `budget`。路由仍保留对旧扁平字段的读取兜底，用于反序列化 Phase 1–3 的 checkpoint；新运行不再产生它们。

### 17.4 验证

新增 [`tests/test_checkpoint_retention.py`](../../backend/tests/test_checkpoint_retention.py) 4 个用例：只删过期 thread、registry 行同步删除、注册幂等、memory 降级拒绝。

持久化测试补充 verification/plan 列与 EvalOps 三个比率的断言。

真实环境验证：用临时 Postgres 16 容器建表后写入 old/fresh 两个 thread，`prune_expired(7, PostgresSaver)` 删除了 old（checkpoint 与 registry 行同时消失）、保留了 fresh。

相关编排 / 上下文 / 检查点 / 持久化 / 运行时用例 **62 passed, 1 skipped**，`ruff check` 通过，`alembic heads` 单一 head `c4a8e1d72f90`。

### 17.5 已知边界

1. **Celery 调度未实跑**：保留逻辑已在真实 Postgres 上验证（见 17.4），但 beat 的 24h 调度本身只在配置层验证；上线后观察第一次执行日志即可。
2. **旧 checkpoint 兼容**：路由仍读扁平字段；一旦所有存量 thread 过期（默认 7 天），可以删除这些兜底分支。
3. **历史列不回填**：Phase 4 之前的 PR 的 `verify_exit_reason` 为 NULL，EvalOps 只统计非空样本。

---

## 18. 受限自主与回路语义修复（v11）

### 18.1 先修回路语义

自主化之前先止血，否则聪明的控制器只会更高效地空转：

| 问题 | 修复 |
| :--- | :--- |
| source patch 不落地却重跑 execution | `repair_applied_tests` 记录本轮应用的测试补丁数；为 0 时不再回 execution，退出 `source_patches_proposed` |
| 重生成覆盖整包 | 第二轮起合并：新定义按 (file, function) 覆盖旧定义，上一轮失败用例被替换，通过的用例保留 |
| 同一失败无限重试 | execution 计算失败签名；连续两次相同 → `no_progress`，不再开新一轮 |
| exit 原因不够用 | 新增 `no_progress`、`source_patches_proposed` |

### 18.2 自主控制器

新增 [`app/agents/verify_autonomy.py`](../../backend/app/agents/verify_autonomy.py)：

```
observe → decide → act → observe
              └→ settle（确定性裁决）
```

- **动作闭集**：`generate / run / diagnose / repair / mutate / finish`；`decide` 只选动作，不写命令、不改源码。
- **observe** 只喂结构化快照（计划、预算、执行计数、最多 10 条失败摘要、最近动作），不喂原始日志，降低测试输出的提示注入面。
- **act** 分发到确定性工具：generation/execution/failure/repair/mutation 节点；工具异常记录后强制 finish。
- **guard**：`VERIFY_MAX_STEPS`（默认 6）、round budget、无进展签名，在问模型之前检查。
- **settle** 复用 `verify_finalize_node`：green 必须由执行证据推出，模型不能自己宣布通过。
- **模式**：`VERIFY_MODE=deterministic`（默认）/ `shadow`（模型提议、确定性执行）/ `autonomous`（模型执行，失败回退 heuristic）。

LLM 决策用 `instructor` 输出 `ActionDecision{action, reason}`；不可用、超时或返回非法动作时回退 `propose_action()`。

**拓扑收敛**：5 个 Agent 不再是图节点，`verify` 子图只剩 `observe / decide / act / settle` 四个编排节点；`deterministic` 只是“用 heuristic 当 decider”，不再维护第二套条件边。这样只有一套编排壳、一套预算检查、一份 ledger。

### 18.3 验证

新增 [`tests/test_verify_autonomy.py`](../../backend/tests/test_verify_autonomy.py) 5 个用例（脚本化 decider，不调用真实 LLM）：

- autonomous：run → finish，green；
- shadow：模型提议 generate，实际执行 run，提议写入 decisions；
- step guard：`max_steps=2` 时强制 finish；
- 工具异常：记录 error 并退出，不循环；
- source-only 修复：`propose_action` 直接 finish。

验证子图补充 no-progress 与 source-only 两个用例；生成回路补充“重生成合并 / 失败用例替换”用例；另加一个主图集成用例，把 autonomous 子图嵌入 `planner → context → verify → review` 跑通。相关编排/自主/验证/生成/检查点/持久化/运行时 **87 passed, 1 skipped**，`ruff check` 通过；`VERIFY_MODE=autonomous` 下导入主图无循环依赖。

### 18.4 已知边界

1. **LLM decider 未接真实模型跑过**：测试用脚本化 decider；上线前建议 `VERIFY_MODE=shadow` 跑一批真实 PR，对比提议与确定性选择的一致率和 rounds-to-green。
2. **工具仍是 agent 函数适配，不是纯服务**：`act` 直接调用原来的节点函数（它们已不在图里），副作用与之前一致；拆成无状态服务是后续重构。
3. **成本/延迟未量化**：每次 decide 一次 LLM 调用，上限 6 次；shadow 也会产生调用，建议只在小流量开启。
4. **默认 deterministic**：自主模式需要显式配置，避免默认行为漂移。

---

## 19. 另起一套 verify 引擎（v12 / 骨架）

### 19.1 原则：复用能力，不继承契约

旧实现的问题不是逻辑，而是形状：`*_agent_node(state) -> partial_state` 让工具知道整个 `AgentState`，越权写编排元数据，五个动作的返回结构还互不相同。新引擎从**契约**出发，只复用沙箱、runner、解析器这类底层能力。

### 19.2 结构

```
backend/app/verify/
├── contracts.py    # VerifyRequest / RunResult / VerifyReport / ...
├── tools.py        # ToolSpec / ToolRegistry / 四个能力
├── runners.py      # SandboxTestRunner：真实 pytest + JUnit XML
├── services.py     # repair / patch proposal / mutation probe / bundle editor
├── assembly.py     # run_default_verify：生产入口
├── adapter.py      # AgentState ↔ VerifyRequest/Report，VERIFY_ENGINE=next
└── controller.py   # 纯循环：observe → decide → act → settle
```

| 类型 | 作用 |
| :--- | :--- |
| `VerifyRequest` | workspace + `TestBundle` + runner + `VerifyBudget` |
| `RunResult` | 一次执行的通过/失败/错误与 `FailureFact` 列表 |
| `ToolOutcome` | 工具只返回结果，控制器负责写 run/ledger/预算 |
| `VerifyReport` | 唯一输出：verdict、exit_reason、runs、failures、proposals、mutations、ledger |

工具只有四个：`execute`、`repair_tests`（只改生成测试）、`propose_source_patch`（只记录、不落地）、`probe_mutations`。`finish` 是控制器原语，不是工具。

控制器不依赖 LangGraph，也不读 `AgentState`：`decide` 只拿到 `VerifyRun` 和可用工具名，`act` 校验参数后分发。`HeuristicDecider` 是默认策略；模型 decider 可以后续实现同一 Protocol。

### 19.3 契约测试

新增 [`tests/test_verify_engine.py`](../../backend/tests/test_verify_engine.py) 11 个用例：

| 用例 | 断言 |
| :--- | :--- |
| green | execute → probe → `green`，ledger 两条 |
| repair | 失败 → `repair_tests` → 再执行 → green，editor 收到 patch |
| source-only | `propose_source_patch` 后不重跑、不落地，`source_patches_proposed` |
| no-progress | 同一失败签名两次 → `no_progress` |
| budget | 轮次上限在控制器层拒绝，不进入第二次 execute |
| unknown tool | 注册表拒绝，`no_applicable_action` |
| invalid args | Pydantic 校验拒绝 |
| no tests | `no_tests`，runner 零调用 |
| registry | 恰好四个能力，schema 可导出 |
| finish | 控制流不是工具，未执行任何工具 |
| serialization | report 可 `model_dump` |

### 19.4 真实 runner 适配

[`app/verify/runners.py`](../../backend/app/verify/runners.py) 的 `SandboxTestRunner`：

- 复用 `sandbox.run_sandboxed` 与 per-run workspace；
- 生成的测试写到 run-scoped 文件名，运行后删除，不污染 checkout；
- 用 pytest 内置 `--junitxml` 解析，不依赖 `pytest-json-report` / `pytest-cov`；
- 5 个真实沙箱用例（green / red / 生成测试清理 / JUnit 解析 / 缺报告）通过。

**顺带发现的存量问题**：旧 execution 节点的 pytest 命令硬编码 `--json-report` 与 `--cov`，但 `requirements.txt` 里没有 pytest 及这两个插件——Docker 镜像里生成测试实际上跑不起来。已在 requirements 增加 `pytest`、`pytest-json-report`、`pytest-cov`。

### 19.5 服务适配

[`app/verify/services.py`](../../backend/app/verify/services.py)：

| 服务 | 行为 | 边界 |
| :--- | :--- | :--- |
| `LLMRepairService` | 根据失败信息 + 当前生成测试内容，返回**完整替换文件** | 只接受已知 test id；源码缺陷不返回补丁 |
| `LLMPatchService` | 根据失败信息生成源码补丁建议 | 只提议、不落地；拒绝绝对路径与 `..` |
| `EngineMutationProbe` | 包装 `MutationEngine.generate_mutants` + `run_mutant_in_sandbox` | 只读源码文件，返回 killed/survived |

三个服务都通过构造函数注入 client，测试用 fake client / monkeypatch，不依赖真实模型或沙箱。

`BundleTestEditor` 负责把 `TestPatch` 写回 `VerifyRequest.bundle`；runner 每次执行前读取该 bundle，所以修复会在下一次 execute 生效，而不是只记录在 ledger 里。

[`app/verify/assembly.py`](../../backend/app/verify/assembly.py) 的 `run_default_verify()` 把 `SandboxTestRunner + LLMRepairService + BundleTestEditor + LLMPatchService + EngineMutationProbe` 装配成生产入口；green 路径不需要 LLM。

新增 [`tests/test_verify_services.py`](../../backend/tests/test_verify_services.py) 7 个用例：补丁映射、未知 test id 丢弃、模型失败 no-op、路径逃逸拒绝、mutation killed/survived 映射；runner 侧另加端到端 green 与 default assembly 用例。

相关套件通过，`ruff check` 与格式通过（legacy 控制器与其测试已删除，具体数量随切主变化，不再固定写死）。

### 19.6 切主与删除 legacy

- 主图新增 `produce` 节点（单次生成），verify 只负责执行、诊断、修复、变异；
- 唯一 verify 实现是 `app/verify`；[`verify_graph.py`](../../backend/app/agents/verify_graph.py) 只剩一个薄入口；
- [`adapter.py`](../../backend/app/verify/adapter.py) 把 `AgentState` 映射成 `VerifyRequest`，再把 `VerifyReport` 映射回 `execution_results` / `generated_tests` / `verification` / `completed_agents`；
- `apply_report` 会把修复后的测试内容写回 `generated_tests`，并只统计真正变化的 patch 数量；
- `pull_requests.verify_engine`（迁移 `d6a1c8e42b70`）保留，用于区分历史行。

已删除：`app/agents/verify_autonomy.py`（含 `VerifyAction` 六动作）、`VERIFY_MODE` / `VERIFY_ENGINE` / `VERIFY_CANARY_RATE` 配置、canary 选择器与测试、旧 verify 子图/自主控制器测试、差分脚本。`routing.py` 只剩 diff 与 produce 两个门。

### 19.7 切主前的本地差分

切主前用临时仓库（`app/calc.py` + 一个 pytest 用例）跑过 legacy 与 next：

| 场景 | legacy | next |
| :--- | :--- | :--- |
| green | green，1 passed，~8.8s，execute + finish | green，1 passed，~6.7s，仅 execute |
| red | 未跑（失败归因会调用 LLM） | red / `failures_reported`，1 failed，8.03s，无 LLM |

green 场景 verdict 与 exit_reason 一致；next 的红跑因为失败归因是 `unknown`，保守地停在 `failures_reported`，不会误修。差分脚本在切主后删除，保留数据作为决策记录。

### 19.8 取舍

1. **控制器不是 LangGraph 子图**：纯循环更好测试、没有状态通道；接入时作为一个节点，checkpoint 粒度是整次 verify。若需要逐步续跑，再暴露 step hook。
2. **工具参数是 Pydantic**：模型 decider 输出 `{tool, args}`，注册表校验；比 enum + if/elif 更容易扩展。
3. **repair 仍是服务**：工具只负责“调用 repairer 并应用 test patch”，LLM 逻辑留在服务里，控制器不碰 prompt。

---

## 附录 A：关键命令

```bash
# 迁移
cd backend && poetry run alembic upgrade head

# 重建向量索引（全量 / 指定 / 内联）
poetry run python scripts/reindex_vectors.py
poetry run python scripts/reindex_vectors.py --repository-id <uuid>
poetry run python scripts/reindex_vectors.py --sync

# 测试
poetry run pytest tests -q
poetry run pytest tests/test_context_agents.py --noconftest -q
```

## 附录 B：关键常量

| 常量 | 值 | 位置 |
| :--- | :--- | :--- |
| 影响面遍历深度 | 5 | `impact_agent.MAX_IMPACT_DEPTH` |
| 每 query 命中数 / query 上限 / 保留条数 | 3 / 10 / 12 | `search_agent` |
| 变更源码 prompt 预算 | 6000 字符 / 5 文件 / 6 符号 | `test_generator_agent` |
| 检索上下文预算 | 6 条 / 4500 字符 | `test_generator_agent` |
| 选择性执行上限 | 200 文件 | `settings.max_selected_test_files` |
| 测试扫描上限 | 20000 文件 | `test_discovery_agent.MAX_FILES_SCANNED` |
| 修复轮次上限 | 2 | `settings.max_repair_attempts` |
| 红队变异轮次上限 | 1（0 关闭） | `settings.max_mutation_attempts` |
| 验证总轮次上限 | 3 | `settings.max_verify_rounds` |
