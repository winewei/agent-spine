# spine-run §publish — `npc integrate --prepared` 回执处理

按需读取：发布回执的 `status` 不是 `archived` 时读本节，按对应分支处理。

- npc 把启动目标分支的新提交合到 change worktree，按补丁身份（`git patch-id --verbatim`，不含 `[integrate].derived` 声明的派生文件）校验与已审查补丁一致，并在组合树复跑测试；目标分支在此期间可供其他发布使用。当前候选已验证时复用测试回执。仅派生文件冲突时 npc 取目标分支版本并执行 `[integrate].regenerate` 重新生成，不进入 needs-resolution。
- 组合测试后短暂抢目标锁，复核启动分支身份、基线 HEAD 和干净状态，快进发布并 archive。保存原 implement/fix 提交链，无需 hash 翻译。worktree 保留供核查，不自动删除。
- `archived` → PENDING 移除，DONE/FINISHED 加入并续闸。正常路径只有此处满足下游代码依赖。
- `target-busy` / `target-moved` 表示**启动目标**忙或已推进；保存 PENDING，稍后重试 publication，不重跑 implement。`archive-failed` 同样保留发布回执，解决原因后重试归档。
- `needs-review` → 补丁已改变，转 ACTIVE/INNER，在现有 worktree 执行 `change run --isolated --handoff --from review`。npc 记录最近一次 clean 审查的补丁，复审只针对两版补丁的集成增量（`round-N.integration-delta.diff`），已审查且未变化的代码不再作为 blocking 范围。
- `needs-resolution`（`reason` 为 merge-conflict / merge-failed / regenerate-failed / regenerate-touched-sources / regenerate-incomplete / hook-modified-worktree / merge-would-overwrite-ignored，`conflicts` / `files` 列出涉及文件；`abort_incomplete` 表示合并中止后 worktree 未恢复干净，须先核对并整理；hook-modified-worktree 表示合并提交已生成、hook 改写的文件未提交，须核对后提交）/ `tests-failed` → agent 读取必要诊断，在原 worktree 合入最新目标提交、解决冲突或测试回归并提交，然后按上述路径做增量复审；禁止丢弃原实现或改在目标工作区裸写。同一派生文件（锁文件、生成代码）反复冲突时，在工程 `.npc/config.toml` 声明 `[integrate]`。
- `needs-recovery` → 核对未装订提交和回执，优先恢复已有工作。`needs-decision` → 主 agent 按证据选择修复、调整执行体、拆分任务或解释阻塞（交互档在 blocking 连续两轮未严格下降，或 blocking 轮数每累计 3 轮时进入该决策点，用于识别技术路线层面的不收敛）；不得仅因轮数上限把 critical/high 问题当可接受并归档。
- `aborted` → 停止调度并保留所有产物。未知错误先诊断，不猜成成功或终态失败。准备/发布命令可重试；不要绕过锁或使用 force。
