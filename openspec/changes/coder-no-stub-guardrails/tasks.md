## 1. spine-coder agent 契约 Guardrails（先做，最少依赖）

- [x] 1.1 在 `plugins/agent-spine/agents/spine-coder.md` 的 `## Guardrails` 段新增一条：禁止以 stub / 占位实现（空函数体、恒定返回值、未覆盖核心逻辑的简化分支）充数勾选 task 完成
- [x] 1.2 同段新增一条：禁止删除、注释掉或 skip 任何既有测试，也禁止以放宽断言范围、移除关键覆盖点、跳过关键分支等方式弱化既有测试以换取 `tests=pass`
- [x] 1.3 通读现有 Guardrails 段落语气与格式（列表项、加粗关键词），保持新增两条与既有条目风格一致

## 2. SELFCHECK_CATEGORIES / SELFCHECK_RUBRIC_MD（TDD）

- [x] 2.1 写测试（RED）：`src/npc/templates.py` 的 `SELFCHECK_CATEGORIES` 包含 `"no-stub"`
- [x] 2.2 写测试（RED）：`SELFCHECK_RUBRIC_MD` 表格中 `no-stub` 一行的自查要点同时提及"占位实现"（或等价措辞）与"测试被删除/弱化"（或等价措辞）
- [x] 2.3 在 `SELFCHECK_CATEGORIES` 元组追加 `"no-stub"`（保持既有 7 项顺序不变，新增项放末尾）
- [x] 2.4 在 `SELFCHECK_RUBRIC_MD` 表格追加 `no-stub` 行，措辞为**通用类目层级提醒**，不引用 reviewer 侧「多段注释自我辩护」这一具体启发式（守不变量 1）
- [x] 2.5 跑既有测试 `tests/test_reduce_review_fix_cost.py` 中 `TestSelfcheckRubric` 全部用例，确认未因新增类目而回归（这些断言基于集合成员/文本包含，理论上不受影响，仍需实测确认）
- [x] 2.6 跑 2.1–2.2 确认 GREEN

## 3. review focus 共享判据（TDD）

- [x] 3.1 写测试（RED）：`src/npc/focus.py` 的 `_output_requirements_block()` 返回文本中包含 stub / 占位实现为 blocking 的判据说明
- [x] 3.2 写测试（RED）：同一返回文本中包含"测试被删除、注释、skip 或断言被弱化"为 blocking 的判据说明
- [x] 3.3 写测试（RED）：同一返回文本中包含"需要多段注释自我辩护的实现视为可疑信号"的提示
- [x] 3.4 写测试（RED）：分别调用 `_round_0_template(...)` 与 `_round_n_template(...)`（构造最小可行参数），断言两者渲染结果都包含 3.1–3.3 断言的判据文本，且逐字相同（用同一段标记字符串核对，参照既有 `SPEC_ATTRIBUTION_ENUM_SEMANTICS` 的验证方式）
- [x] 3.5 在 `src/npc/focus.py` 新增一个类似 `SPEC_ATTRIBUTION_ENUM_SEMANTICS` 的模块级字符串常量（承载 stub / 删测 / 可疑注释三条判据文案），并在 `_output_requirements_block()` 中引用，确保 Round 0 / Round N 同源
- [x] 3.6 跑 3.1–3.4 确认 GREEN

## 4. 守不变量 1 的负向防护（TDD）

- [x] 4.1 写负向测试（RED）：implement 或 fix prompt（`render_implementer` / `render_fixer` 渲染结果）中**不包含** focus.py 新增判据常量里"需要多段注释自我辩护"这一具体措辞
- [x] 4.2 写负向测试（RED）：`SELFCHECK_RUBRIC_MD` 的 `no-stub` 行文本与 focus.py 新增判据常量的具体措辞**不逐字相同**（类目名可同源，细则不共享；参照既有 `implement-selfcheck-rubric` 能力的负向测试写法）
- [x] 4.3 跑 4.1–4.2 确认 GREEN

## 5. 非目标守护

- [x] 5.1 grep 确认本 change 未修改 `src/npc/schema.py` 中 `REVIEW_SCHEMA` 的字段结构或新增 `category` 枚举
- [x] 5.2 grep 确认本 change 未修改 `blocking` 的判定逻辑（`severity`/`in_scope` 的组合规则不变）
- [x] 5.3 确认 `SELFCHECK_CATEGORIES` 中既有 7 个类目（`validation`/`partial-failure`/`locking`/`test-coverage`/`edge-case`/`telemetry`/`concurrency`）的文本与顺序未被改动（仅追加）

## 6. 收尾

- [x] 6.1 跑全量 `uv run pytest -q`，确认无回归
- [x] 6.2 用真实参数调用一次 `npc agent`（implement 与 fix 两种 prompt 渲染路径）与 `npc focus render`（round 0 与 round N），人工核对渲染出的完整文本，确认新增内容排版正常、无截断
