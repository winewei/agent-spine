# 设计：spine-run 的「每 run 一棵 worktree」隔离

**日期**：2026-06-23
**状态**：已批准（待写实现计划）
**关联不变量**：不变量 3（确定性笼子 ∝ 1/(人在回路)）——本设计是被真实方差点位「打」出来的硬轨，不是预先过度设计。

---

## 问题

`npc init` 把一切都按 `repo_root = cwd 的 git toplevel` 来键。两个 `/spine-run`
调用跑在同一个 checkout 时，硬冲突两次：

1. **工作树冲突**：两个 coder 在同一工作树并发 `git add` / `git commit`，会互相
   把对方写了一半的文件提交进 commit——必冲。
2. **active.json 指针被劫**：`~/task_log/<proj_key>/active.json` 是 per-proj_key
   单例指针。第二个 run 的 `npc init`（尤其 `--fresh`）会把指针从旧 run 切到自己，
   旧 run 的 coder 跑完后 `npc record` 会记到错的 run 上。

现场（2026-06-23 17:58 旧 run 与 18:24 新 run）已暴露这两点：`git worktree list`
只有主树一个，没有隔离。

## 核心思路

每个 `/spine-run` 调用拿一棵自己的 git worktree，跑在自己的分支上。因为 npc 的
state 全部按 `git toplevel` 来键，只要把编排者放进 worktree，隔离就**自动成立**：

- 不同 worktree 路径 → 不同 `proj_key` → 不同 `task_log_dir`、`active.json`、
  `index.jsonl`、coder cwd、commit。
- 没有任何共享可变指针在并发 run 之间存活。

**关键事实（已验证）**：`git rev-parse --show-toplevel` 在 linked worktree 内返回
worktree 路径本身。因此 `detect_repo_root` / `compute_paths` **零改动**——只要 cwd
进了 worktree，它们就自然派生出 worktree 的 proj_key。

## 已定的四个分叉

| 分叉 | 决定 |
|---|---|
| 隔离粒度 | 每个 `/spine-run` 调用一棵 worktree（不是 per-change）；在 `npc init` 时创建 |
| 合并回 main | 干净则自动 `--ff-only`；分叉/有冲突则停下报告分支给人 |
| 跑完清理 | 合成功则自动拆 worktree + 删分支（task_log 记账保留）；失败/需人介入则保留树+分支 |
| state 身份 | 按 worktree 路径重新键（零逻辑改动）；telemetry 补 `canonical_proj_key` 字段供分组 |

## 组件

### 1. `npc init` — 创建 worktree（新职责）

- 探测 canonical repo_root（主 checkout）+ `canonical_proj_key`；生成 `run_ts`。
- 在当前 HEAD 上建分支 `spine/<run_ts>`，worktree 落
  `~/.spine/worktrees/<canonical_proj_key>/<run_ts>/`。
- 用 `repo_root = worktree 路径` 计算 `Paths` → state 自动键到 worktree。
- `run.json` 增加回指字段：`canonical_repo_root`、`canonical_proj_key`、
  `base_branch`、`spine_branch`。
- init JSON 输出新增 `worktree_root`、`spine_branch`、`canonical_proj_key`。
- `--no-worktree` 逃生口：保留旧的就地行为（调试 / 单 run 受信场景）。

### 2. 编排者（`spine-run.md`）— 在 worktree 内操作

- init 后读 `worktree_root`；后续每个 `npc …` 与 coder 一律以
  `cwd = worktree_root` 运行。cwd→repo_root→active.json 的 resolve 让每个子命令
  在自己的 worktree 内自定位。共享的 canonical `active.json` 永不被触碰 → 劫持
  bug 结构性消失。

### 3. 续跑探测 — 扫描悬空 worktree

失败 run 的 state 落在 **worktree 的** proj_key 下，从主 checkout 看不见。所以
`npc init`（在建新树之前）跑 `git worktree list --porcelain`，过滤 `spine/*`，
检查每棵 worktree 的 task_log 有无 in-progress state；命中 → `needs_resume=true`
并指向那棵 `worktree_root`，编排者 cd 进去续跑，不建新树。保住「续跑优先」硬轨。

### 4. `npc finalize` — fast-forward + 拆树（机械、以"干净"为闸）

finalize 顶层 status 为 `completed` 时：从 canonical checkout 尝试
`git merge --ff-only spine/<run_ts>` 到 `base_branch`。

- **FF 干净** → `git worktree remove` + `git branch -d spine/<run_ts>`；task_log
  记账保留。
- **FF 不可能**（main 已分叉）或 `completed-with-issues`/needs-decision →
  **停，不做破坏性动作**：保留 worktree + 分支，把分支名 + commit 列表报给人。

> 与 `deliver.py`「npc 不自作主张」一致：ff-only 是本地机械动作且以「干净」为硬闸，
> 放进恒跑的 finalize 合理；push / 开 PR 不在范围内（明确只做本地 ff-only）。

### 5. telemetry 分组

telemetry 记录新增 `canonical_proj_key` 字段（取自 run.json）。全局
`events.ndjson` 即便存储键是 per-worktree，仍能按逻辑工程分组。

### 6. `npc clean` — 感知 worktree

扩展为：在清 task_log 目录的同时，`git worktree remove` + prune 悬空的 spine
worktree 与分支。

## worktree 位置与命名

- `~/.spine/worktrees/<canonical_proj_key>/<run_ts>/`——在仓库外（避免嵌套仓库
  混淆），确定性，按工程分组。
- 分支 `spine/<run_ts>`。
- base ref：init 时主 checkout 的当前 HEAD。

## 本设计顺带暴露的一个 bug

`make_run_ts` 是**分钟粒度**。同一分钟内两个 init → 相同 `run_ts` → 相同 worktree
路径/分支 → 冲突。worktree 模式下给 `run_ts` 加一个短唯一后缀（秒 + pid，如
`2026-06-23-1758-23a1`），并发 run 永不撞。

## 错误处理

- worktree 创建失败（分支已存在且指向别处 / 磁盘满）→ init exit 3，不留半残状态。
- ff-only 失败 → 不做任何破坏性动作，保留分支+树，清晰报告。
- 并发 init → 各自拿到不同 run_ts（靠上面的唯一后缀）→ 不同 worktree。

## 测试

- **单元**：worktree 路径/分支计算；`run.json` 新字段；ff-only 决策（干净 vs 分叉）；
  `git worktree list` 续跑扫描解析；run_ts 并发唯一性。
- **集成**：临时仓库真实走 create→commit→ff-merge→remove 全程；两个并发 init 落到
  不同 worktree；失败 run 保留 worktree 且能从主 checkout 续跑。

## YAGNI 砍掉的

- 不自动开 PR（已定只做 ff-only）。
- 不 per-change worktree（已定 per-run）。
- 不 push 远端。
