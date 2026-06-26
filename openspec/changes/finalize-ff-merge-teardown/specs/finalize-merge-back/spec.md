## ADDED Requirements

### Requirement: finalize 干净则 ff-only 合并回并拆树

worktree 模式下，`npc finalize` 在顶层 status 为 `completed` 时 MUST 尝试 `git merge --ff-only` 把 `spine/<run_ts>` 合回 `base_branch`（取自 run.json）。仅当 fast-forward 成功，才 MAY 执行破坏性收尾：移除 worktree、删除 `spine/<run_ts>` 分支。

#### Scenario: 干净 run fast-forward 合回并拆树

- **WHEN** run status=`completed` 且 `base_branch` 能 fast-forward 到 `spine/<run_ts>`
- **THEN** 合并成功，移除 worktree，删除 spine 分支
- **AND** finalize 返回 `merged_back=true`、`worktree_removed=true`
- **AND** task_log 记账保留（不删）

#### Scenario: base 已分叉则保留并报告

- **WHEN** run status=`completed` 但 `base_branch` 无法 fast-forward（已分叉/有新提交）
- **THEN** 不执行任何破坏性动作（不合、不拆、不删分支）
- **AND** finalize 返回 `merged_back=false` + `spine_branch` + 原因，供编排者报人

#### Scenario: 非 completed 状态保留全部

- **WHEN** run status 为 `completed-with-issues` 或含 needs-user-decision
- **THEN** 不尝试合并、不拆树，worktree 与分支原样保留

#### Scenario: --no-worktree run 跳过合并/拆树

- **WHEN** run 以 `--no-worktree` 模式跑（run.json 无 spine_branch）
- **THEN** finalize 不触发任何 worktree 合并/拆除逻辑
