# fix-prompt-exhaustive-sweep — Tasks

## 0. 落点清单的确定性枚举

以下命令在 REPO_ROOT 下逐条执行，用于确定本 change 需要触碰的调用点/文件集合（覆盖率判据 = 下表能否被逐项勾完，而非"觉得全了"）：

```
$ grep -rn "render_fixer(" src/npc/
src/npc/coder.py:293:        text = templates.render_fixer(
src/npc/agent.py:239:        text = templates.render_fixer(
src/npc/templates.py:179:def render_fixer(
```
→ 3 处匹配：1 处定义（`templates.py`）+ 2 处独立调用点（`coder.py`、`agent.py`，分别对应 headless / in-session dispatch 路径）。**两处调用点均需接入新的连续计数/复现计算，且必须共享同一份纯函数（design D2），不得各自实现**。

```
$ grep -rn '"categories":' src/npc/pipeline.py
src/npc/pipeline.py:412:            "categories": metrics.get("categories"),
src/npc/pipeline.py:462:            "categories": metrics.get("categories"),
src/npc/pipeline.py:944:        "categories": metrics["categories"],
```
→ 3 处匹配：确认 `entry["phases"]["review-rN"]["categories"]` 的落盘位置（412/462 行为 `_do_review_phase_exit_and_trend`），是连续计数与复现判定唯一允许读取的数据源。

```
$ grep -n '": frozenset(' src/npc/telemetry.py | wc -l
14
```
→ `EMIT_FIELD_CONTRACT` 现有 14 个已登记 kind；新增的 category_recurrence_after_sweep_claim telemetry kind 必须在此追加第 15 条登记，否则 `tests/test_structural_invariants.py` 的 AST 扫描断言会 fail。

```
$ grep -rn "_verify_categories_scanned\|_collect_categories_scanned" src/npc/ tests/
src/npc/spec_report.py:200:def _collect_categories_scanned(entry: dict) -> tuple[set[str], bool]:
src/npc/spec_report.py:219:def _verify_categories_scanned(entry: dict) -> dict:
src/npc/spec_report.py:222:    scanned, any_present = _collect_categories_scanned(entry)
src/npc/spec_report.py:330:    categories_verification = _verify_categories_scanned(entry)
tests/test_spec_report.py:197:def test_verify_categories_scanned_ok_when_covers_seen():
tests/test_spec_report.py:204:    out = _sr._verify_categories_scanned(entry)
tests/test_spec_report.py:219:def test_verify_categories_scanned_unverifiable_when_no_self_report():
tests/test_spec_report.py:228:def test_verify_categories_scanned_ok_when_nothing_seen():
```
→ `unsubstantiated` verdict 落点唯一：`_verify_categories_scanned`（220-244 行）+ 汇总函数 `_aggregate_self_report_verdict`（247 行起）；既有 4 条测试需保持通过（不误伤既有 ok/warn/unverifiable 断言），并新增 unsubstantiated 相关测试。

```
$ grep -n "max_rounds_large\|max_rounds:" src/npc/config.py | wc -l
6
```
→ 确认 `[review].max_rounds_large` / `[spec_review].max_rounds` 的字段定义+校验+TOML 解析三段式共 6 处引用，新增 `[coder].category_streak_threshold` 需照此三段式模式落地（`CoderConfig` 字段 + `__post_init__` 校验 + `_parse` 中的 TOML 读取）。

## 1. 配置面：`[coder].category_streak_threshold`

- [x] 1.1 `src/npc/config.py`：`CoderConfig` 新增 `category_streak_threshold: int = 2` 字段；`__post_init__` 校验 `isinstance(..., int) and >= 1`，非法值抛 `ConfigError`，报错文案格式对齐 `[review].max_rounds_large`
- [x] 1.2 `src/npc/config.py` 的 TOML 解析函数新增 `coder_raw.get("category_streak_threshold", 2)` 读取与类型校验（非整数抛 `ConfigError`）
- [x] 1.3 测试：默认值 2、显式配置非默认值、非法值（非整数/< 1）分别的解析/报错行为

## 2. 连续计数 + 复现判定的共享纯函数

