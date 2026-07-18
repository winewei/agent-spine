# agent-spine

> 人驾驭的自主 harness（跑在 Claude Code 进程内）——从 spec 到结果交付。
>
> 主 session 调度、spine-coder 执行、确定性动作委托给安装后的 `npc` 命令（代码在 `src/npc`）。

本仓库是 **harness 的上层**（plugin + skill + 宪法），并包含完整的确定性执行层代码（`src/npc`）。在仓库根执行 `uv tool install --from . npc` 后，本机得到的 `npc` 命令就是这个执行层。

- **智能层**：[`/spine-run`](plugins/agent-spine/commands/spine-run.md) 编排 plan→implement→review→fix→archive；[`/spine-analyze`](plugins/agent-spine/commands/spine-analyze.md) 自迭代。
- **执行层**：[`spine-coder`](plugins/agent-spine/agents/spine-coder.md) subagent。
- **底座**：安装后的 `npc` 命令（代码在 `src/npc`）。
- **批量入口**：[`/new-plan-changes-v2`](plugins/agent-spine/commands/new-plan-changes-v2.md)（串行）、[`new-plan-changes-v3`](plugins/agent-spine/skills/new-plan-changes-v3/SKILL.md)（波次并行 worktree）与 [`new-plan-changes-v4`](plugins/agent-spine/skills/new-plan-changes-v4/SKILL.md)（v1.5 上下文预算版：每 change 三条 npc 命令——spawn / `npc integrate` / `npc change run`，主 session 只在决策分叉点出场）——按依赖顺序批量推进 OpenSpec active changes，共享同一个 `npc` 底座。
- **宪法**：[docs/principles.md](docs/principles.md) 4 条不变量。

---

## 安装

```bash
git clone https://github.com/winewei/agent-spine.git
cd agent-spine

# 1) 装 npc 命令（从当前仓库根安装 src/npc）
uv tool install --force --from . npc
npc --version          # npc 1.6.0

# 2) 装 harness plugin（Claude Code 内）
#   /plugin marketplace add <本仓库路径或 winewei/agent-spine>
#   /plugin install agent-spine@agent-spine
```

> **推荐三层配置**（CLI + plugin + CLAUDE.md 片段）见 [docs/usage.md](docs/usage.md)。`npc` 完整契约见 [docs/cli.md](docs/cli.md)。

### 一句话安装（面向 agent）

在仓库根执行（幂等，详见 [INSTALL.md](INSTALL.md)）：

```bash
uv tool install --force --from . npc && claude plugin marketplace add "$(pwd)" && claude plugin install agent-spine@agent-spine --scope user
```

### Claude Code Plugin 安装（手动）

```text
# 在 Claude Code 中执行：
/plugin marketplace add <本仓库绝对路径>   # 或 winewei/agent-spine
/plugin install agent-spine@agent-spine
```

装完得到 `/spine-run`、`/spine-analyze`、`spine-coder`、`/new-plan-changes-v2`、`new-plan-changes-v3`、`new-plan-changes-v4`（skill，自动触发）（**重启 Claude Code 后生效**）。Plugin 升级 `/plugin update agent-spine@agent-spine`。

> `npc` 命令与 plugin 相互独立：CLI 机器级装一次，plugin 用户级装一次。`npc` 的命令速查/契约见 [docs/cli.md](docs/cli.md)。

### 系统依赖

- Python ≥ 3.11
- `git`（必需）
- `jq`（推荐；`npc state get` 输出 JSON 时下游 shell 经常需要 jq 取字段）
- `codex` CLI（默认 review 引擎，`npc review run` 需要；可经 `.npc/config.toml` 切到 `claude` 引擎，二选一）
- `openspec` CLI（仅 `npc archive run` 需要）
- `portable-timeout`（自动安装到 `~/.local/bin/`，跨平台 timeout wrapper；首次 `npc init` 时自举）

---

## 核心概念

### 运行时上下文：`run.json` + `active.json`

`npc init` 落盘两份文件，后续所有子命令据此**自包含 resolve** 当前 run 的全部派生路径：

```
~/task_log/<PROJ_KEY>/
├── active.json                       # 指向当前 active run_ts
└── 2026-05-22-1545/
    └── run.json                      # 该 run 的全部 deterministic 元数据
```

