# agent-spine plugin

本地自主 **harness**，跑在 Claude Code 进程内——无服务、无容器、无常驻进程。把"长时运行 + 自主决策 + 反复 review 打磨"的编排做成纯 markdown plugin commands，确定性动作全部委托给 [`npc` CLI](https://github.com/winewei/agent-spine)。

## 三层职责

| 层 | 角色 | 由谁承担 |
|---|---|---|
| 智能层 | 调度 + 决策（排计划、spawn、读一行 JSON 分支） | 主 session（`/spine-run`） |
| 执行层 | 写代码 + 详细过程日志 + 一行 RESULT 回报 | `spine-coder` subagent |
| 底座 | 确定性机械动作（状态/事件/模板/子进程/指标） | `npc` CLI |

## 提供的能力

| 命令 / agent | 作用 |
|---|---|
| `/spine-run <目标 或 change名> [--auto]` | 主 harness：init→plan→（每个 change）implement→review→fix→archive→收尾。支持续跑。 |
| `spine-coder`（subagent） | 专职执行体，被 `/spine-run` spawn，产代码 commit + summary.md 详细日志。 |
| `/spine-spec "<目标>"` | 独立的 spec 生成入口：拆目标→撰写 openspec change→强制独立语义评审→固定轮次上限 fix 循环。不接管 `/spine-run` Step 2B；产出可被 `/spine-run <change名>` 直接消费。 |
| `spine-spec-writer`（subagent） | 专职 spec 生成执行体，被 `/spine-spec` spawn，只写 `openspec/changes/<id>/` 下的 artifact，不 commit。 |
| `/spine-analyze` | 读跨 run 指标，提 ≤3 条 harness 自迭代建议（只读不改，人审闸门）。 |

## 前置依赖

- **`npc` CLI**（必需，内置 `src/npc`）：
  在 agent-spine 仓库根 `uv tool install --force --from . npc`
- **`git`**、**`openspec`**（archive + 目标拆解需要）
- **`codex`**（默认 review 引擎；可经 `.npc/config.toml` 切 `claude` 引擎）

## 安装

```text
/plugin marketplace add winewei/agent-spine
/plugin install agent-spine@agent-spine
```

随后在任意 git 工程内：

```text
/spine-run 给认证模块加限流 --auto      # 自由目标 → 自动拆解 → 全自主跑完
/spine-run add-rate-limit               # 已有 openspec change → 交互档跑
/spine-spec "给认证模块加限流"           # 只产 spec：撰写 + 强制语义评审，产出喂给 /spine-run
/spine-analyze                          # 跑几个 run 后分析、迭代 harness 自身
```

## 两种运行档

- **交互档**（默认）：plan 排好给你确认；review 卡死 / archive 失败时 AskUserQuestion 让你定。
- **auto 档**（`--auto`）：决策点交给 `npc auto-decide` 机械判定，fire-and-forget，只在真正卡死时才停。

## 全轨迹留存

所有 state / event / 每轮 review.json / 每个 summary.md 落 `~/task_log/<PROJ_KEY>/`，跨 run 指标落 `~/task_log/_telemetry/`，工程目录零侵入。`/spine-analyze` 据此持续优化 harness。

详见仓库根 [README](../../README.md) 与 [docs/usage.md](../../docs/usage.md)。
