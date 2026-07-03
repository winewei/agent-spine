# reduce-review-fix-cost

## Why

2026-07-03 的自迭代分析（`docs/optimization-proposals/2026-07-03.md`，输入仅派生指标 `npc telemetry hotspots/agg/cages`，197 事件 / 27 change / 11 run）显示：hotspots 前 5 名**全是 review 轮次**（review-r0…r4），失败率均为 0——瓶颈不是"跑挂"，而是"review 反复打回 + fix 重跑"的墙钟与 token。两个实测事实：

- **首轮通过率 ≈11%**：`review-r0` count=27，approve 仅 3、changes-requested 24。几乎每个 change 都被强制拉进至少一轮 fix。
- **长尾方差极端**：`wire-incentive-queue-consumers`（11 轮 / 21 blocking / ≈63k token）与 `m1-eval-protocol`（10 轮 / ≈45k token）两个 change 合计 ≈108k est token，占 30d 全量显著比例；而 27 个里 22 个 ≤3 轮就收敛。少数超大 change 吞掉绝大部分 fix/review 成本。

本 change 把该分析经**人在回路复核后修正**的三条裁决固化。修正相对原分析的关键点：① 建议 1 收窄到"静态通用 checklist"，**不注入当次 review focus 原文**，守住核心不变量"生成 ⊥ 验证"（见 design D1）；② 建议 2 换信号，用"跨领域广度"而非纯 LOC，且"告警不强拆"（见 design D3）；③ 建议 3 **否掉删除**，改为"保留 + 备注 + 排除复推"（见 design D5）。

**实施闸门**：本 change 只描述、不实施；落地需人在回路显式点头（沿 design.md §11.6 自迭代闸门）。

## What Changes

- **implement/fix prompt 注入静态通用自检 checklist**：coder（`spine-coder` subagent）在 implement/fix 阶段目前对 reviewer 的判据零可见（已核实 `src/npc/agent.py` 不引用 `focus`）。新增：渲染 prompt 时注入一份**change 无关、静态**的提交前自检类目清单（validation / partial-failure / locking / test-coverage / edge-case / telemetry / concurrency 等反复出现的 blocking 维度），要求 coder 提交前逐条自查。**硬边界：绝不注入当次 change 的 review focus 渲染文本或 reviewer 的具体 findings**——否则 coder"应试"、reviewer 丧失独立性（Goodhart），违反不变量 1。
- **plan 前置软性复杂度门**：`npc plan` 前置门目前不对 change 体量/跨领域范围设限。新增一个**软性告警**：主信号为跨领域广度（touched 路径覆盖的顶层模块数 / spec requirement 条目数），文件数为辅；超阈值 → 输出 warning + 建议（拆分 **或** 标记 `large` 抬高 max-rounds 预算）。**绝不自动拆分、绝不阻断 run**。
- **verify-tests-rerun 笼子保留裁决**：该笼子（`src/npc/telemetry.py`，映射 `phase.exit` + `outcome_reason=="rerun-tests-failed"`）90d/11run 零触发被列入 `deletion_candidates`。裁决为**保留**（守"coder 谎报 tests pass、rerun 打脸"的真实故障类，成本≈0，删它是拿近乎免费的保险换整洁度）。在 cage 定义处备注理由，并让 `deletion_candidates` 计算**排除标注为 retained 的笼子**，避免每次分析反复推荐删除。

## Capabilities

### New Capabilities

- `implement-selfcheck-rubric`：implement/fix prompt 注入静态通用自检 checklist，严守"生成 ⊥ 验证"边界（不注入 per-change review focus）。
- `plan-complexity-gate`：plan 前置软性复杂度告警——跨领域信号、告警不阻断、`large` 标记抬高 max-rounds 预算。
- `cage-retention-policy`：笼子保留裁决与 `deletion_candidates` 排除机制，防止低触发守卫被反复推荐删除。

### Modified Capabilities

<!-- 无已建立的 spec capability 需要 delta；本 change 三项均为新增契约。 -->

## Impact

- `src/npc/agent.py`（implement/fix prompt 注入静态 checklist）
- `src/npc/templates.py` 或新常量模块（checklist 类目的单一事实源）
- `src/npc/plan.py`（前置复杂度门 + `large` 标记输出）
- `src/npc/config.py` / `src/npc/state.py`（`[review]` 复杂度阈值与 `large` 的 max-rounds 预算覆盖）
- `src/npc/telemetry.py`（cage 保留备注 + `deletion_candidates` 排除 retained 笼子）
- `plugins/agent-spine/commands/spine-run.md`（review-fix 循环上限对 `large` change 读预算覆盖；plan 告警的编排侧呈现）