子命令的 resolve 优先级：

1. 显式参数 `--run-ts` / `--task-log-dir` / `--state-json`
2. `cwd → git toplevel → task_log_dir → active.json → run.json`
3. `NPC_*` 环境变量（显式覆盖，仍可用）

初始化与首次建 plan 只需两行，无需把环境变量注入当前 shell：

```bash
npc init --auto
npc state init-run --plan-order '["a","b","c"]'
```

### 命令层级

```
低层（细粒度）           高层（pipeline）              典型用途
─────────────────────────────────────────────────────────────
npc phase enter/exit  ┐
npc state set-progress├─→  npc implement record       sub-agent 跑完后一行装订
npc state add-change  ┘    npc fix record

npc focus render      ┐
codex exec (外部)      │
npc review parse      ├─→  npc review run --seq N --round M
npc review update-trend│                                完整一轮 codex review
npc review check-stale ┘
npc fixer findings

npc archive precheck   ┐
openspec validate (外部)├─→ npc archive run --seq N
openspec archive (外部) │                                archive 全流程
git add+commit         ┘
```

LLM 仅需调用高层命令，看一行 JSON 输出做决策；低层命令保留供调试与定制场景。

---

## 命令速查

### 初始化与续跑

| 命令 | 职责 |
|---|---|
| `npc init [--auto] [--fresh]` | 探测 git/session，计算路径，落 `run.json` + `active.json`，自举 schema 与 portable-timeout |
| `npc resume detect` | 续跑断点判定（next_seq / next_phase / current_round） |
| `npc auto-decide --trigger T [opts]` | `--auto` 模式决策器：输入触发点，基于 progress 输出 action（continue-retry / skip / force-archive / abort），主 session 只负责执行 |

### State 读写

| 命令 | 职责 |
|---|---|
| `npc state init-run --plan-order JSON` | 首次创建 STATE_JSON / STATE_MD / run.events.jsonl |
| `npc state get <jq-path>` | 按 jq 路径取 STATE_JSON 字段 |
| `npc state add-change <seq> <change_id>` | 向 progress 追加 change 条目 |
| `npc state set-progress <seq> [opts]` | 更新 progress 字段（一般由 pipeline 命令代调） |
| `npc state finalize` | 收尾：判定顶层 status |
| `npc state repair <seq>` | state 自愈：git HEAD 与 task_log 漂移时，把对应 progress 重置为 pending、openspec archive 退回 active |

### Phase 计时与事件（细粒度）

| 命令 | 职责 |
|---|---|
| `npc phase enter <seq> <phase>` | 落事件 + 计时起点 |
| `npc phase exit <seq> <phase> --status done\|failed [--extra JSON]` | 计算 duration_ms + 落事件 |

### Review（低层）

| 命令 | 职责 |
|---|---|
| `npc review parse <review.json>` | 派生 verdict / blocking / advisory / categories |
| `npc review update-trend <seq> --metrics JSON` | 维护 blocking_trend + rounds_since_strict_decrease |
| `npc review check-stale <seq>` | 判定 stale（连续 ≥ 3 轮 blocking 未严格下降） |

### Pipeline（高层 — 推荐 LLM 用这层）

| 命令 | 职责 |
|---|---|
| `npc review run --seq N --round M` | focus → codex exec（重试 1 次） → parse → update-trend → phase exit；输出 verdict/blocking/stale/review_json |
| `npc archive run --seq N` | archive precheck → openspec validate --strict → openspec archive --yes → git commit → 状态装订 |
| `npc implement record --seq N --result "<RESULT 行>"` | 校验 commit + summary，phase exit + state set-progress 一条龙 |
| `npc fix record --seq N --round M --result "<RESULT 行>"` | 同上，针对 fix 阶段 |

### 模板与抽取（一般由 pipeline 内部调用）

| 命令 | 职责 |
|---|---|
| `npc focus render --round N --change-id ID --output PATH` | 渲染 codex review focus 文本 |
| `npc fixer findings --review PATH --output-fragment PATH` | 抽 in_scope blocking findings 为 Fixer prompt 片段 |

### Sub-agent Prompt（把模板从 skill 下沉到 CLI）

