## ADDED Requirements

### Requirement: npc clean 清理孤儿 spine worktree

`npc clean` MUST 在清理陈旧 task_log run 的同时，移除悬空的 `spine/*` worktree 及其分支；但 MUST NOT 移除仍有 in-progress state 的 worktree（可续跑的 run）。

#### Scenario: 孤儿 worktree 被清

- **WHEN** 存在一个 `spine/*` worktree，其 task_log 无 in-progress state（已陈旧）
- **AND** 执行 `npc clean`
- **THEN** 该 worktree 被 `git worktree remove`，对应分支被删

#### Scenario: in-progress worktree 保留

- **WHEN** 某 `spine/*` worktree 的 task_log 有 in-progress state
- **THEN** `npc clean` 不移除它，也不删其分支
