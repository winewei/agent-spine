# archive-error-contract Specification

## Purpose
TBD - created by archiving change archive-structured-errors. Update Purpose after archive.
## Requirements
### Requirement: archive 所有失败路径输出单行结构化 JSON

`npc archive run` 的任何失败（含 `git add`、`_git_head`、commit 等子进程失败，以及归档命令未产生真实归档副作用的场景）MUST 捕获并转为 stdout 单行 JSON（`ok=false` + `error` 分类 + 摘要），exit code 1；MUST NOT 向 stdout 泄漏裸 traceback 或空输出。归档命令返回成功退出码但未产生真实归档副作用时，MUST 视为失败，MUST NOT 仅凭退出码判定归档已完成。

#### Scenario: git add 因 index.lock 失败仍返回单行 JSON

- **WHEN** archive 阶段 `git add openspec/` 因 index.lock（或权限、hook）非零退出
- **THEN** stdout 是单行合法 JSON：`ok=false`、`error="git-add-failed"`、含 stderr 摘要，进程 exit 1
- **AND** 主 session `jq -r '.ok'` 读到 `false`（非空串），可转 3d 决策点

#### Scenario: _git_head 失败结构化报错

- **WHEN** `_git_head` 的 `git rev-parse` 非零退出
- **THEN** stdout 单行 JSON `ok=false`、`error="git-head-failed"`，无裸 traceback

#### Scenario: 正常路径不受影响

- **WHEN** archive 各 git 步骤全部成功
- **THEN** 返回原契约字段（`ok=true`、`archive_commit` 等），行为不变

#### Scenario: 归档命令退出码为成功但未产生真实归档副作用（静默 abort）

- **WHEN** 归档命令以成功退出码（exit 0）返回，但归档目标 change 目录仍存在于原路径、且归档存档目录下不存在对应该 change 的新目录（即归档动作实际未发生，归档命令仅在其标准输出中提示已中止）
- **THEN** archive 阶段 MUST NOT 继续执行后续的 git 提交步骤
- **AND** stdout 是单行合法 JSON：`ok=false`、`error="openspec-archive-aborted"`、含归档命令标准输出的摘要，进程 exit 1
- **AND** 该失败分类与"归档命令本身以非零退出码失败"的既有分类相互独立、不混用同一 `error` 值
- **AND** 该核验只在归档命令退出码为 0 时触发；若归档命令退出码非 0，MUST 沿用既有分类 `error="openspec-archive-failed"`（不触发本 Scenario 描述的核验，也 MUST NOT 输出 `error="openspec-archive-aborted"`），无论此时归档副作用实际是否发生

