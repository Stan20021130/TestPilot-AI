# 🏆 TestPilot AI — 大规模宏观基准评测报告 (N=20 PRs, M=5 Repositories)

## 📌 核心宏观结论

> **在 20 个 PR / 5 个代码仓库中，捕获了 20/20 个预埋回归缺陷（100.0% 召回率）**

---

## 1. 核心指标总览

| 维度 | 数值 | 占比 / 召回率 | 说明 |
| :--- | :---: | :---: | :--- |
| **代码仓库总量 ($M$)** | **5** | 100% | 覆盖密码学、缓存、配置解析、插件调用、数据序列化等五类基础架构组件 |
| **评估 PR 样本量 ($N$)** | **20** | 100% | 真实模拟 20 个独立特性/重构 PR 分支 |
| **预埋回归缺陷量 ($m$)** | **20** | 100% | 人工高仿真注入的已知回归缺陷（改边界、删校验、改返回值） |
| **🔥 成功拦截检出量 ($n$)** | **20** | **100.0%** | 成功被测试网拦截并打上失败标签阻止合入的缺陷数 |
| **选择性回归拦截贡献** | **11 / 20** | **55.0%** | 由历史受影响测试用例选择子集直接捕获 |
| **生成测试拦截贡献** | **20 / 20** | **100.0%** | 由针对变更契约合成的定向断言用例精准捕获 |
| **评测执行总耗时** | **320.91s** | - | 20 个 PR 完整双轨沙箱执行与还原总时间 |

---

## 2. 代码仓库维度细分 ($M=5$ Repositories)

| 仓库名称 | 领域分类 | PR 样本数 | 捕获缺陷数 | 检出率 | 选择性回归捕获 | 生成测试捕获 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `itsdangerous` | 密码学签名 & Token 安全 | 4 | 4 | **100.0%** | 3 | 4 |
| `cachetools` | 内存缓存 & LRU/LFU 算法 | 4 | 4 | **100.0%** | 3 | 4 |
| `iniconfig` | INI 文件解析 & 配置引擎 | 4 | 4 | **100.0%** | 4 | 4 |
| `pluggy` | 钩子调用 & 插件管理器 | 4 | 4 | **100.0%** | 0 | 4 |
| `marshmallow` | 模式校验 & 数据序列化 | 4 | 4 | **100.0%** | 1 | 4 |

---

## 3. 缺陷类别维度细分

| 缺陷类别 | 样本量 | 检出数 | 检出率 | 代表性场景 |
| :--- | :---: | :---: | :---: | :--- |
| **改边界** | 5 | 5 | **100.0%** | 容量阈值提前截断 / 超时边界放宽 (+60s) / 模数对齐错误 / 括号切片边界破损 |
| **删校验** | 10 | 10 | **100.0%** | 签名验签完全旁路 / 超大对象尺寸守卫丢失 / 分隔符存在性缺失 / 重复配置覆盖 |
| **改返回值** | 5 | 5 | **100.0%** | 元组解包顺序倒置 / URLSafe 尾部填充未去 / 压缩标头丢失 / 类型转字符串失效 |

---

## 4. 全量 20 个 PR 评测记录明细清单

