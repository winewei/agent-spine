## Why

现有报告有三层——每 phase 原始 `*.summary.md`（coder 自报流水账，碎且长）、每 run 聚合 `run-summary.md`（一个 run 里所有 change 混在一起）、跨 run `telemetry`/`/spine-analyze`。**中间缺一格**：没有「每交付一个 spec（change）就出一份、从『工作 agent 表现』视角滚动汇总」的单-change 收尾回执。人想在单个 spec 落地后一眼看懂「负责它的 agent 靠不靠谱」（几轮收敛、返工在哪、一次过还是磨了半天、花了多少钱），只能去翻碎日志或读 run 级大杂烩。

原料已齐（`state.progress[*]` 的 phases/commit/categories/blocking_trend + `_telemetry` 的 token/cost 事件 + git + coder 自报 summary.md），这是一个**纯确定性派生渲染**，不新增采集，风险低。

## What Changes

- 新增 `npc spec-report render --seq <N>`：对单个 **archived** change 派生**三产物**（一次渲染，同源派生对象）：
  - **`spec-report.md`**（人读视图）：**简要、重点**，固定指标标题段 + 叙事，有可测行数上限，不含 phase 原始流水账。
  - **`spec-report.json`**（审计/机器契约源）：**目标与实用优先、不求美观**，字段齐全供 `/spine-analyze` 与交叉核验消费；md 是它的渲染视图。
  - 一条 `kind=spec.report` 的 telemetry 事件：仅含 `common_metrics` 子集 + pointer（`report_json` 路径、change_seq/change_id/run_ts/proj_key/status），**不复制全量报告**，贴合既有 telemetry「派生指标 + pointer」模式。
  - 三视图的 `common_metrics`（终态 / review 轮数 / fix 轮数 / blocking_trend / total_duration_ms / estimated_tokens 合计 / 自报核验汇总）取值 MUST 一致；md/telemetry 可只呈现子集但不得矛盾。
- 内容维度：交付结果（commit chain / 终态）、收敛质量（review 轮数 + blocking_trend + `one_shot`）、返工画像（category 分布 + 每类返工轮数）、耗时（各 phase + 总时长）、资源（`estimated_tokens_by_backend`，沿用 `cost` heuristic 口径、标注估算，**不含货币费用**）、**自报核验（C）**、叙事一句话。
- **自报核验（C，确定性小节，不调 LLM、不新增 agent）**：`regressions_added` 以 fix 轮 commit range 判定 diff 是否触及测试文件（通用启发式）；`categories_scanned` 与 `categories_seen` 对照。一致 ✓、不一致 ⚠、缺数据 unverifiable（不误报）。
- **自动触发**：archived 成功后由 spine-run 主循环以**非阻塞 wrapper** 调用（与收尾 `npc summary render` 同构，粒度到单 change）；即使返回 `ok:false` 也不回滚 archive。非 archived 终态（failed/skipped-auto/needs-user-decision）不生成。
- **命令契约边界**：非法输入（seq 不存在 / 非 archived / state 损坏）返回 `ok:false` + 稳定 error code、不产半成品；仅**产物落盘/telemetry 写入失败**才是 best-effort 吞错。两类分开。
- **不做**（YAGNI）：跨 spec/跨 run 相对基线对比；重量级 C——再 spawn agent 审 coder（inline 核验已由 `focus.py` 注入下轮 review focus 承担）；货币费用估算（无价格表源，留待后续）；非 archived 终态报告。

## Capabilities

### New Capabilities

- `spec-report`: 每交付一个 change 派生 per-spec agent 报告（md 人读视图 + json 审计契约源 + telemetry 事件）的契约。

### Modified Capabilities

## Impact

- `src/npc/` 新增 `spec_report.py`（派生渲染 + 三产物落盘）
- `src/npc/cli.py`（注册 `spec-report render` 子命令）
- `src/npc/telemetry.py`（如需新增 `spec.report` kind 常量/字段）
- `plugins/agent-spine/commands/spine-run.md`（archive 成功后新增一步调用）
- `docs/cli.md`（`spec-report` 子命令契约）
- `tests/`（三产物落盘、字段齐全、C 核验 ✓/⚠、容错不阻塞）
