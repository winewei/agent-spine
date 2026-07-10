# fix-coder-cwd-desync

## Why

`/spine-run`/`/spine-spec` 用 Agent 工具 spawn 的 `spine-coder`/`spine-spec-writer` subagent **不继承编排者的 shell cwd**：Claude Code 会在 `cd` 到非授信目录时把 shell cwd 静默重置回主 checkout（primary working directory），Agent 子代理的初始 cwd 同样锚定主 checkout。而 npc 渲染的 prompt 只在 Runtime Variables 里给出 `REPO_ROOT=<worktree>`，**没有任何一条指令要求锚定该目录**——「必读输入」「git commit」全部用相对路径。主 checkout 里有一模一样的文件、还有 worktree 没有的 `node_modules`，coder 在错误目录里工作毫无违和感。

已两次实证（2026-07-10 add-email-sending-identities、add-email-template-editor）：coder 的实现 commit 落在 main 而非 run worktree，npc 的 worktree 状态机（review diff、rerun-tests、archive）全部与现实脱节。record 现有的 commit 校验用 `git cat-file -e`，而 **linked worktree 与主 checkout 共享对象库**，落在 main 的 commit 在 worktree 里也「存在」，校验穿透。

## What Changes

三层防御，任何一层失守都有下一层兜底：

1. **Prompt 层（根修）**：`templates.py` 新增共享常量 `CWD_CONTRACT_MD`（工作目录契约），在全部 5 个 agent prompt 模板（implementer / fixer / spec-interrogator / spec-writer / spec-fixer）的 Runtime Variables 段之后渲染：初始 cwd 视为不可信；每个 Bash 调用显式锚定 `REPO_ROOT`（`cd "$REPO_ROOT" && …` 或 `git -C "$REPO_ROOT"`）；动手前与 commit 前用 `git rev-parse --show-toplevel` 断言；Read/Write/Edit 一律用 `REPO_ROOT` 前缀的绝对路径；断言失败立即按失败 RESULT 汇报 `notes=cwd-mismatch`，不改任何文件。
2. **Record 层（确定性门）**：`pipeline.py` 的 implement record 与 fix record 在 `git cat-file -e` 之后追加 `git merge-base --is-ancestor <commit> HEAD`（cwd=run worktree）。commit 不在本 run 分支上 → 新错误码 `commit-not-on-run-branch`（附 hint：cwd 漂移，commit 可能落在其他 checkout），phase 记 failed，绝不放行进 review。
3. **Worktree 可用性（消除漂移诱因）**：`config.py` 新增 `[worktree].provision_cmd`（默认空）。非空时 `npc init` 在 worktree 创建成功后于其中执行该命令（如 `pnpm install --frozen-lockfile --prefer-offline`），失败仅 warn 不阻塞 init（结果记入 init JSON 的 `provision` 字段）。没有 node_modules 的 worktree 是 coder 漂移回主 checkout 的现实诱因，也让 record 的 rerun-tests 硬轨在 worktree 里根本跑不起来。

同步：`plugins/agent-spine/agents/spine-coder.md` 与 `spine-spec-writer.md` 的 agent 系统提示加同一契约摘要（模板改动之外的第二道口径）；`docs/cli.md` 记录新错误码与新 config 键。

## Non-Goals

- 不改 Agent 工具的 spawn cwd 行为（harness 侧不可控）。
- 不做 worktree 与主 checkout 的自动 commit 迁移/搬运——检测到漂移即 fail，由编排者按 auto-decide/人工处置。
- 不给 `--no-worktree` 模式加额外行为（ancestor 门在该模式同样成立：commit 必须在当前 HEAD 链上）。

## Impact

| 面 | 影响 |
|---|---|
| `src/npc/templates.py` | 新常量 + 5 个 render 函数注入契约段 |
| `src/npc/pipeline.py` | implement/fix record 各 +1 道 ancestor 门与错误码 |
| `src/npc/init_cmd.py` / `config.py` | `[worktree].provision_cmd` + init 执行与 JSON 字段 |
| `plugins/agent-spine/agents/*.md` | 系统提示补契约摘要 |
| `docs/cli.md` | 错误码 / config 键文档 |
| 兼容性 | RESULT 契约不变；无 config 时行为除新增门外与现状一致；`TEMPLATE_VERSION` bump |
