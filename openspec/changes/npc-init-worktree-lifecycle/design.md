## Context

`detect_repo_root` 跑 `git rev-parse --show-toplevel`，在 linked worktree 内返回 worktree 路径本身（已验证）。因此一旦 cwd 进入 worktree，`compute_paths` 自然派生 worktree 的 proj_key——隔离零逻辑改动。难点只在 `npc init` 本身跑在主 checkout：它必须创建 worktree、用 worktree 路径（而非自身 cwd）算 Paths、并把 worktree 路径回报给编排者。

## Goals / Non-Goals

**Goals:**
- init 默认建 worktree + `spine/<run_ts>` 分支，按 worktree 路径重键 state。
- run.json 持久化 canonical 回指（供 finalize/telemetry）。
- 续跑扫描悬空 spine worktree，保住「续跑优先」。
- `--no-worktree` 逃生口。

**Non-Goals:**
- finalize 的 ff-merge + 拆树（change `finalize-ff-merge-teardown`）。
- telemetry canonical 字段、clean 感知（各自独立 change）。
- per-change worktree（设计已砍，只 per-run）。

## Decisions

- **worktree 位置**：`~/.spine/worktrees/<canonical_proj_key>/<run_ts>/`，仓库外，确定性，按工程分组。
- **分支**：`spine/<run_ts>`，base = 主 checkout 当前 HEAD；记 `base_branch`（当前分支名）入 run.json。
- **Paths 计算**：init 探测 canonical repo_root 与 canonical_proj_key 后，创建 worktree，再 `compute_paths(repo_root=worktree_path)`；schema/portable-timeout/session 等其余自举不变。
- **run.json 扩展**：新增 4 字段，`to_run_json_dict`/`read_run_json` 同步；非 worktree 模式这些字段为 null 或省略（`read_run_json` 容忍缺失，保持对旧 run.json 兼容）。
- **续跑扫描**：解析 `git worktree list --porcelain` 取每个 worktree 路径，过滤其分支为 `spine/*`，对每个候选按其路径推 task_log_dir 并查 in-progress；命中即返回该 worktree_root。多个命中取最新（按 state mtime）。
- **git 封装**：`git worktree add -b spine/<run_ts> <path> <HEAD>`、`git worktree list --porcelain` 收敛到 git_ops（可注入 runner，便于单测）。

## Risks / Trade-offs

- **state/index 按 worktree 路径碎片化**：每 run 一个 task_log 目录——这是隔离的代价，跨工程聚合由 telemetry 的 `canonical_proj_key` 字段（独立 change）补回。
- **run.json 兼容**：必须容忍旧 run.json 无新字段（reader 用 `.get` 容错）。
- **孤儿 worktree**：失败 run 的 worktree 不在本 change 清理（由 finalize/clean 负责）；本 change 只保证续跑扫描能再找到它。
- **`~/.spine` 路径**：跨平台 home 解析复用 `Path.home()`；目录按需 mkdir。