| 命令 | 职责 |
|---|---|
| `npc agent prompt render --phase {implement\|fix} --change-id ID [--round N]` | 渲染完整 Implementer / Fixer prompt 到 `$BASE/...prompt.md`（主 session 不接触模板内容） |
| `npc agent spawn-prompt --phase ... --change-id ID [--round N] [--extension PATH\|--extension-inline TEXT]` | 生成给 Claude `Agent` 工具 `prompt` 字段用的薄引导语（~150 tokens） |

典型流程：

```bash
# 1. npc 渲染完整 prompt 到 disk（~2500 bytes 完全不流过主 session）
npc agent prompt render --phase implement --change-id add-foo

# 2. 取薄引导语；主 session 用它作为 Agent.prompt 字段
SPAWN=$(npc agent spawn-prompt --phase implement --change-id add-foo)
PROMPT_TEXT=$(echo "$SPAWN" | jq -r '.prompt')
# 主 session：Agent(subagent_type="senior-code-developer", prompt="$PROMPT_TEXT")
```

Fix 阶段额外做的事：自动从 `$BASE/round-(N-1).review.json` 抽 blocking findings、从 state 取 `categories_seen` / `blocking_trend` / `implement_commit`，全部注入到模板，调用方无需先跑 `npc fixer findings`。

### 收尾

| 命令 | 职责 |
|---|---|
| `npc archive precheck <seq>` | archive 前 commit chain 一致性校验（低层；archive run 内部调用） |
| `npc summary render` | 写 `<run_dir>/run-summary.md` |
| `npc index append` | 追加 `<task_log_dir>/index.jsonl` |

### Telemetry（跨 run 指标，1.2+）

| 命令 | 职责 |
|---|---|
| `npc telemetry tail [--kind K] [--last N]` | 看 `~/task_log/_telemetry/events.ndjson` 最近 N 条原始指标事件 |
| `npc telemetry agg [--by phase\|change\|week] [--since 7d]` | 按维度聚合并落盘到 `_telemetry/aggregates/by-*.json` |
| `npc telemetry hotspots [--top 5]` | 按 (failure_rate × p50_duration × retries) 排序，给出最值得优化的 phase |
| `npc telemetry estimate-tokens <file>` | 单文件 token 估算（bytes/4） |
| `npc telemetry emit --kind K [...]` | 手动追加 record（排错；正常由 pipeline/events/agent 自动 emit） |

埋点已嵌入 `events.phase_exit` / `events.phase_rotate` / `pipeline._do_phase_exit` / `pipeline.run_review_round` / `pipeline.run_archive` / `agent.spawn_prompt`，全部 best-effort——失败 swallow，主流程零影响。主 session 永远只读 `aggregates/*.json` 与 `hotspots` 输出，不读 events.ndjson 原文。

### Watchable Tasks（本机后台任务观测）

| 命令 | 职责 |
|---|---|
| `npc task start --id ID --description TEXT [...]` | 在当前 run 的 `<run_dir>/tasks/` 登记一个可观测任务，记录 worktree/branch/head 与 heartbeat 检查契约 |
| `npc task update --id ID [...]` | 更新 phase/message/progress/pointer |
| `npc task heartbeat --id ID [...]` | 刷新任务心跳，供 watch 判活 |
| `npc task finish --id ID [--status done\|failed\|cancelled]` | 标记任务终态 |
| `npc watch [--once] [--all] [--project PATH]` | 只读扫描 active run 的 state + tasks；`--once` 输出 JSON，默认循环刷新终端视图 |

完整契约（参数、stdout JSON schema、exit code）见 [docs/cli.md](docs/cli.md)。

---

## Review 引擎配置

`npc review run` 默认用 `codex`，也可切到 `claude`（即 `claude -p` 非交互模式）。引擎与可执行文件经 TOML 配置文件指定，按优先级分层加载：

1. `--config <path>`（CLI 显式传入；只读该文件，不参与合并）
2. `<repo_root>/.npc/config.toml`（项目级；可入 git，只放路由不放凭据）
3. `~/.config/npc/config.toml`（用户全局；provider 定义与凭据指针的家）
4. `~/task_log/config.toml`（兼容 task_log 布局）