- [x] 2.1 `src/npc/trend.py` 新增纯函数（输入 `entry["phases"]`，输出 `category_streaks: dict[str, int]`）：对每个在最近一轮 review 中出现的 category，逐轮向前追溯连续出现轮数，缺席即停止（OQ2 逐轮不中断、缺席清零）；MUST 只读 `entry["phases"]`，MUST NOT 打开任何 `round-*.review.json`
- [x] 2.2 `src/npc/trend.py` 新增纯函数，输出 `recurred_categories: list[dict]`（含 `category`/`claimed_at_round`/`recurred_at_round`，取满足条件的最小 M）：遍历各 `fix-rN` 的自报 `categories_scanned`，检查是否有更晚轮次 `review-rM`（M ≥ N，即时序上晚于 `fix-rN` 的 review；`review-r(N-1)` 是触发该自报的原因，MUST NOT 被计入证据）的 `categories` 再次包含该 category；命中即记为一次复现。该函数 MUST NOT 落盘，纯粹是 `entry["phases"]` → `list[dict]` 的映射，供 pipeline/coder/agent/spec_report 各消费点统一调用
- [x] 2.3 测试：连续 2/3 轮触发、中断清零、跨轮复现命中、`review-r(N-1)`（触发轮）本身不被误判为复现证据、自报缺失不产生复现判定、无历史轮次时两函数均返回空结构

## 3. review phase-exit：现场重算差集触发 telemetry（不落盘复现证据）

- [x] 3.1 `src/npc/pipeline.py::_do_review_phase_exit_and_trend` 的 mutate 闭包内，在写入本轮 `review-rN` 的 `categories` 前后各调用一次 2.2 的纯函数（`recurred_before` / `recurred_after`），两者的差集即"本轮新增的复现条目"；MUST NOT 新增任何 state 字段（不落盘 `entry["category_recurrence_evidence"]` 或等价字段，与 D1/OQ4 保持一致，见 design D5）
- [x] 3.2 `src/npc/telemetry.py`：`EMIT_FIELD_CONTRACT` 新增一个 kind（如 `coder.category_recurrence_after_sweep_claim`）的字段契约登记；`pipeline.py` 在 3.1 算出的差集非空时调用 `telemetry.emit_event(...)` 发出该 kind 的事件（含 change/category/声明轮/再现轮等字段），差集为空则不发事件
- [x] 3.3 测试：本轮新增复现条目触发 telemetry 事件（字段契约断言通过 `tests/test_structural_invariants.py` 既有 AST 扫描机制）；差集为空时不发事件；已在更早轮次记为复现过的条目不重复发事件（差集去重）；确认 `entry["phases"]`/`state.json` 不出现任何本 change 新增的字段

## 4. `render_fixer` 渲染逻辑

- [x] 4.1 `src/npc/templates.py::render_fixer` 新增可选形参 `category_streaks: dict[str, int] | None = None` 与 `recurred_categories: list[str] | None = None`（或等价合并结构）；计算 `escalated = {c: n for c,n in streaks if n >= threshold} ∪ recurred`，非空时对应 category 段落追加强制穷举格式（三态：已覆盖/新增覆盖/确认不可达+理由）文案，空集合时渲染与本 change 之前逐字等价
- [x] 4.2 `src/npc/coder.py::_render_prompt_file`（fix 分支）接入 2.1/2.2 共享纯函数 + 1.1 阈值配置，传参给 `render_fixer`
- [x] 4.3 `src/npc/agent.py::prompt_render`（fix 分支）同步接入，与 4.2 使用同一份共享纯函数（design D2，禁止独立实现）
- [x] 4.4 测试：未达阈值时 prompt 与快照逐字等价；达阈值时段落含强制穷举格式提示；unsubstantiated category 即使未达阈值也强制穷举；`coder.py`/`agent.py` 两条路径对同一 state 输入产出一致的 escalated 集合

## 5. `spec_report.py` 新增 `unsubstantiated` verdict

- [x] 5.1 `src/npc/spec_report.py::_verify_categories_scanned` 直接调用 2.2 的共享纯函数，对该 change 完整 `entry["phases"]` 现场重算 `recurred_categories`（不读取任何持久化字段），非空时该核验条目 verdict 强制为 `unsubstantiated`（优先级高于 warn），报告中呈现存在复现信号的 category 列表及声明/复现轮次
- [x] 5.2 `src/npc/spec_report.py::_aggregate_self_report_verdict` 汇总优先级新增 `unsubstantiated` 档，置于 `warn` 之前（`unsubstantiated > warn > unverifiable > ok`）
- [x] 5.3 `spec-report.json`/`spec-report.md` 渲染逻辑呈现 `unsubstantiated` verdict 与证据字段，遵守既有 `MD_LINE_LIMIT` 行数上限约束
- [x] 5.4 测试：既有 4 条 `_verify_categories_scanned` 测试（`test_spec_report.py:197/219/228` 起）继续通过；新增复现场景测试覆盖 unsubstantiated 判定与汇总优先级

## 6. 验证

- [x] 6.1 `openspec validate fix-prompt-exhaustive-sweep --type change --strict` 通过
- [x] 6.2 全量测试通过（`uv run pytest -q`），新增测试覆盖上述各点，且不破坏既有 `render_fixer`/`spec_report`/`telemetry` 相关既有测试
