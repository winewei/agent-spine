# agent-spine plugin

本地自主 **harness**，跑在 Claude Code 进程内——无服务、无容器、无常驻进程。把"长时运行 + 自主决策 + 反复 review 打磨"的编排做成纯 markdown skill，确定性动作全部委托给安装后的 `npc` 命令。

## 三层职责

| 层 | 角色 | 由谁承担 |
|---|---|---|
| 智能层 | 调度 + 决策（排计划、spawn、读一行 JSON 分支） | 主 session（`/spine-run`） |
| 执行层 | 写代码 + 详细过程日志 + 一行 RESULT 回报 | `spine-coder` subagent |
| 底座 | 确定性机械动作（状态/事件/模板/子进程/指标） | `npc` CLI |

## 提供的能力

| 命令 / agent / skill | 作用 |
|---|---|
| `/spine-run <目标 或 change名> [--auto]` | 主 harness：init→plan→（每个 change）implement→review→fix→archive→收尾。支持续跑。 |
| `spine-coder`（subagent） | 专职执行体，被 `/spine-run` spawn，产代码 commit + summary.md 详细日志。 |
| `/spine-analyze` | 读跨 run 指标，提 ≤3 条 harness 自迭代建议（只读不改，人审闸门）。 |
| `/new-plan-changes-v2` | 批量推进所有活跃 OpenSpec changes：DAG 拓扑排序后**串行** implement→review→fix→archive，复用 npc 状态/日志/续跑/telemetry。 |
| `new-plan-changes-v3`（skill，自动触发） | v2 的**波次并行**升级版：架构师 sub-agent 切波次，每波在独立 git worktree 内并行 implement，串行 cherry-pick 整合后逐字复用 v2/npc 的 review→fix→archive。用户说"并行推进 changes"/"波次并行实施"等描述时自动触发，也可显式要求。 |

`/spine-run` 面向"给定一个自由目标、全自主拆解并实施"；`new-plan-changes-v2/v3` 面向"OpenSpec 已有一批 active changes，按依赖顺序批量推进"——两条互补的入口，共享同一个 `npc` 底座。

## 前置依赖

- **`npc` 命令**（必需，代码在 `src/npc`）：
  在 agent-spine 仓库根执行 `uv tool install --force --from . npc`
- **`git`**、**`openspec`**（archive + 目标拆解需要）
- **`codex`**（默认 review 引擎；可经 `.npc/config.toml` 切 `claude` 引擎）
- `new-plan-changes-v3` 额外要求 `npc --version` ≥ 1.4.0，且 `worktree.baseRef=head`（`.claude/settings.json` 或 `~/.claude/settings.json`），否则拒绝以 fresh 语义静默降级

## 安装

```text
/plugin marketplace add winewei/agent-spine
/plugin install agent-spine@agent-spine
```

随后在任意 git 工程内：

```text
/spine-run 给认证模块加限流 --auto      # 自由目标 → 自动拆解 → 全自主跑完
/spine-run add-rate-limit               # 已有 openspec change → 交互档跑
/spine-analyze                          # 跑几个 run 后分析、迭代 harness 自身

/new-plan-changes-v2                    # 批量推进所有活跃 openspec changes（串行）
并行推进所有活跃的 changes              # 触发 new-plan-changes-v3（波次并行 worktree）
```

## 两种运行档

- **交互档**（默认）：plan 排好给你确认；review 卡死 / archive 失败时 AskUserQuestion 让你定。
- **auto 档**（`--auto`）：决策点交给 `npc auto-decide` 机械判定，fire-and-forget，只在真正卡死时才停。

## 全轨迹留存

所有 state / event / 每轮 review.json / 每个 summary.md 落 `~/task_log/<PROJ_KEY>/`，跨 run 指标落 `~/task_log/_telemetry/`，工程目录零侵入。`/spine-analyze` 据此持续优化 harness。

详见仓库根 [README](../../README.md) 与 [docs/usage.md](../../docs/usage.md)。
