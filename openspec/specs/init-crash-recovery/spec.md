# init-crash-recovery Specification

## Purpose
TBD - created by archiving change init-crash-worktree-recovery. Update Purpose after archive.
## Requirements
### Requirement: worktree 创建必须有先行意向落盘且孤儿可回收

`npc init` MUST 在创建 worktree 前落盘可被续跑扫描发现的意向记录（plan-state 骨架，status=initializing）。后续 init 扫描发现「worktree 完好但无 plan 进度」的中间态时 MUST 复用该 worktree 而非新建；`npc clean` MUST 能列出并安全回收孤儿 worktree（含删除对应 spine 分支，保留 task_log 账目）。

#### Scenario: init 与 init-run 之间崩溃后可复用

- **WHEN** `npc init` 建好 worktree 后、`npc state init-run` 前进程崩溃，随后再次运行 `npc init`
- **THEN** 扫描通过意向记录发现该 worktree，返回其 worktree_root 供复用，不创建第二棵 worktree

#### Scenario: clean 回收孤儿 worktree

- **WHEN** 存在 initializing 态记录对应的 worktree 且它不属于任何 active run，用户运行 `npc clean`
- **THEN** clean 列出该孤儿并可执行回收：`git worktree remove` + 删除 spine 分支，task_log 记录保留

#### Scenario: 正常 run 不受影响

- **WHEN** init→init-run→主循环→finalize 正常完成
- **THEN** 骨架被 init-run 正常升级，clean 不把完成的 run 误判为孤儿

