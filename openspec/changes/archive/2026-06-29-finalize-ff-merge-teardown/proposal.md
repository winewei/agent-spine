## Why

worktree-per-run 后，archive 的 commit 落在 `spine/<run_ts>` 分支（worktree 内）。设计定：run 干净跑完时，finalize 把分支 **fast-forward only** 合回 `base_branch`，成功则拆 worktree + 删分支；分叉/未干净则**保留**树与分支、报告给人。需要一个机械、以"干净"为硬闸的合并回 + 拆树步骤。

## What Changes

- `npc finalize` 在顶层 status 判定为 `completed` 时，从 canonical checkout 尝试 `git merge --ff-only spine/<run_ts>` 到 `base_branch`（取自 run.json）。
  - **FF 干净** → `git worktree remove <worktree_root>` + `git branch -d spine/<run_ts>`；task_log 记账保留。返回 `merged_back=true`、`worktree_removed=true`。
  - **FF 不可能**（base 已分叉）或 status 非 `completed`（completed-with-issues / needs-decision）→ **不做任何破坏性动作**：保留 worktree + 分支，返回 `merged_back=false` + 分支名 + 原因。
- 仅本地 ff-only，不 push、不开 PR（设计明确范围）。
- `--no-worktree` run 不触发合并/拆树逻辑（无 spine 分支）。

## Capabilities

### New Capabilities
- `finalize-merge-back`: finalize 的 ff-only 合并回 + worktree 拆除契约（以干净为硬闸，破坏性动作仅在成功时发生）。

### Modified Capabilities

## Impact

- `src/npc/state.py`（finalize handler）或 `src/npc/deliver.py`：ff-merge + teardown。
- `src/npc/git_ops.py`：`merge_ff_only`、`worktree_remove`、`branch_delete` 封装（可注入 runner）。
- `tests/`：FF 干净路径（合+拆）、分叉路径（保留+报告）、非 completed 路径（保留）、--no-worktree（跳过）。