2-4 层做**分层深合并**（v1.6+）：table 递归合并、标量/数组整体覆盖，低优先级打底、高优先级覆盖。典型分工：全局定义 `[providers.*]`，项目只写 `[coder]` 路由（见下节 [Coder 多模型配置](#coder-多模型配置每工程独立选型)）。

完整 schema：

```toml
[review]
engine = "codex"           # codex（默认）| claude

[review.codex]
bin = "codex"              # 可省略；默认 PATH 查找

[review.claude]
bin = "claude"             # 可省略；默认 PATH 查找
model = "claude-opus-4-7"  # 可省略；省略则用 claude 默认 model
extra_args = ["--permission-mode", "default"]   # 可省略
```

### 用自定义 claude 包装（路由到 qwen / deepseek 等后端）

常见做法是用 shell alias 把 `claude` 指到不同后端，例如 `~/.zshrc` 里：

```zsh
alias claude-qwen='claude --settings ~/.claude/qwen-settings.json'
```

**alias 不能直接填进 `bin`**——npc 用 `subprocess` 起子进程，不经过 shell，看不到 `.zshrc` 里的 alias。正确做法是把 alias 拆成 `bin` + `extra_args`：

```toml
[review]
engine = "claude"

[review.claude]
bin = "claude"
extra_args = ["--settings", "/Users/you/.claude/qwen-settings.json"]
```

四个易错点：

- **`bin` 填真实可执行文件**（`claude`，PATH 上有；或写绝对路径），**不是 alias 名**。
- **alias 后面的 flag 全挪进 `extra_args`**；npc 会拼成 `claude -p --output-format text <extra_args...>`，顺序无所谓。
- **路径必须用绝对路径，`~` 不会展开**——`extra_args` 原样进 argv、不经过 shell，写 `~/...` 会被当字面量找不到文件。
- 后端模型由 settings 文件决定时，**别再设 `model`**，否则会多传一个冲突的 `--model`。

> 若 alias 里含内联环境变量（如 `FOO=bar claude ...`），`extra_args` 表达不了，需做一个真实 wrapper 脚本（如 `~/.local/bin/claude-qwen`，内部 `export` 后 `exec claude "$@"`），再把 `bin` 指向它的绝对路径。

---

## Coder 多模型配置（每工程独立选型）

v1.6+ 的 coder（implement / fix 执行体）通过 **provider 注册表**路由：全局定义一次模型与凭据，每个工程只声明"用哪个"。内置三个 provider 无需声明：`claude`（默认，走订阅）、`mimo`、`codex`。接入 kimi / qwen / deepseek 等 Anthropic 兼容端点按下面四步照做。

**第 1 步：为每个模型建一个 env 凭据文件**（全局，勿入任何 git 仓库）：

```bash
mkdir -p ~/.config/npc
cat > ~/.config/npc/kimi.env <<'EOF'
export ANTHROPIC_BASE_URL=https://api.moonshot.cn/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-你的密钥
EOF
chmod 600 ~/.config/npc/kimi.env
```

`BASE_URL` 填厂商的 **Anthropic 兼容端点**（以各厂商官方文档为准；kimi 为 `https://api.moonshot.cn/anthropic`，deepseek 为 `https://api.deepseek.com/anthropic`）。支持 `export K=V` 与裸 `K=V` 两种写法，`#` 注释行会被忽略。

**第 2 步：在全局 `~/.config/npc/config.toml` 注册 provider**：

```toml
[providers.kimi]
runner = "claude-cli"                  # claude-cli（默认，可省略）| codex-cli
env_file = "~/.config/npc/kimi.env"
model = "kimi-k3"

[providers.deepseek]
env_file = "~/.config/npc/deepseek.env"
model = "deepseek-chat"

[providers.gpt-codex]
runner = "codex-cli"                   # 走 codex exec，无需 env_file（用 codex 自身登录态）
model = "gpt-5.4-codex"
```

**第 3 步：在目标工程写 `.npc/config.toml`，只做路由**（可入 git，无凭据）：

```toml
[coder]
backend = "kimi"          # 本工程 coder 默认走 kimi

[coder.phase]             # 可选：按阶段细分
implement = "kimi"
fix = "deepseek"
```

review 不用配——恒走 premium 引擎（codex / claude），这是刻意设计：第三方廉价层只许执行，不许给自己盖章（`npc verify routing` 会强制拦截 review 路由到任何带 env_file 的 provider）。

**第 4 步：验证**：

```bash
npc doctor | jq '.checks[] | select(.name=="providers")'   # 期望 status=="ok"，detail 列出在用 provider
npc verify routing                                          # 期望 {"ok":true,...}，exit 0
```

临时切换某一次执行的后端：`npc implement run --seq 1 --backend deepseek`（`--backend` 接受任意已注册 provider 名）。

常见报错对照：

| 现象 | 原因 | 处理 |
|---|---|---|
| 加载配置报 `未知 coder backend：'kimi'` (exit 2) | 路由引用了没注册的 provider | 检查全局 config.toml 的 `[providers.kimi]` 是否存在、拼写是否一致 |
| `env_file 缺失`（exit 4）| 第 1 步的 env 文件路径不对或没建 | `npc doctor` 的 providers 检查会给出期望路径 |
| `env_file 读取失败...权限`（exit 3）| 文件权限过紧且属主不对 | 确认文件属主为当前用户且 `chmod 600` |
| coder 跑完但走的还是 Claude 订阅 | env 文件里 `ANTHROPIC_BASE_URL` 拼错 / 未生效 | 检查 env 文件 key 名；provider 路由只在显式配置后启用 |

---

## 输出约定

- **stdout**：单行 JSON 对象，便于 bash 用 `jq` 取值
- **stderr**：人类可读消息（`[npc] ...`）/ 警告（`[npc:warn] ...`）
- **exit code**：
  - `0` 成功
  - `1` 业务失败（含 jq 表达式错、seq 越界、JSON 解析失败、pipeline 业务失败）
  - `2` 用法错误（参数缺失 / 格式错）
  - `3` 环境错误（非 git 仓库 / 缺 STATE_JSON / 缺 run.json）
  - `4` 外部依赖缺失（codex / openspec / portable-timeout 未找到）

错误输出格式：

```json
{"ok": false, "error": "seq_out_of_range", "message": "seq=99 超出 progress 数组长度（total=3）"}
```

---

## 端到端示例

```bash
# 1. 初始化（落 run.json + active.json）
npc init --auto

# 2. 续跑判定
RESUME=$(npc resume detect)
NEXT_SEQ=$(echo "$RESUME" | jq -r '.next_seq')
NEXT_PHASE=$(echo "$RESUME" | jq -r '.next_phase')
# 主 session 据此决定走续跑还是新开

# 3. 首次创建 plan
npc state init-run --plan-order '["add-foo","add-bar","add-baz"]'

# 4. 每个 change 的循环（典型形态）
for SEQ in 1 2 3; do
  CID=$(npc state get ".plan_order[$((SEQ-1))]" | tr -d '"')
  npc state add-change $SEQ "$CID"

  # 4.1 Implement — sub-agent 跑完后把 RESULT 行喂回
  # 主 session 写 prompt → Agent(senior-code-developer) → 拿 RESULT 行
  IMPL=$(npc implement record --seq $SEQ --result "$RESULT_LINE")
  [ "$(echo "$IMPL" | jq -r '.ok')" = "true" ] || continue

  # 4.2 Review-Fix 循环（高层一行）
  R=$(npc review run --seq $SEQ --round 0)
  N=0
  while [ "$(echo "$R" | jq -r '.blocking')" -gt 0 ] \
     && [ "$(echo "$R" | jq -r '.stale')" = "false" ] \
     && [ $N -lt 20 ]; do
    N=$((N+1))
    # LLM 写 fix.prompt（基于 R.findings_path）→ Agent(senior-code-developer)
    FIX=$(npc fix record --seq $SEQ --round $N --result "$FIX_RESULT_LINE")
    [ "$(echo "$FIX" | jq -r '.ok')" = "true" ] || break
    R=$(npc review run --seq $SEQ --round $N)
  done

  # 4.3 Archive 全流程一行
  npc archive run --seq $SEQ
done

# 5. 收尾
npc state finalize
npc summary render
npc index append
```

---

## 运行轨迹外置

所有 state / event / summary / index 落在用户级目录，**工程内零侵入**：

```
~/task_log/
├── .new-plan-review-schema.json           # codex output-schema（全用户共享）
├── .session-cache/                        # SessionStart hook 落盘
│   ├── sessions/<sid>.json
│   └── by-cwd/<PROJ_KEY>.jsonl
└── <PROJ_KEY>/                            # 与 ~/.claude/projects/ 同 mangle
    ├── active.json                        # 指向当前 active run_ts
    ├── index.jsonl                        # 跨 run 索引（每 run 一行 JSON）
    ├── 2026-05-22-1545-plan-state.json    # 此 run 权威状态
    ├── 2026-05-22-1545-plan-state.md      # 此 run 人类视图
    └── 2026-05-22-1545/                   # 此 run 中间产物
        ├── run.json                       # 该 run 的派生路径快照
        ├── run.events.jsonl               # run 级聚合事件流
        ├── run-summary.md                 # run 结束最终汇总
        ├── tasks/                         # watchable task 主动上报契约
        │   ├── implement-001.json         # 当前任务快照（heartbeat/status/pointer/worktree）
        │   └── implement-001.events.jsonl # 追加式任务事件历史
        ├── 001-add-foo/
        │   ├── change.md
        │   ├── events.jsonl
        │   ├── implement.prompt.md
        │   ├── implement.summary.md
        │   ├── round-0.focus.md
        │   ├── round-0.review.json        # codex 结构化输出
        │   ├── round-0.events.jsonl       # codex --json 事件流
        │   ├── round-1.fix.findings.md    # review run 自动渲染（当 blocking>0）
        │   ├── round-1.fix.prompt.md
        │   └── round-1.fix.summary.md
        └── 002-add-bar/
            └── ...
```

`PROJ_KEY` 是工程根路径中 `/` 替换为 `-` 的结果（与 `~/.claude/projects/` 编码一致），方便 agent 跨工程串联 Claude Code transcript 做学习。

---

## 项目结构

```
agent-spine/
├── README.md
├── pyproject.toml              # uv 管理；scripts: npc -> npc.cli:main
├── uv.lock
├── .claude-plugin/
│   └── marketplace.json        # Claude Code marketplace 元数据（仓库根 = 一个 marketplace）
├── plugins/
│   └── agent-spine/            # 唯一 plugin
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── README.md
│       ├── commands/
│       │   ├── spine-run.md            # /spine-run
│       │   ├── spine-analyze.md        # /spine-analyze
│       │   └── new-plan-changes-v2.md  # /new-plan-changes-v2（串行 pipeline）
│       ├── agents/
│       │   └── spine-coder.md          # spine-coder subagent
│       ├── scripts/
│       │   └── spine-coder-mimo.sh     # MiMo 成本路由 helper（coder.py 调用）
│       └── skills/
│           ├── new-plan-changes-v3/
│           └── new-plan-changes-v4/
│               └── SKILL.md            # v3 波次并行 skill
├── docs/
│   ├── design.md               # 总体方案 + 设计决策记录
│   ├── cli.md                  # CLI 契约
│   ├── usage.md                # 推荐用法：CLI + skill + CLAUDE.md 三层配置
│   ├── principles.md           # 架构不变量与 roadmap
│   └── optimization-proposals/ # 优化提案（含实施状态标注）
├── src/npc/
│   ├── __init__.py             # __version__
│   ├── cli.py                  # argparse dispatcher + 惰性导入 handler
│   ├── _io.py                  # 输出 JSON / stderr / 时间戳工具
│   ├── paths.py                # 路径计算 + run.json/active.json + load_paths
│   ├── state.py                # STATE_JSON 读写 + STATE_MD 渲染 + 原子替换
│   ├── events.py               # phase 计时 + 双流事件追加
│   ├── review.py               # review.json 派生指标
│   ├── trend.py                # blocking_trend + stale 检测
│   ├── focus.py                # codex focus 文本模板渲染
│   ├── fixer.py                # Fixer findings 片段抽取
│   ├── session.py              # Claude Code session_id 三路径识别
│   ├── resume.py               # 续跑断点判定
│   ├── git_chain.py            # commit chain 校验
│   ├── git_ops.py              # git branch-for / ensure-clean / commit
│   ├── schema.py               # review output-schema 自举
│   ├── summary.py              # run-summary.md + index.jsonl
│   ├── init_cmd.py             # npc init 整合入口
│   ├── pipeline.py             # 高层 pipeline：review run / archive run / record
│   ├── templates.py            # §A Implementer / §B Fixer prompt 模板
│   ├── agent.py                # agent prompt render / spawn-prompt / timeout-budget
│   ├── config.py               # 引擎/verify/coder/providers 配置加载（TOML，分层深合并）
│   ├── engines.py              # review 引擎抽象：codex exec / claude -p
│   ├── auto_decide.py          # npc auto-decide：--auto 模式决策器
│   ├── repair.py               # npc state repair：HEAD/state 漂移自愈
│   ├── telemetry.py            # 跨 run 指标层（v1.2）
│   ├── doctor.py               # npc doctor 环境体检
│   ├── coder.py                # coder 后端分层（provider 路由：claude-cli / codex-cli）
│   ├── plan.py                 # plan check / new-change / waves 入口
│   ├── waves.py                # DAG 切波（v1.4）
│   ├── verify.py               # verify tests / routing / manifest（v1.4）
│   ├── notify.py               # webhook 通知（v1.4）
│   ├── task.py                 # 后台任务上报契约 start/update/heartbeat/finish
│   ├── watch.py                # 跨 run 任务观测
│   ├── deliver.py              # deliver / pr open
│   ├── status.py               # run 进度只读快照
│   ├── cost.py                 # 按后端拆 token 成本
│   ├── clean.py                # task_log 清理
│   ├── spec_analyze.py         # spec 一致性分析
│   └── settings_auth.py        # settings/auth 辅助
└── tests/                      # pytest 测试套件（35 个文件，580+ 用例）
    ├── conftest.py
    └── test_*.py               # 与 src/npc/ 模块一一对应，另有 test_v11_features.py 等特性集
```

---

## 测试

```bash
uv run pytest -q                          # 跑全部（580+ 用例）
uv run pytest tests/test_pipeline.py -v   # 跑 pipeline 模块
uv run pytest tests/test_agent.py -v      # 跑 agent prompt 模块
uv run pytest -k phase --tb=short         # 按名称过滤
uv run pytest --cov=npc                   # 覆盖率
```

所有测试用 `tmp_path` + monkeypatch 隔离，不污染真实 `~/task_log` 或 `~/.claude`。`codex` / `openspec` 子进程在测试中通过 monkeypatch 替换为 fake。

---

## 设计哲学

- **分清"决策"与"动作"**：LLM 强项是语义判断与人机交互，软件强项是确定性的状态 / 字符串 / 子进程编排，两者各司其职。更进一步——`--auto` 模式下，凡能由 progress / blocking 趋势**机械判定**的决策点（continue-retry / skip / force-archive / abort）也下沉给 `npc auto-decide`，LLM 不再为例行选择打断用户
- **LLM 不做数据搬运**：子命令自包含 resolve 路径、高层 pipeline 把"focus 渲染 + 子进程调度 + 状态装订"打包成一行 JSON、sub-agent prompt 模板下沉到 CLI 包资源不流过主 session context、例行决策下沉到 `auto-decide`——主 session 的注意力从搬运 / 拼接 / 例行决策中逐层释放
- **引擎可插拔**：review 管线对 `codex` / `claude` 做抽象，同一份 focus prompt 可换后端（含经 `--settings` 路由到 qwen / deepseek 等自定义 claude 配置），不绑定单一工具
- **stdout JSON + exit code 是通信契约**：主 session 用 `jq` 取字段，用 `$?` 分支错误；不再解析自然语言
- **状态原子化 + 可自愈**：每次 STATE_JSON 改动用 tmp + `os.replace` 并同步重写 STATE_MD，杜绝漂移；当 git HEAD 与 task_log 漂移时，`npc state repair` 把对应 progress 退回 pending 重新对齐
- **运行轨迹外置 + 学习入口结构化**：`~/task_log/<PROJ_KEY>/index.jsonl` 是跨 run 学习的稳定入口
- **不依赖 Claude Code 也能跑**：`npc` 是独立 CLI 程序，可在 CI / 普通终端手工调试时单独运行（不像 MCP server 必须挂在 LLM agent 里），便于回归测试与契约演化

---

## License

MIT