| PR 编号 | 所属仓库 | PR 标题与变更行为 | 类别 | 选择性回归 | 生成测试 | 综合检出 | 拦截详情 / Killer Test |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| `PR-01` | `itsdangerous` | **feat(timed): optimize TimestampSigner max_age calculation**<br>_篡改过期边界检查：将 age > max_age 放宽为 age > max_age + 60，导致过期 60s 内的令牌被异常放行_ | 改边界 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [tests.test_itsdangerous.test_timed.TestTimestampSigner::test_max_age] + 生成测试用例 [tests.test_gen_pr-01__run_e551106fc1::test_contract_exact_max_age_boundary]` |
| `PR-02` | `itsdangerous` | **refactor(signer): streamline unsign parsing logic**<br>_删校验：删除了签名载荷中分隔符存在性校验，导致无分隔符的畸形数据抛出底层解包异常而非契约约定的 BadSignature_ | 删校验 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [tests.test_itsdangerous.test_signer.TestSigner::test_no_separator] + 生成测试用例 [tests.test_gen_pr-02__run_d20bee7f5b::test_contract_missing_separator_validation]` |
| `PR-03` | `itsdangerous` | **feat(encoding): fast-path urlsafe base64 encoding**<br>_改返回值：Base64 编码时漏掉了 .rstrip(b'=')，导致生成的 URL-safe 字符串夹带 '=' 填充符破坏 URL 规范_ | 改返回值 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [tests.test_gen_pr-03__run_38878f5357::test_contract_base64_encode_must_strip_padding]` |
| `PR-04` | `itsdangerous` | **perf(signer): bypass redundant HMAC validation in unsign**<br>_删校验（重大安全漏洞）：未执行 verify_signature 验签便直接返回载荷，导致篡改与伪造的签名直接逃逸_ | 删校验 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [tests.test_itsdangerous.test_signer.TestSigner::test_broken_signature] + 生成测试用例 [tests.test_gen_pr-04__run_efc6888429::test_contract_tampered_signature_rejection]` |
| `PR-05` | `cachetools` | **refactor(cache): adjust eviction threshold in __setitem__**<br>_改边界：将缓存淘汰边界从 > maxsize 篡改为 >= maxsize，导致容量为 N 时只能存 N-1 个元素，提前淘汰_ | 改边界 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [tests.test_cache.CacheTest::test_clear] + 生成测试用例 [tests.test_gen_pr-05__run_9ca1469486::test_contract_cache_exact_maxsize_capacity]` |
| `PR-06` | `cachetools` | **feat(cache): remove eager size guard for oversized items**<br>_删校验：删除了超大元素插入时的 ValueError 异常拦截，导致单项尺寸超过容量时非正常存入并撑爆缓存_ | 删校验 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [tests.test_cache.CacheTest::test_getsizeof_param] + 生成测试用例 [tests.test_gen_pr-06__run_b85c9430e8::test_contract_oversized_item_must_raise]` |
| `PR-07` | `cachetools` | **refactor(lru): swap popitem return tuple structure**<br>_改返回值：LRUCache.popitem() 返回的 (key, value) 倒置为 (value, key)，破坏字典标准解包契约_ | 改返回值 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [tests.test_gen_pr-07__run_7fe86c4345::test_contract_lru_popitem_key_value_order]` |
| `PR-08` | `cachetools` | **perf(lru): avoid move_to_end on cache hit for read performance**<br>_删校验/状态丢失：读取 LRU 缓存命中时未更新访问次序，导致该热点键沦为最冷键被错误淘汰_ | 删校验 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [tests.test_lru.LRUCacheTest::test_lru] + 生成测试用例 [tests.test_gen_pr-08__run_9de4af50da::test_contract_lru_hit_updates_recency]` |
| `PR-09` | `iniconfig` | **feat(parse): silently merge duplicate INI sections**<br>_删校验：删除了重复 section 头的拦截报错，静默允许重复 section 存在破坏 INI 语法严格性_ | 删校验 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [testing.test_iniconfig::test_iniconig_section_duplicate_fails] + 生成测试用例 [testing.test_gen_pr-09__run_f7ce17fe6b::test_contract_duplicate_section_rejected]` |
| `PR-10` | `iniconfig` | **refactor(parse): loosen section header closing bracket check**<br>_改边界：将 section 结束符匹配 line[-1] == ']' 改写为 line[0] == ']'，导致标准 [section] 无法被识别_ | 改边界 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [testing.test_iniconfig::test_tokenize[blank line]] + 生成测试用例 [testing.test_gen_pr-10__run_153e42d59a::test_contract_standard_section_header_parsed]` |
| `PR-11` | `iniconfig` | **fix(parse): off-by-one slice in section name extraction**<br>_改返回值：切片提取 section 名称时漏切末尾右括号 line[1:]，导致解析出的名称夹带 ']' 字符_ | 改返回值 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [testing.test_iniconfig::test_tokenize[blank line]] + 生成测试用例 [testing.test_gen_pr-11__run_166aa99563::test_contract_section_name_clean_slice]` |
| `PR-12` | `iniconfig` | **feat(parse): allow duplicate keys within same section**<br>_删校验：删除了同一 section 内重名键的 ParseError 校验，导致同名配置静默覆盖_ | 删校验 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [testing.test_iniconfig::test_iniconfig_duplicate_key_fails] + 生成测试用例 [testing.test_gen_pr-12__run_447fb501ff::test_contract_duplicate_key_rejected]` |
| `PR-13` | `pluggy` | **feat(manager): silently allow duplicate plugin registrations**<br>_删校验：删除了插件重名注册时的 ValueError 拦截报错，破坏插件名唯一性约束契约_ | 删校验 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [testing.test_gen_pr-13__run_9d6309359e::test_contract_duplicate_plugin_name_rejected]` |
| `PR-14` | `pluggy` | **refactor(manager): return string token when registering None**<br>_改返回值：register(None) 原本返回 None，改写为返回非空字符串 'none_plugin'，破坏契约规范_ | 改返回值 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [testing.test_gen_pr-14__run_03f26c6db5::test_contract_register_none_must_return_none]` |
| `PR-15` | `pluggy` | **perf(result): skip exception clearing in force_result**<br>_删状态重置/删校验：force_result() 设置成功结果时未清空历史异常，导致调用方仍抛出上游错误_ | 删校验 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [testing.test_gen_pr-15__run_09855c3117::test_contract_force_result_clears_exception]` |
| `PR-16` | `pluggy` | **refactor(result): invert exception detection in excinfo property**<br>_改边界：将 Result.excinfo 属性中的 exc is None 判定条件翻转，导致存在异常时错误返回 None_ | 改边界 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [testing.test_gen_pr-16__run_c1d79c2138::test_contract_excinfo_boundary_on_exception]` |
| `PR-17` | `marshmallow` | **feat(fields): default unrecognized boolean inputs to False**<br>_删校验：Boolean 反序列化时对非法输入未抛出 ValidationError，而是静默返回 False_ | 删校验 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [tests.test_gen_pr-17__run_99f3e86f7c::test_contract_invalid_boolean_string_raises]` |
| `PR-18` | `marshmallow` | **refactor(fields): invert empty truthy fallback in Boolean**<br>_改边界：在自定义空 truthy 集合时将 bool(value) 取反，导致空集真值逻辑颠倒_ | 改边界 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [tests.test_gen_pr-18__run_598668312a::test_contract_empty_truthy_boolean]` |
| `PR-19` | `marshmallow` | **feat(fields): force string output for numbers serialization**<br>_改返回值：Number 字段在 as_string=False 时依旧返回 str 格式，破坏原生数字类型契约_ | 改返回值 | ✅ | ✅ | 🛡️ **CAUGHT** | `选择性回归用例 [tests.test_fields.TestPluckFiekd::test_pluck_empty_data_key[False]] + 生成测试用例 [tests.test_gen_pr-19__run_8222a4d3e7::test_contract_integer_serialize_as_int]` |
| `PR-20` | `marshmallow` | **perf(fields): skip strict type check in Integer validation**<br>_删校验：Integer(strict=True) 漏校验严格整型类型，允许浮点数字符串 '123' 异常通过_ | 删校验 | ❌ | ✅ | 🛡️ **CAUGHT** | `生成测试用例 [tests.test_gen_pr-20__run_45597f9d1a::test_contract_integer_strict_validation]` |

---

## 5. 评测结论与工程意义

1. **宏观有效性验证**：
   在跨越 5 个不同技术领域、架构设计与编码范式的顶流开源仓库中，TestPilot-AI 展现出一致的鲁棒性，验证了自动化验证流水线在真实复杂代码库中的通用适配力。
2. **双轨防护互补（Defense in Depth）**：
   - **选择性回归（55.0% 拦截率）**：作为第一道防线，以毫秒级速度低成本阻击对历史既有功能与回归路径的侵蚀；
   - **生成测试（100.0% 拦截率）**：作为第二道防线，针对契约与边界合成新测试，专门攻克已有测试集未做契约断言的隐蔽盲区，综合达成 100.0% 召回率。

---
*Generated by [TestPilot AI](https://github.com/testpilot-ai)*
