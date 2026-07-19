# agent-spine

[English](README.md) | **简体中文**

人驾驭的自主工程 harness，跑在任意 agent CLI 宿主进程内（Claude Code / Kimi CLI / Qwen Code / Codex / OpenCode / …），从 spec 一路推进到代码交付。

agent-spine 把一次自主编码 run 拆成两层，层间以严格契约通信：**智能层**是宿主中立的 playbook，负责调度与语义判断；**确定性执行层**是 `npc` CLI，负责一切机械动作。主 session 只读一行 JSON 做决策——不搬运模板、不解析日志、不手工维护状态。

## 核心能力

- **spec 到交付的自主闭环** — 给 harness 一批 OpenSpec change 或一句话目标，它自动完成 plan → implement → review → fix → archive。交互档在决策分叉点停下问人；`--auto` 档全程无人值守，例行决策下沉给 `npc auto-decide`。
- **波次并行批量执行** — `new-plan-changes-v4` 按依赖关系把 active changes 切成 DAG 波次，每个 change 在独立 git worktree 内并行实施，再串行整合（`npc integrate` / `npc change run`）。
- **独立 review 闸门** — 每个 change 经过 premium 引擎（`codex exec` 或 `claude -p`，可插拔）驱动的 review→fix 循环，带 blocking 趋势追踪与 stale 检测。廉价执行后端在结构上被禁止给自己的产出盖章（`npc verify routing` 强制拦截）。
- **coder 多模型路由** — provider 注册表把 implement / fix 路由到任意 Anthropic 兼容端点（Kimi / Qwen / DeepSeek / …）或 `codex exec`。凭据与模型全局定义一次，每个工程只声明用哪个，可按阶段细分。
- **确定性执行层** — 状态、事件、prompt 模板、review 解析、archive、git 机械动作各是一条 `npc` 子命令：stdout 一行 JSON + 文档化 exit code 契约（`0` 成功 / `1` 业务失败 / `2` 用法错 / `3` 环境错 / `4` 依赖缺失）。
- **上下文经济性** — sub-agent 完整 prompt 渲染到磁盘，主 session 只传 ~150 tokens 薄引导语（spawn 环节约省 93% token）。
- **宿主中立** — `npc` 是唯一分发物。playbook 随包发行，经 `npc playbook install` 物化到任意宿主（Claude Code、Codex CLI 或任意目录）。每份 playbook 顶部带宿主适配表，把 Claude Code 专有机制映射为通用回退。
- **状态外置、可续跑** — 全部运行状态落在 `~/task_log/`，对目标仓库零侵入。跨 session 续跑（`npc resume detect`）、git/state 漂移自愈（`npc state repair`）、跨 run 指标沉淀（`npc telemetry hotspots`）。

## 工作原理

```
┌─ 智能层（playbook，在你的 agent CLI 内执行）───────────────────────┐
│  spine-run            单目标 / 单 change 完整闭环                  │
│  new-plan-changes-v4  批量：DAG 波次 + worktree 并行   ← 推荐入口  │
│  spine-analyze        跨 run 指标复盘，harness 自迭代              │
│  spine-coder          coder sub-agent 定义 / persona               │
└──────────────────────────────┬─────────────────────────────────────┘
                               │  一行 JSON + exit code
┌─ 确定性执行层 ─────────────────────────────────────────────────────┐
│  npc CLI：init / state / phase / review run / fix record /         │
│  archive run / integrate / change run / playbook / telemetry / ... │
└────────────────────────────────────────────────────────────────────┘
```

`npc` 可以单独使用（CI 或普通终端手工调试），但推荐形态是 playbook + npc 配合：批量推进走 `new-plan-changes-v4`，单目标自主闭环走 `spine-run`（`new-plan-changes-v2`/`v3` 为历史演进版本，保留备查）。

## 快速开始

```bash
git clone https://github.com/winewei/agent-spine.git
cd agent-spine

# 1) 装 npc 命令（唯一分发物）
uv tool install --force --from . npc
npc --version          # npc 1.7.0

# 2) 把 playbooks 物化到你的宿主 CLI（三选一）
npc playbook install --host claude    # Claude Code：commands/skills/agents 目录
npc playbook install --host codex     # Codex CLI：~/.codex/prompts/
npc playbook install --dest <DIR>     # 其它宿主：平铺到任意目录，自行挂载
```

然后在一个带 `openspec/` 目录的 git 工程内：

```text
/new-plan-changes-v4                    # 批量：波次并行推进全部 active changes
/spine-run 给认证模块加请求限流 --auto    # 单目标，fire-and-forget
```

完整三层配置（CLI + playbooks + 项目上下文片段）见 [docs/usage.md](docs/usage.md)。

### 系统依赖

