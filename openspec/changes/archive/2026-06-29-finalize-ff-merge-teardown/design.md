## Context

archive commit 落在 worktree 的 `spine/<run_ts>` 分支。finalize 当前只判定顶层 status。worktree 模式需要在 finalize 成功时把分支 ff-only 合回 base 并拆树；失败/未干净则保留。所有动作必须从 **canonical checkout** 执行（worktree 不能移除自己所在的分支检出）。

## Goals / Non-Goals

**Goals:**
- ff-only 合并回 base_branch，以"干净"为硬闸。
- 仅成功时拆 worktree + 删分支；否则保留并结构化报告。
- 与 `deliver.py`「npc 不自作主张」一致：ff-only 是确定性机械动作且有硬闸，可放进恒跑的 finalize。

**Non-Goals:**
- push / 开 PR（明确不做）。
- 非 ff 的真合并 / rebase（分叉一律交人）。
- 清理任意悬空 worktree（那是 `clean-worktree-aware`）。

## Decisions

- **执行位置**：合并/拆树从 `canonical_repo_root`（run.json）执行，不在 worktree 内。
- **闸门**：`status==completed` 且 `git merge --ff-only spine/<run_ts>`（在 base_branch 上）退出 0 → 成功路径；否则保留路径。
- **拆树顺序**：先 `git worktree remove <worktree_root>`（必要时 `--force` 仅在确认无未提交改动时），再 `git branch -d spine/<run_ts>`（用 `-d` 安全删，已合并才删得掉，双保险）。
- **返回契约**：finalize JSON 增 `merged_back`(bool)、`worktree_removed`(bool)、`spine_branch`、`base_branch`、可选 `reason`。编排者据此在 Step 4 报告。
- **幂等/容错**：worktree 已被手动删 / 分支已不存在 → 视为已收尾，不报错。

## Risks / Trade-offs

- **base 在 run 期间前进**：长 run 期间 base_branch 可能被推进，导致无法 ff → 落到保留路径（预期行为，交人处理），不丢 commit（仍在 spine 分支）。
- **worktree 内有未提交残留**：正常 run 收尾时 worktree 应已干净（coder 都 commit 了）；`worktree remove` 若因脏树失败，保留并报告，不强删。
- **与 finalize 现有 exit 1 语义**：needs-user-decision 仍走原 exit 1 路径，合并逻辑只在 completed 分支触发，不影响既有行为。
