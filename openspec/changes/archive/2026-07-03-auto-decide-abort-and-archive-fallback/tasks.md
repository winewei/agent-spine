## 1. abort 可达（auto_decide.py）

- [x] 1.1 `_decide` 前置系统性阻塞检测：读全量 progress，同一 trigger 连续 ≥3 次 或 skipped-auto 占比 ≥50%（且 ≥3 个）→ `action=abort`，reason=systemic-failure
- [x] 1.2 阈值为模块常量（与既有 B_THRESHOLD_* 同风格），决策仍是纯确定性函数
- [x] 1.3 abort 决策 `--apply` 时置顶层标记（供 finalize 判 aborted 语义）

## 2. force-archive 二次失败兜底（skill）

- [x] 2.1 spine-run.md 3d：force-archive 执行 `npc archive run` 后检查 `.ok`，失败 → `npc auto-decide --trigger archive-failed` 二次决策
- [x] 2.2 明确二次决策只在 skip/abort 中收敛（不再 force-archive 死循环）
- [x] 2.3 spine-run.md 定义 abort 执行语义：余下 change 标记 skipped → 直接 Step 4 finalize，worktree/分支保留

## 3. 测试

- [x] 3.1 同一 trigger 连续 3 次 → abort
- [x] 3.2 skip 比例超阈值 → abort
- [x] 3.3 未达阈值 → 原有 skip/retry 语义回归不变
- [x] 3.4 archive-failed 二次触发收敛到 skip（状态为终态，finalize 不再 incomplete）
- [x] 3.5 `pytest` 全绿