- Python ≥ 3.11、`git`（必需）
- `codex` CLI — 默认 review 引擎，可经配置切到 `claude`
- `openspec` CLI — 仅 `npc archive run` 需要
- 推荐 `jq`；`portable-timeout` 首次 `npc init` 时自动自举

## 支持的宿主

Claude Code、Kimi CLI、Qwen Code、Codex CLI、OpenCode——其它 agent CLI 经 `npc playbook install --dest` 挂载后同样可用。宿主探测（`[host]` 配置或 `CLAUDECODE` env）在 Claude Code 上启用完整能力，其它宿主自动降级（session 识别走 by-cwd 索引、跳过自动授权）。项目上下文读 `CLAUDE.md` 时一律带 `AGENTS.md` fallback，非 Claude 宿主只维护 `AGENTS.md` 即可。

## 命令面

面向 LLM 的命令面刻意收敛——高层 pipeline 命令把完整一步打包成一次调用：

| 命令 | 一次调用完成 |
|---|---|
| `npc init --auto` / `npc resume detect` | 初始化或续跑一个 run（`~/task_log/` 下落 `run.json` + `active.json`） |
| `npc implement record` / `npc fix record` | 校验 coder 的 RESULT 行，装订 phase 计时与状态 |
| `npc review run --seq N --round M` | focus 渲染 → review 引擎执行（含重试）→ parse → trend → stale 判定 |
| `npc archive run --seq N` | precheck → `openspec validate` → `openspec archive` → git commit |
| `npc integrate` / `npc change run` | worktree 产物整合进 main / 驱动单 change 内环 |
| `npc agent prompt render` / `npc agent spawn-prompt` | 完整 sub-agent prompt 写盘，返回薄 spawn 引导语 |
| `npc telemetry hotspots` / `npc watch` | 跨 run 成本热点 / 后台任务实时观测 |

低层命令（`state`、`phase`、`review parse` 等）保留供调试与定制。完整契约——每个参数、stdout schema、exit code——见 [docs/cli.md](docs/cli.md)。

## 运行轨迹

一切落在 `~/task_log/<PROJ_KEY>/`，按工程路径分 key——目标仓库内零写入：

```
~/task_log/<PROJ_KEY>/
├── active.json                     # 指向当前 active run
├── index.jsonl                     # 跨 run 索引（每 run 一行 JSON）
├── <run_ts>-plan-state.json        # run 权威状态（另有 .md 人类视图）
└── <run_ts>/                       # 该 run 的中间产物
    ├── run.json / run.events.jsonl / run-summary.md
    ├── tasks/                      # watchable 后台任务契约
    └── 001-<change>/               # 每 change 的 prompt / review / summary
```

## 配置

TOML 分层深合并：全局 `~/.config/npc/config.toml` 定义 provider 与凭据，工程内 `.npc/config.toml` 只做路由。覆盖 review 引擎（`codex`/`claude`）、coder provider 注册表与宿主设置——详见 [docs/configuration.md](docs/configuration.md)。

## 设计哲学

- **分清"决策"与"动作"** — LLM 做语义判断与人机交互，软件做确定性的状态 / 字符串 / 子进程编排；例行决策点下沉给 `npc auto-decide`。
- **LLM 不做数据搬运** — 子命令自包含 resolve 路径，模板落盘不过 context，pipeline 把多步机械动作打包成一次调用。
- **JSON + exit code 是通信契约** — 主 session 用 `jq` 取字段、用 `$?` 分支，不解析自然语言。
- **状态原子化 + 可自愈** — 每次状态写入走 tmp + `os.replace`；git HEAD 与 task_log 漂移时修复而非忽略。
- **执行可以廉价，review 必须 premium** — 第三方后端只许实现，不许给自己的产出盖章。

完整版本（架构不变量与 roadmap）见 [docs/principles.md](docs/principles.md) 与 [docs/design.md](docs/design.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/usage.md](docs/usage.md) | 推荐用法：CLI + playbooks + 项目上下文三层配置，端到端 |
| [docs/cli.md](docs/cli.md) | `npc` 完整契约：全部命令、stdout schema、exit code |
| [docs/configuration.md](docs/configuration.md) | review 引擎、coder provider、宿主配置与排错 |
| [docs/design.md](docs/design.md) | 总体方案与设计决策记录 |
| [docs/principles.md](docs/principles.md) | 架构不变量与 roadmap |

## 开发

```bash
uv run pytest -q            # 全部用例（40 个文件，680+ 用例）
uv run pytest --cov=npc     # 覆盖率
```

测试经 `tmp_path` + monkeypatch 隔离，不触碰真实 `~/task_log` 与 `~/.claude`；外部二进制（`codex`、`openspec`）在测试中为 fake。

## License

MIT
