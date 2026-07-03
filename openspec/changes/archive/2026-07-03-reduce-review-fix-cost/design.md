# reduce-review-fix-cost — Design

## Context

三条建议均源自 2026-07-03 派生指标分析。本 design 记录**人在回路复核对原分析的修正**及其理由——这些修正是本 change 相对原 proposal 文档的核心增量，实施时不得回退。

## D1 — 建议 1 的硬边界：静态通用 checklist，不注入 per-change review focus

**决策**：implement/fix prompt 只注入一份 change 无关的静态类目清单，**绝不**注入 `npc focus` 为当次 change 渲染的 review focus 文本或 reviewer 的具体 findings。

**理由**：核心不变量 1「生成 ⊥ 验证」（`docs/principles.md`）要求 coder（生成）与 review（验证）永不同源。若把 reviewer 用的同源 per-change focus 喂给 coder 自检，reviewer 再用同一份 focus 评审就不再是独立验证——coder 会朝判据"应试"，产出表面满足 checklist 但未必真质量的代码，codex 的信号价值下降（Goodhart's law）。

**边界划法**：
- 允许：稳定的、反复出现的 blocking **类目名**（validation / partial-failure / locking / test-coverage / edge-case / telemetry / concurrency…）作为"完成定义"提醒。类目名与 reviewer 使用的类目 SHOULD 同源命名，但 coder 侧只见通用层级，reviewer 侧见当次具体判据。
- 禁止：当次 change 的 review focus 渲染文本、上一轮 review 的具体 findings 原文、reviewer 的评分 rubric 细则。
- 单一事实源：类目清单集中定义（如 `templates.py` 常量），implement/fix prompt 引用同一份，避免 drift。

## D2 — 首轮通过率不是纯成本；验证指标要区分"低级遗漏轮"与"深审轮"

**决策**：本 change 的目标是砍掉"coder 本可自查却漏掉、被 review 一句话打回"的低级轮次，**不是**把 review 轮数一味压到最低。

**理由**：review-r0 的 24 个 changes-requested 不全是浪费。实测 `parallel-dag-scheduling` 的 blocking 轨迹是 5→2→1→2→1→1→0，每轮 codex 挖出**不同的真 bug**（glob 路径重叠、`applyRequires` 方括号正则、evicted state 写传播…）——这是"渐进深审"的收益，不是首轮该消灭的成本。把首轮通过率当唯一北极星会牺牲审深。

**验证方式**：checklist 上线后，看 `review-r0` 的 approve 占比是否上升、且**总 review_rounds 下降幅度主要来自原本 1–2 轮就该收敛的 change**；对 6+ 轮的深审 change，轮数不应被人为压缩（若压缩了，反而要警惕漏审）。

## D3 — 建议 2 的信号选择：跨领域广度 > 纯 LOC/文件数；软门不强拆

**决策**：复杂度门的**主信号**是跨领域广度（touched 路径覆盖的顶层模块/目录数，或 spec requirement 条目数），文件数/LOC 仅作辅助。超阈值只**告警**，给两条出路——建议拆分 **或** 标记 `large` 并抬高 max-rounds 预算。绝不自动拆分、绝不阻断。

**理由**：纯体量是糙代理。`parallel-dag-scheduling` 自己就是 11 文件的大 change，却 6 轮干净收敛——因为它虽大但**单一主题**。真正推高轮数的是"一个 change 同时背 7 类 blocking 维度"的**跨领域**分散，review 每轮只能收敛一两类，于是轮数线性堆高（对照 `wire-incentive-queue-consumers` 11 轮）。因此信号要测"领域广度"而非"行数"，否则误伤大而自洽的 change。

**为何软门**：自动拆分需要理解语义，确定性层做不了且易出错；plan 阶段人（或 auto-decide）看到告警自行决定拆或提预算，更稳。

## D4 — `large` 预算覆盖的落点

**决策**：`large` 标记落进 change 的 plan-state；review-fix 循环上限（`spine-run.md` 3b 默认 20，及 npc 侧任何软上限）对 `large` change 读取一个可配的更高预算（如 `[review].max_rounds_large`）。非 large 行为不变。

## D5 — 建议 3 否掉删除：保留 + 备注 + 排除复推

**决策**：`verify-tests-rerun` 笼子**保留**，不因零触发删除。在 `_CAGE_DEFS` 对应条目加 `retained: true`（或等价标注）+ 理由备注；`deletion_candidates` 计算排除 retained 笼子。

**理由**：该笼子守的是真实故障类——coder 声称 tests pass、rerun 打脸。它的成本≈0（只在真失败时才 fire），11 run 零触发恰说明上游健康，**不是**冗余证据。删它是拿近乎免费的保险换一点整洁度，风险/收益不划算。加"排除复推"是因为纯统计的 deletion_candidates 会每次分析都重新推荐删——需要一个持久的"已裁决保留"记号止损。

**边界**：`retained` 标注是人在回路裁决的产物，不是自动判定；其余 12 个 `no_data` 笼子（事件未接线）**仍不可**当删除候选，与本决策无关。

## Open Questions

- checklist 的**具体类目定版**：是否就用 review focus 现有的类目集，还是另立一份精简版？（倾向同名子集，D1 单一事实源）
- 复杂度门的**阈值定值**：跨领域广度阈值、辅助文件数阈值取多少？需用 27 个历史 change 回测校准（≤3 轮的 22 个不应触发，10+ 轮的 2 个应触发）。
- `max_rounds_large` **取值**：20 → 多少？还是不设硬上限只提预算？
- `retained` 标注的**存储位置**：`_CAGE_DEFS` 内联 vs 独立配置？倾向内联（就近备注理由）。
