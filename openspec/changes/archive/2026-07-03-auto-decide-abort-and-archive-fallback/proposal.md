## Why

审计 B10+B11（中）：(1) skill 声称 auto-decide 的 action∈{continue-retry, skip, force-archive, abort}（plugins/agent-spine/commands/spine-run.md:185-187），但 `_decide`（src/npc/auto_decide.py:45-122）**永不返回 abort**——遇系统性阻塞（如环境坏了导致每个 change 都失败）只会一路 skip 到底，没有「及时止损整体退出」；(2) force-archive 只 `npc archive run` 一次（spine-run.md:187），再失败则 change 状态停在非终态 → `npc state finalize` `emit_error("incomplete")`（src/npc/state.py:543-548），而 3d 对「force-archive 又失败」无二次决策 → run 潜在悬挂。不变量 3：auto 档去掉人，决策空间必须闭合、不得有悬挂路径。

## What Changes

- `_decide` 增加可达的 abort 路径：跨 change 维度检测系统性阻塞——同一 trigger 连续出现 N 次（默认 3）或 progress 中 skipped-auto 比例超阈值（默认 ≥50% 且 ≥3 个）时返回 `action=abort`（reason=systemic-failure），阈值为模块内常量。
- `spine-run.md` 3d：force-archive 后的 `npc archive run` 再失败 → 触发二次决策（`npc auto-decide --trigger archive-failed`，依赖 fix-auto-decide-trigger-contract 引入的分支；二次后仅在 skip/abort 中收敛），消除 finalize 悬挂。
- `spine-run.md` 明确 abort 的执行语义：标记剩余 change 为 skipped、直接进 Step 4 finalize（保留 worktree/分支供人查）。
- 补测试：连续同 trigger→abort、skip 比例→abort、force-archive 二次失败→skip 收敛、abort 后 finalize 可完成。

## Capabilities

### New Capabilities

- `auto-decide-loss-cutting`: auto 档的止损闭环——可达的 abort 决策 + force-archive 二次失败兜底，保证 run 无悬挂路径。

### Modified Capabilities

## Impact

- `src/npc/auto_decide.py`（跨 change 系统性阻塞检测 → abort）
- `plugins/agent-spine/commands/spine-run.md`（3d force-archive 失败二次决策 + abort 执行语义）
- `tests/`（abort 可达性与收敛用例）
