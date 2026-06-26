## ADDED Requirements

### Requirement: npc init 默认为每个 run 创建独立 worktree

`npc init`（无 `--no-worktree`）MUST 在主 checkout 当前 HEAD 上创建分支 `spine/<run_ts>` 与对应 git worktree（位于 `~/.spine/worktrees/<canonical_proj_key>/<run_ts>/`），并以 worktree 路径作为 `repo_root` 计算本 run 的 `Paths`（从而 state/active.json/index 全部按 worktree 路径隔离）。

#### Scenario: 默认创建 worktree 并按其路径重键

- **WHEN** 在主 checkout 执行 `npc init`
- **THEN** 新建 worktree 与分支 `spine/<run_ts>`
- **AND** 返回 JSON 含 `worktree_root`、`spine_branch`、`canonical_proj_key`
- **AND** `Paths.repo_root` 等于 worktree 路径，`task_log_dir` 按 worktree 路径派生

#### Scenario: --no-worktree 保留就地行为

- **WHEN** 执行 `npc init --no-worktree`
- **THEN** 不创建 worktree，行为与既有就地 init 一致（repo_root = 主 checkout）

#### Scenario: worktree 创建失败不留半残

- **WHEN** worktree 或分支创建失败（分支已存在指向别处 / 磁盘问题）
- **THEN** init 以环境错误（exit 3）报错，不写入半残的 run.json/active.json

### Requirement: run.json 记录 canonical 回指字段

worktree 模式下 `run.json` MUST 持久化 `canonical_repo_root`、`canonical_proj_key`、`base_branch`、`spine_branch`，供 finalize 合并回 main、telemetry 分组使用。`read_run_json` MUST 能还原这些字段。

#### Scenario: 回指字段往返一致

- **WHEN** worktree 模式 init 写出 run.json 后再 `read_run_json`
- **THEN** 还原出的 `canonical_repo_root`/`canonical_proj_key`/`base_branch`/`spine_branch` 与写入一致

### Requirement: 续跑探测扫描悬空 spine worktree

`npc init` 在创建新 worktree 之前 MUST 扫描既有 `spine/*` worktree（`git worktree list --porcelain`），若某 worktree 的 task_log 存在 in-progress state，则报 `needs_resume=true` 并指向该 `worktree_root`，不创建新 worktree。

#### Scenario: 命中悬空 in-progress worktree 则续跑

- **WHEN** 存在一个 `spine/*` worktree 且其 task_log 有 in-progress state
- **AND** 在主 checkout 执行 `npc init`
- **THEN** 返回 `needs_resume=true` 且 `worktree_root` 指向该悬空 worktree
- **AND** 不创建新的 worktree/分支

#### Scenario: 无悬空 run 则正常新建

- **WHEN** 不存在带 in-progress state 的 `spine/*` worktree
- **THEN** 正常创建新 worktree 并 `needs_resume=false`
