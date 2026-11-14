# 🤖 TestPilot AI — 大模型现场 0-to-1 对抗测试生成实测报告
*(无完备测试脚本 / 原有测试全绿漏网极端场景)*

## 📌 核心评测背景与工业假定

> **在真实工业软件研发中，绝大多数生产事故并非发生于“已有测试报错但依然强行上线”，而是发生于：**
> **“开发者修改了代码，本地已有的测试套件全部通过（全绿），CI 门禁全绿放行；然而因既有测试缺乏对该边界分支的覆盖，暗藏缺陷的代码被盲目合入，最终在生产环境直接崩溃。”**
> 
> 本评测针对这一最关键的真实世界痛点，采掘 GitHub 历史真实事故 PR，实测 **TestPilot-AI 的大模型（LLM）能否在“原有测试完全全绿失灵”的前提下，现场 0-to-1 编写出精准击穿该缺陷的全新对抗用例**！

---

## 1. 现场实测量化矩阵

| 案例 ID | 开源仓库 | 真实事故场景 | 原项目已有测试网表现 (Existing Tests) | TestPilot-AI 大模型现场生成 (0-to-1 AI Test) | 终审判定 |
| :---: | :--- | :--- | :---: | :---: | :---: |
| `RPR-097` | `cachetools` | **Fix #430: Honor falsey getsizeof callables (#431)** | 🟢 全部通过 (23 passed, 0 failed) | 🔴 击中失败 (1 failed, 0 passed) | **🛡️ 成功无中生有阻断事故** |
| `RPR-071` | `marshmallow` | **URL validator scheme parsing without maxsplit trap** | 🟢 全部通过 (221 passed, 0 failed) | ⚪ 通过 (5 passed) | ⚪ 未击中 |

---

## 2. 经典案例深度剖析 (现场实录)

### 案例实测: `cachetools` - `RPR-097` (Falsy 陷阱: `if getsizeof:` 误判)

1. **事故现场复原**:
   - 开发者在 `Cache.__init__` 中将 `if getsizeof is not None:` 写成了 `if getsizeof:`。
   - 历史仓库自带的 `tests/test_cache.py` 包含 23 个测试用例，执行结果：**23 passed, 0 failed (100% 全绿)**！原作者误以为代码无比安全，合入主干。
2. **大模型现场 0-to-1 生成的测试代码**:
```python
import pytest

from cachetools import Cache


def test_falsey_getsizeof_callable_honored():
    class FalseyCallable:
        def __call__(self, value):
            return 42

        def __len__(self):
            return 0

    custom_callable = FalseyCallable()

    cache = Cache(maxsize=100, getsizeof=custom_callable)

    assert cache.getsizeof is custom_callable

    cache["key"] = "value"
    assert cache.getsizeof("value") == 42
    assert cache.currsize == 42
```
3. **物理沙箱执行结果**:
   - **状态**: `failed` (1 failure)
   - **错误堆栈**: `assert <function Cache.getsizeof at 0x000001AB3F5C5B20> is <tests.test_live_ai_generated__run_76075d75dd.test_falsey_getsizeof_callable_honored.<locals>.FalseyCallable object at 0x000001AB3F5E37D0>
 +  where <function Cache.getsizeof at 0x000001AB3F5C5B20> = Cache({}, maxsize=100, currsize=0).getsiz...`
   - **实证结论**: 大模型现场识别出了 `__len__ == 0` / `__bool__ == False` 的 Falsy 陷阱，生成的测试代码在物理沙箱中运行**当场爆出 AssertionError**，在既有测试全绿的真空地带，**成功凭借纯 AI 生成的全新用例将事故死死拦住！**

---

## 3. 架构结论与核心价值

- **彻底打破对“已有测试是否覆盖全”的依赖**：即使被测项目是零测试、弱测试或粗放测试，TestPilot-AI 依靠 **Diff 语义分析 -> LLM 针对性合成 -> 物理沙箱断言** 的完整闭环，依然能够无中生有地建立安全网；
- **真正的生产止损价值**：将测试生成的重心从“无脑提高覆盖率指标”转变为“**专门攻击既有测试未能覆盖的边缘契约漏洞**”。

---
*Report generated live by [TestPilot AI](https://github.com/testpilot-ai)*
