# agent-spine

> Deterministic execution layer for Agent skills — the "Non-Player Character" that runs the scripted work while the LLM plays.
>
> Agent skill 的确定性执行层：执行脚本化机械动作的 NPC，让 LLM 专注决策。

[![tests](https://github.com/winewei/agent-spine/actions/workflows/test.yml/badge.svg)](https://github.com/winewei/agent-spine/actions/workflows/test.yml)

把 skill.md 中**确定性的** bash + jq 散落逻辑（路径计算、状态读写、模板渲染、事件追加、指标派生、commit 校验，以及外部子进程 codex/openspec/git 的编排）迁移到 Python CLI，达成：

- **减少 LLM 上下文压力**：skill.md 行数大幅下降，每次调用 token 成本下降
- **提升执行确定性**：纯字符串拼接 / 状态读写 / 模板渲染 / 子进程编排交给软件，而不是 LLM
- **降低维护成本**：bash + jq 散落片段 → Python 类型化模块 + pytest 覆盖
- **统一调用契约**：CLI 子命令名稳定，skill.md 引用接口而非内部实现

首期产品：`npc` CLI，配合 `/new-plan-changes-v2` skill。

| 版本 | 关键变化 |
|---|---|
| **1.0.0** | **Sub-agent prompt 下沉**：新增 `agent prompt render` / `agent spawn-prompt`，把 §A Implementer / §B Fixer 模板从 skill 文档迁到 CLI 包资源（`templates.py`）。模板内容**不再流过主 session context**——主 session 仅承担 ~150 tokens 的薄引导语，模板本体由 sub-agent 自己 Read。单次 sub-agent 调用主 session 注意力节省 ~77%。 |
| 0.3.0 | 高层 pipeline：`review run` / `archive run` / `implement record` / `fix record`，把 codex/openspec 子进程与 phase 装订下沉；LLM 只读一行 JSON 决策 |
| 0.2.0 | 子命令自包含：`run.json` + `active.json` 持久化运行时上下文，废弃 `eval "$(npc init --shell-exports)"` 仪式 |
| 0.1.0 | paths / state / events / review / trend / focus / fixer / session / resume / git_chain / summary / init 等基础模块 |

---

## 安装

### 从 GitHub 安装（推荐）

直接从仓库安装，无需克隆到本地：

```bash
# SSH（私有仓库，需配置 GitHub SSH key）
uv tool install --reinstall git+ssh://git@github.com/winewei/agent-spine.git

# HTTPS（仓库公开时）
uv tool install --reinstall git+https://github.com/winewei/agent-spine.git

npc --version
npc --help
```

升级到远端最新：

```bash
uv tool upgrade agent-spine
```

卸载：

```bash
uv tool uninstall agent-spine
```

### 本地源码安装

先克隆仓库，再从本地目录安装（`--reinstall` 必需：从本地路径装时 uv 不会自动感知版本变化）：

```bash
git clone git@github.com:winewei/agent-spine.git
cd agent-spine
uv tool install --reinstall --from . agent-spine
```

### 开发模式

```bash
git clone git@github.com:winewei/agent-spine.git
cd agent-spine
uv sync                # 创建 .venv + 装依赖
uv run npc --help      # 跑 CLI
uv run pytest -q       # 跑测试套件
```

或者用 editable 模式（源码改动直接生效，仅 `pyproject.toml` 元数据变化时才需重装）：

```bash
uv tool install --reinstall --editable .
```

### 系统依赖

- Python ≥ 3.11
- `git`（必需）
- `jq`（推荐；`npc state get` 输出 JSON 时下游 shell 经常需要 jq 取字段）
- `codex` CLI（仅 `npc review run` 需要）
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
3. `NPC_*` 环境变量（0.1 兼容，仍可用）

调用端不再需要 `eval "$(npc init --shell-exports)"`：

```bash
# 0.1 旧写法（仍可用，已 deprecated）
eval "$(npc init --shell-exports --auto)"
npc state init-run --plan-order '["a","b","c"]'

# 0.2+ 推荐写法
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

### State 读写

| 命令 | 职责 |
|---|---|
| `npc state init-run --plan-order JSON` | 首次创建 STATE_JSON / STATE_MD / run.events.jsonl |
| `npc state get <jq-path>` | 按 jq 路径取 STATE_JSON 字段 |
| `npc state add-change <seq> <change_id>` | 向 progress 追加 change 条目 |
| `npc state set-progress <seq> [opts]` | 更新 progress 字段（一般由 pipeline 命令代调） |
| `npc state finalize` | 收尾：判定顶层 status |

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

### Sub-agent Prompt（v1.0+ — 把模板从 skill 下沉到 CLI）

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

完整契约（参数、stdout JSON schema、exit code）见 [docs/cli.md](docs/cli.md)。

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

## 端到端示例（0.3）

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
    ├── active.json                        # 指向当前 active run_ts（0.2+）
    ├── index.jsonl                        # 跨 run 索引（每 run 一行 JSON）
    ├── 2026-05-22-1545-plan-state.json    # 此 run 权威状态
    ├── 2026-05-22-1545-plan-state.md      # 此 run 人类视图
    └── 2026-05-22-1545/                   # 此 run 中间产物
        ├── run.json                       # 该 run 的派生路径快照（0.2+）
        ├── run.events.jsonl               # run 级聚合事件流
        ├── run-summary.md                 # run 结束最终汇总
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

`PROJ_KEY` 是工程根路径中 `/` 替换为 `-` 的结果（与 `~/.claude/projects/` 编码一致），方便 agent 跨工程串联 cc transcript 做学习。

---

## 项目结构

```
claude_tools/
├── README.md
├── pyproject.toml              # uv 管理；scripts: npc -> agent_spine.npc.cli:main
├── uv.lock
├── docs/
│   ├── design.md               # 总体方案 + 决策记录 + 路线图
│   └── cli.md                  # CLI 契约
├── src/agent_spine/
│   ├── __init__.py
│   └── npc/
│       ├── __init__.py
│       ├── cli.py              # argparse dispatcher + 惰性导入 handler
│       ├── _io.py              # 输出 JSON / stderr / 时间戳工具
│       ├── paths.py            # 路径计算 + run.json/active.json + load_paths
│       ├── state.py            # STATE_JSON 读写 + STATE_MD 渲染 + 原子替换
│       ├── events.py           # phase 计时 + 双流事件追加
│       ├── review.py           # review.json 派生指标
│       ├── trend.py            # blocking_trend + stale 检测
│       ├── focus.py            # codex focus 文本模板渲染
│       ├── fixer.py            # Fixer findings 片段抽取
│       ├── session.py          # cc session_id 三路径识别
│       ├── resume.py           # 续跑断点判定
│       ├── git_chain.py        # commit chain 校验
│       ├── schema.py           # review output-schema 自举
│       ├── summary.py          # run-summary.md + index.jsonl
│       ├── init_cmd.py         # npc init 整合入口
│       ├── pipeline.py         # 高层 pipeline：review run / archive run / record（0.3+）
│       ├── templates.py        # §A Implementer / §B Fixer prompt 模板（1.0+）
│       └── agent.py            # agent prompt render / spawn-prompt handler（1.0+）
└── tests/                      # pytest 测试套件（159 个测试）
    ├── conftest.py
    ├── test_paths.py
    ├── test_state.py
    ├── test_events.py
    ├── test_review.py
    ├── test_trend.py
    ├── test_focus.py
    ├── test_fixer.py
    ├── test_session.py
    ├── test_resume.py
    ├── test_git_chain.py
    ├── test_schema.py
    ├── test_summary.py
    ├── test_init_cmd.py
    ├── test_pipeline.py        # review run / archive run / record（0.3+）
    ├── test_templates.py       # §A / §B prompt 模板（1.0+）
    └── test_agent.py           # agent prompt render / spawn-prompt（1.0+）
```

---

## 测试

```bash
uv run pytest -q                          # 跑全部（159 个）
uv run pytest tests/test_pipeline.py -v   # 跑 pipeline 模块
uv run pytest tests/test_agent.py -v      # 跑 agent prompt 模块
uv run pytest -k phase --tb=short         # 按名称过滤
uv run pytest --cov=agent_spine          # 覆盖率
```

所有测试用 `tmp_path` + monkeypatch 隔离，不污染真实 `~/task_log` 或 `~/.claude`。`codex` / `openspec` 子进程在测试中通过 monkeypatch 替换为 fake。

---

## 设计哲学

- **分清"决策"与"动作"**：LLM 强项是语义判断与人机交互；软件强项是确定性的状态、字符串、子进程编排。两者各司其职
- **LLM 不做数据搬运**：从 0.2 起子命令自包含 resolve 路径；从 0.3 起高层 pipeline 把整段"focus 渲染 + 子进程调度 + 状态装订"打包，LLM 只看一行 JSON 决策
- **stdout JSON + exit code 是通信契约**：主 session 用 `jq` 取字段，用 `$?` 分支错误；不再解析自然语言
- **状态写入原子化**：每次 STATE_JSON 改动都用 tmp + `os.replace`，同步重写 STATE_MD，杜绝漂移
- **运行轨迹外置 + 学习入口结构化**：`~/task_log/<PROJ_KEY>/index.jsonl` 是跨 run 学习的稳定入口
- **不依赖 cc 才能跑**：CLI 可在 CI / 手工调试时独立运行，便于演化与回归

---

## License

MIT
