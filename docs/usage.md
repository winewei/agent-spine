# 推荐用法：CLI + plugin + CLAUDE.md 三层配置

`npc` 单独可用，但它作为**自主 harness 底座**的价值要靠 **CLI + plugin + CLAUDE.md 三层一起配** 才能兑现。本文给出可直接照做的完整步骤。

---

## 层 1：装 `npc` 命令（机器级，所有 Claude Code session 共享）

`npc` 是从当前 agent-spine 仓库安装出来的 CLI 命令，代码在 `src/npc`。直接从仓库根安装：

```bash
uv tool install --force --from . npc              # 从当前仓库根安装 src/npc 为 npc 命令
npc --version          # 应输出 npc 1.4.0
```

首次在某工程内 `npc init` 时会自举 `~/task_log/.new-plan-review-schema.json` 与 `~/.local/bin/portable-timeout`。

外部依赖：`git`（必需）、`openspec`（archive + 目标拆解）、`codex`（默认 review 引擎）、`jq`（推荐）。

---

## 层 2：装 harness plugin（用户级，所有 project 共享）

```text
# 在 Claude Code 中：
/plugin marketplace add winewei/agent-spine
/plugin install agent-spine@agent-spine
```

装完得到五个能力：`/spine-run`、`/spine-analyze`、`/new-plan-changes-v2`（串行推进全部 active changes）、`new-plan-changes-v3`（skill，波次并行版）、`spine-coder`（subagent）。

> CLI 与 plugin 版本应保持一致；升级 CLI（在仓库根重跑 `uv tool install --force --from . npc`）后建议同步 `/plugin update agent-spine@agent-spine`。

---

## 层 3：CLAUDE.md 片段（项目级，让主 session 知道何时该用 harness）

把下面这段粘到目标工程的 `CLAUDE.md`（或 `~/.claude/CLAUDE.md` 全局）：

```markdown
## 自主 harness（agent-spine）

当用户要"实现一批 openspec change"、"把某目标自主跑完"、"长时无人值守地 plan→implement→review→archive"时，
用 `/spine-run`，不要手工逐步操作：

- `/spine-run <目标>` —— 自由目标，harness 自动拆解成 change 再跑（交互档）
- `/spine-run <change名…>` —— 已有 openspec change，直接跑
- `/spine-run <…> --auto` —— 全自主档，fire-and-forget

规则：
- 主 session 只调度与决策；实现/修复一律 spawn `spine-coder` subagent。
- 确定性动作（状态/事件/模板/review/archive）一律走 `npc` 子命令，看一行 JSON 做分支。
- 不在 context 里搬运 prompt 模板 / review.json / summary.md 原文。
- 跑过几个 run 后，用 `/spine-analyze` 读跨 run 指标迭代 harness 自身。
```

---

## 端到端：第一次跑

```text
# 在一个带 openspec/ 的 git 工程内
/spine-run 给认证模块加请求限流和审计日志 --auto
```

harness 会：

1. `npc init` 落 run.json + active.json，检测是否需续跑。
2. 把目标拆成若干 openspec change（如 `add-rate-limit`、`add-audit-log`），排 plan_order。
3. 逐个 change：spawn `spine-coder` 实现 → `npc review run` 多轮 codex review → 有 blocking 就 spawn coder 修 → 干净后 `npc archive run`。
4. 决策点（review 卡死 / archive 失败）：auto 档由 `npc auto-decide` 判定，交互档问你。
5. 收尾：`finalize` + `summary render` + `index append`，汇报结果与轨迹路径。

全程轨迹在 `~/task_log/<PROJ_KEY>/`，跨 run 指标在 `~/task_log/_telemetry/`。

---

## 续跑

中断后再次 `/spine-run`（同工程）会自动检测 `needs_resume` 并从断点（next_seq / next_phase）接着跑，不会重复已 archived 的 change。

## 切 review 引擎到 claude（或自定义后端）

见仓库根 [README — Review 引擎配置](../README.md#review-引擎配置)。常见：用 `.npc/config.toml` 把 `engine` 切到 `claude`，`bin` + `extra_args` 路由到经 `--settings` 配置的 qwen / deepseek 后端。

## Meta-loop 定时化（v1.5，P8）

design.md §11.6 第二阶段的落地方式：**定时自动跑 `/spine-analyze`，人闸不动（只产提案、不改代码）**。它把"人记得去跑分析"这个易失步骤自动化——telemetry 指标一直在积累，但没人看等于没有。

两种接法（选其一）：

```text
# A. Claude Code 内（推荐）：让 Claude 用 CronCreate 建每周任务
"每周一上午 9 点跑一次 /spine-analyze，把建议落到 docs/optimization-proposals/"

# B. 系统 cron / CI 定时任务（headless）
0 9 * * 1  cd /path/to/your-project && claude -p "/spine-analyze" --permission-mode acceptEdits
```

约束（与 /spine-analyze 的 guardrails 一致）：

- 每次只读派生指标（`npc telemetry hotspots` / `agg`，< 5KB），不读 events.ndjson 原文。
- 产出落 `docs/optimization-proposals/YYYY-MM-DD.md`，**最多 3 条**、每条引用具体指标数值。
- **绝不自动实施**——实施须经人审阅点头。这是自迭代的人在回路闸门，定时化只自动"触发分析"，不自动"改 harness"。
- 指标样本不足（events_considered < 10）时如实写"数据不足"，跳过本期。

## Steering：长 run 中途转向（v1.5，P4）

run 跑着的时候想调整方向，不必打断：

```bash
npc state note --text "wave 3 之前先把 auth 相关的两个 change 提到前面" --source user
```

主循环（v4 skill）在每个波次边界跑 `npc status --brief` 消费未读 note，按指令调整剩余计划后 `npc state note --consume` 打水位。你也可以随时用 `npc status --brief` 看盘面：`pending_decisions` 是等你裁定的分叉点，`next_action` 是下一步动作提示。
