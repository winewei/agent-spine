# agent-spine 方案设计

## 1. 项目目标

为 Claude Code 的 skill 系统提供"工程化下沉"的命令行工具集，把 skill.md 中确定性的 bash + jq + 模板渲染逻辑迁移到 Python 实现，达成：

- **减少 LLM 上下文压力**：skill.md 行数下降，每次调用的 token 成本下降
- **提升执行确定性**：纯字符串拼接、状态读写、模板渲染交给软件而不是 LLM
- **降低维护成本**：bash/jq 散落片段 → Python 类型化模块 + pytest
- **统一调用契约**：CLI 子命令名稳定，skill.md 引用接口而非内部实现

范围：把 `/new-plan-changes` skill（1280 行）下沉，产出 `npc` 子工具与 `/new-plan-changes-v2` skill（目标 ≤ 500 行）。

## 2. 与 v1 skill 的关系

| 维度 | v1 (`/new-plan-changes`) | v2 (`/new-plan-changes-v2`) |
|---|---|---|
| 状态 | 保留作对照、生产可用 | 新建、激进重构 |
| 行数 | 1280 | 目标 ≤ 500 |
| state/events 操作 | bash + jq 散落 | `npc` CLI 调用 |
| schema_version | 兼容 v1 (平铺) + v2 (子目录) | 仅 v2 (子目录) |
| 旧 run 续跑 | 兼容 | 不兼容 |

v1 与 v2 共存，由用户按 skill 名显式选择。v2 稳定后再考虑是否废弃 v1。

## 3. 工具切分原则

### 留给 LLM 的决策（保留在 skill.md）

- 进入/退出 plan 模式的时机、用户交互
- 调用哪个 sub-agent、何时发起 `codex exec`
- 解读 review JSON 后的语义判断（archive / fix-loop / stale）
- 写 sub-agent prompt 时的语义裁量
- 错误恢复时的人机协商

### 下沉到 Python CLI 的工程化逻辑

| 模块 | 职责 |
|---|---|
| `paths` | REPO_ROOT / PROJ_KEY / RUN_DIR / STATE_JSON 等路径计算 |
| `state` | STATE_JSON 读写 + STATE_MD 同步渲染（事务化、原子写入） |
| `events` | events.jsonl + run.events.jsonl 双流追加，phase 计时 |
| `review` | review.json 解析、blocking/verdict/categories 派生 |
| `trend` | blocking_trend + rounds_since_strict_decrease 更新、stale 检测 |
| `focus` | round-0 / round-N focus 文本渲染，PROJECT_REVIEW_CONTEXT 注入 |
| `fixer` | 从 review 抽 in_scope blocking findings 为 prompt 片段 |
| `session` | session_id 识别（mtime 启发 + hook fallback 三路径） |
| `resume` | 续跑断点判定（扫 phases 找最后非 done） |
| `git_chain` | archive 前 commit chain 校验 |
| `summary` | run-summary.md 渲染 |
| `schema` | review schema 文件自举 |

## 4. 架构决策

### 4.1 CLI vs MCP：选 CLI

| 维度 | CLI | MCP |
|---|---|---|
| system prompt 占用 | 0 token | ~5000 token 永久占用 |
| 与 codex/git/openspec 协同 | shell 自然组合 | 需要 LLM 在多次 tool call 间编排 |
| stdin/stderr/管道 | 原生 | 难 |
| 脱离 Claude Code 跑 | 可（CI/手工） | 不可 |
| 演化成本 | 改 CLI 契约 + skill.md | 改 MCP server + 协议 + 重启 |

MCP 适合"LLM 智能调度"场景；本工具是"机械动作", CLI 完胜。

### 4.2 进程模型：每次调用一次进程

- 启动开销 ~30-50ms 可接受（一次 phase 转换涉及 2-3 次 npc 调用，总开销 ~100ms）
- 状态全部落盘，无内存常驻
- 与 `codex exec` 风格一致：stateless 子进程

### 4.3 状态写入：原子替换 + 自动同步 MD

每次 `npc state ...` 写入：

1. 读 STATE_JSON 到内存
2. 修改字段
3. 写临时文件 `.STATE_JSON.tmp`
4. `os.replace()` 原子替换 → STATE_JSON
5. 从 STATE_JSON 重新渲染 STATE_MD（一次性整体写入）

杜绝"STATE_MD 与 STATE_JSON 漂移"事故（v1 历史已发生过）。

### 4.4 输出格式：JSON to stdout + 人类消息到 stderr

所有 `npc <cmd>` 默认输出**单行 JSON 到 stdout**，便于 bash 用 `jq` 取值。
信息性消息 / 警告输出到 stderr，不污染数据流。

特殊命令 `--shell-exports` 选项允许输出 `export KEY=VALUE` 行供 `eval` 使用（仅 `npc init` 用）。

### 4.5 schema_version：v2 起步不兼容 v1

v1 平铺布局 + .txt 后缀在 v2 工具中不支持。旧 run 由用户手工标 aborted。

### 4.6 portable-timeout：保留 bash wrapper

不下沉到 Python，理由：每次 codex 调用都要走 wrapper，bash exec 转 Python 子进程平均增加 30-50ms × N 次，无收益。

## 5. 目录结构

```
agent-spine/
├── .git/
├── .gitignore
├── pyproject.toml              # uv 管理；scripts: npc -> npc.cli:main（包名 npc，v1.3 起从 agent_spine 改名）
├── README.md                   # 安装、使用、与 skill 的关系
├── docs/
│   ├── design.md               # 本文件
│   ├── cli.md                  # CLI 契约（每个子命令的参数与输出）
│   ├── usage.md                # 推荐用法：CLI + skill + CLAUDE.md 三层配置
│   ├── principles.md           # 架构不变量与 roadmap
│   └── optimization-proposals/ # 优化提案（含实施状态标注）
├── plugins/agent-spine/        # Claude Code plugin：commands / agents / skills / scripts
├── src/npc/
│   ├── __init__.py             # __version__
│   ├── cli.py                  # argparse dispatcher + 惰性导入 handler
│   ├── _io.py                  # 输出 JSON / stderr 工具
│   ├── paths.py / state.py / events.py          # 基石：路径、状态、事件
│   ├── review.py / trend.py / focus.py / fixer.py / schema.py   # review 管线低层
│   ├── session.py / resume.py / git_chain.py / summary.py       # 恢复与收尾
│   ├── pipeline.py / templates.py / agent.py    # 高层 pipeline 与 prompt 下沉（§11.2/11.3）
│   ├── config.py / engines.py                   # 多引擎 review（§11.4）
│   ├── auto_decide.py / repair.py               # 决策下沉与自愈（§11.5）
│   ├── telemetry.py                             # 跨 run 指标层（§11.6，v1.2）
│   ├── verify.py / doctor.py / coder.py / plan.py / git_ops.py
│   │                                            # 质量门 / 体检 / coder 分层 / plan 门 / git（§11.7，v1.3）
│   ├── deliver.py / status.py / cost.py / clean.py / spec_analyze.py   # 交付与运维（§11.7，v1.3）
│   ├── task.py / watch.py                       # 后台任务上报与观测（§11.8）
│   ├── waves.py / notify.py                     # 波次切分与通知（§11.9，v1.4）
│   └── change.py / integrate.py                 # 内环状态机与整合编排（§11.10，v1.5）
└── tests/
    ├── conftest.py             # tmp_path fixture + fake STATE_JSON
    └── test_*.py               # 与 src/npc/ 模块一一对应（35 文件，580+ 用例）
```

## 6. CLI 命令一览

完整契约见 `docs/cli.md`。命令分组：

- **初始化与续跑**：`init` / `resume detect`
- **plan 管理**：`state add-change` / `state finalize`
- **状态读写**：`state get` / `state set-progress`
- **Phase 计时**：`phase enter` / `phase exit`
- **Review 解析**：`review parse` / `review update-trend` / `review check-stale`
- **Focus 渲染**：`focus render`
- **Fixer prompt**：`fixer findings`
- **Archive**：`archive precheck`
- **收尾**：`summary render` / `index append`
- **Telemetry（1.2+）**：`telemetry emit` / `telemetry tail` / `telemetry agg` / `telemetry hotspots` / `telemetry estimate-tokens`
- **高层 pipeline（1.1+）**：`review run` / `archive run` / `implement run|record` / `fix run|record` / `phase rotate`
- **Sub-agent prompt（1.1+）**：`agent prompt render` / `agent spawn-prompt` / `agent timeout-budget` / `agent record-timeout`
- **决策与自愈（1.1+）**：`auto-decide` / `state repair`
- **质量门与体检（1.3+）**：`verify tests` / `verify routing` / `doctor`
- **plan 与 git（1.3+）**：`plan check` / `plan new-change` / `git branch-for` / `git ensure-clean` / `git commit`
- **交付与运维（1.3+）**：`deliver` / `pr open` / `status` / `cost` / `clean` / `spec analyze`
- **后台任务观测（1.3+）**：`task start|update|heartbeat|finish` / `watch`
- **波次与通知（1.4+）**：`plan waves` / `verify manifest` / `notify`
- **内环与整合（1.5+）**：`change run` / `integrate` / `status --brief` / `state note` / `verify tasks`

## 7. 安装与分发

```bash
# 开发期
git clone git@github.com:winewei/agent-spine.git
cd agent-spine
uv sync
uv run npc --help

# 全局安装（CLI 入口 npc 进 PATH；包名 npc，从本地 clone 安装）
uv tool install --force --from . npc
npc --help

# 升级（重跑同一条安装命令即可）
uv tool install --force --from . npc
```

skill.md v2 在顶部用 `npc --version` 检查版本兼容性。

## 8. 测试策略

- **单元测试**：每个模块对应 `tests/test_<mod>.py`，覆盖 happy path + 边界
- **fixture**：`conftest.py` 提供 tmp_path + 预制 STATE_JSON 样本
- **集成测试**：跑一次 fake plan（mock codex / agent），验证端到端状态流转
- **不测**：`codex exec` 真实调用、git 仓库交互（用 fake repo fixture）

## 9. 路线图

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 骨架 + docs/cli.md + pyproject.toml | `npc --help` 跑通 |
| P1 | paths + state + events（基石）| 单元测试通过 |
| P2 | review + trend + focus + fixer | 单元测试通过 |
| P3 | session + resume + git_chain | 单元测试通过 |
| P4 | summary + schema + cli 集成 | `npc init` 端到端可跑 |
| P5 | `/new-plan-changes-v2` skill.md | 行数 ≤ 500 |
| P6 | 端到端验证（找小工程实跑）| 完整 plan 跑通 |

## 10. 与既有约束的对齐

- **运行轨迹外置**：所有 state / event / summary 仍落 `~/task_log/<PROJ_KEY>/`，工程目录零侵入
- **schema 路径**：`~/task_log/.new-plan-review-schema.json`，与 v1 共享文件位置（schema 内容兼容）
- **portable-timeout**：保留 `~/.local/bin/portable-timeout` bash wrapper
- **SessionStart hook**：复用 v1 的 `~/.claude/hooks/session-start-cache.sh`，无需重装

v2 工具不重复造这些基础设施，复用 v1 已存在的硬约束。

---

## 11. 进阶设计决策

端到端实测后暴露了几类结构性冗余，下面是对应的下沉决策。

### 11.1 子命令自包含（`run.json` + `active.json`）

**问题**：早期调用模式要求 `eval "$(npc init --shell-exports --auto)"`，把 13 个 `NPC_*` 环境变量注入到当前 shell。Claude Code 的 Bash 工具每次调用是独立 shell，env 不跨调用持久化，主 session 每个 Bash call 都被迫重复 eval init，浪费 token 与轮次。

**方案**：

- `npc init` 落两份文件：`<run_dir>/run.json`（本 run 全部派生路径快照）、`<task_log_dir>/active.json`（指针指向当前 run_ts）
- 新增 `load_paths(args)` 统一 loader，按优先级 `显式参数 → cwd+active.json → NPC_* env` resolve
- 全局参数加 `--run-ts` / `--task-log-dir`，支持显式覆盖
- `--shell-exports` 标 deprecated，保留向后兼容

**收益**：主 session 模板里所有 `eval ... shell-exports` 仪式全部消失；并发 / 跨 session 接手场景下 `cat run.json` 即可恢复上下文。

### 11.2 高层 pipeline（codex / openspec 下沉）

**问题**：即便自包含化之后，LLM 仍在做大量"数据搬运"——`jq` 拆 init JSON 取 `REPO_ROOT` / `SCHEMA_PATH`、字面注入 `codex exec --cd ... --output-schema ...`、串调 `npc review parse → update-trend → check-stale → phase exit` 四步、以及 archive 段的 `precheck → openspec validate → openspec archive → git commit → phase exit → state set-progress` 六步。

**方案**：新增 `pipeline.py` 模块封装四条高层命令：

| 命令 | 替代 LLM 的工作 |
|---|---|
| `npc review run --seq N --round M` | focus render → codex exec（重试 1 次） → review parse → update-trend → phase exit（一次 IO） |
| `npc archive run --seq N` | precheck → openspec validate → openspec archive → git commit → phase exit + state set-progress |
| `npc implement record --seq N --result "..."` | 解析 RESULT 行 + summary/commit 校验 + phase exit + state set-progress |
| `npc fix record --seq N --round M --result "..."` | 同上，针对 fix 阶段 |

**关键设计**：

- pipeline 命令复用既有低层模块（`focus._round_N_template` / `review.parse_review` / `state.update_state` / `events.append_event`），不重写
- 一次 `update_state` 调用尽量完成多项装订（phase exit + trend + categories_seen），保证原子性，避免连发多条 npc 命令导致中间态
- 子进程缺失（codex / openspec / portable-timeout）单独走 exit code 4，与业务失败 exit 1 区分
- `subprocess.run` 替换 v1 的 bash + portable-timeout 拼装，超时与重试在 Python 层管控

**对 LLM 的影响**：原 ~30 行 bash 模板（codex exec + 重试 + parse + 4 步装订）压成 `R=$(npc review run --seq $SEQ --round $N)` 一行，主 session 仅看 `R.blocking` / `R.stale` 做分支。skill 模板预计可再减 30–40% 行数。

### 11.3 Sub-agent prompt 下沉

**问题**：skill 把 §A Implementer / §B Fixer 两段 prompt 模板（各 ~2500 tokens）写在 skill 文档里，主 session 用 Write 拼模板 → 写到 `$BASE/...prompt.md` → 整文件内容塞进 `Agent(prompt=...)` 字段。同一份模板在主 session context 里出现两次（Write input + Agent input），单次 sub-agent 调用注意力开销 ~5000 tokens。整 run（6 changes × 平均 4 轮）累计 ~120k tokens 浪费。

诊断要点：模板内容是**确定性资产**，不该让 LLM 主 session 充当"渲染管道"。

**方案**：新增 `templates.py` + `agent.py` 两个模块：

| 模块 | 职责 |
|---|---|
| `templates.py` | §A / §B / spawn-prompt 三个纯函数，模板内置于 CLI 包资源 |
| `agent.py` | `prompt_render` / `spawn_prompt` 两个 CLI handler，从 STATE_JSON 自包含 resolve 所有运行时变量 |

新增子命令：

- `npc agent prompt render --phase {implement|fix} --change-id CID [--round N]`：把完整模板（implement ~2.3 KB / fix ~3.5 KB）渲染到 disk，主 session 永不接触
- `npc agent spawn-prompt --phase ... --change-id CID [...]`：生成 ~150 tokens 引导语字符串，主 session 直接作为 `Agent.prompt` 字段；可选 `--extension` / `--extension-inline` 追加本次临时约束

**关键设计**：

- Fix 阶段：npc 自动从 `$BASE/round-(N-1).review.json` 抽 `in_scope && blocking` findings、从 state 取 `categories_seen / blocking_trend / implement_commit`，全部注入模板——调用方无需先跑 `npc fixer findings`，单次 `npc agent prompt render` 即完成全部 prompt 构造
- 模板字段（双产物契约、RESULT schema、修复规则 A-D）作为"项目级硬契约"内置，不暴露为可配置项；契约变更通过 npc 版本号体现
- spawn-prompt 不校验 state 中的 phase status（不要求 reviewing / in-fix-loop），保留纯字符串语义；状态机由 `npc phase enter` / record 系列管理
- `npc fixer findings` 低层命令保留供调试与自定义流程；agent 命令是其 + state 注入 + 模板渲染的高层封装

**对主 session 的影响**（实测）：

| 单次 sub-agent 调用 | 下沉前 | 下沉后 | 缩减 |
|---|---|---|---|
| Write 工具 input（模板） | ~2500 tokens | 0 | -100% |
| Agent 工具 prompt 字段 | ~2500 tokens | ~150 tokens | -94% |
| 单次合计 | ~5000 tokens | ~350 tokens（含 render/record JSON 回执） | **-93%** |

### 11.4 多引擎 review

review 管线对引擎做抽象（`engines.py`）：同一份 focus prompt 既可走 `codex exec --output-schema`（原生 schema 约束），也可走 `claude -p`（schema 内联进 prompt、再从 stdout 提取 balanced JSON）。引擎与可执行文件由配置文件（`config.py`，查找 `.npc/config.toml` → `~/.config/npc/config.toml` → `~/task_log/config.toml`）选择，`bin` 可指向自定义 claude 包装（如经 `--settings` 路由到其它后端），不绑定单一工具。

### 11.5 决策下沉与状态自愈

- **`npc auto-decide`**：`--auto` 模式下，把原本需要人机确认的触发点（continue / skip / abort）改成基于 progress / blocking 趋势的机械判定，输出 `continue-retry` / `skip` / `force-archive` / `abort`，主 session 只负责执行返回的 action，实现 fire-and-forget
- **`npc state repair`**：当 git HEAD 与 task_log 漂移（如用户 `git reset` 后 task_log 仍记录已 archived 的 commit 链），把对应 progress 退回 `pending`、openspec archive 退回 active，让后续流程能从该 seq 重新对齐，不再触碰已不存在的 commit

### 11.6 Telemetry：跨 run 指标层（1.2+）

**问题**：现有 `run.events.jsonl` / STATE_JSON / `index.jsonl` 服务于"流程恢复与单 run 摘要"，缺一层**面向自动迭代决策**的派生指标——「哪个 phase 重试率异常高、哪类 review focus 最容易翻车、哪段 prompt 在浪费 token」无法在不读 jsonl 原文的情况下回答。如果让一个 meta-agent 每周直接扫 transcript + jsonl，token 成本高且容易飘。

**方案**：分两步走，第一步先把指标层做扎实，第二步再做调度。

**第一阶段（1.2 已落地）**：

- 新增 `src/npc/telemetry.py`：`emit_event` / `iter_events` / `aggregate` / `hotspots` / `estimate_tokens_*` 五组纯函数
- 写入 `~/task_log/_telemetry/events.ndjson`（schema_version=1，详见 [cli.md §8b](./cli.md#8b-telemetry跨-run-指标流12)）；每条 record 含 `kind`、`duration_ms`、`tokens`（bytes/4 估算）、`verdict`、`blocking_count`、`retry_count`、`pointer` 反查路径
- 自动 emit 钩子内嵌到 `events.phase_exit` / `events.phase_rotate` / `pipeline._do_phase_exit` / `pipeline.run_review_round` / `pipeline.run_archive` / `agent.spawn_prompt`，对主 session **完全透明**
- 钩子层 swallow 所有 IO 异常（写 stderr warning），绝不让 telemetry 失败影响主流程
- 新增 5 个子命令：`emit / tail / agg / hotspots / estimate-tokens`，aggregates 落 `_telemetry/aggregates/by-{phase,change,week}.json`，可重建可删

**关键设计**：

- **派生指标专用**：原始事件留在 `run.events.jsonl`；telemetry 只存优化决策需要的派生量 + 指针，不复制原文。`pointer` 字段把 `state_json` / `run_events` / `summary_md` / `review_json` / `prompt_md` 全部以绝对路径暴露，meta-agent 需要详情时按需 fetch
- **review-rN / archive-done 不重复发 `phase.exit`**：由专用 kind 接管，避免 phase 计数膨胀
- **path 隔离友好**：`NPC_TELEMETRY_ROOT` 环境变量可覆盖根目录，conftest 用它把测试隔离到 tmp，生产无需配置
- **token 估算用 bytes/4**：故意不引 tiktoken，避免冷启动开销 + 第三方依赖；估算法在 record 里以 `tokens.method` 字段标注，未来可平滑切换

**第二阶段（未落地）**：在指标稳定一两周后，引入 CronCreate 调度的 meta-agent，prompt 极简：

> 读 `~/task_log/_telemetry/aggregates/*.json` 与 `npc telemetry hotspots --top 5`，输出 3 条最值得做的 npc CLI / skill 模板优化建议，不改代码，落到 `docs/optimization-proposals/YYYY-WW.md`，等用户审阅后再决定是否实施。

每次只读 < 5KB 派生数据，meta-agent 单次 token 成本可压到几千。这样既不污染主 session context，也让"自动迭代 npc"具备可审计的人在回路闸门。

### 11.7 独立包化与质量门（1.3 已落地）

v1.3 把包从 `agent_spine`（嵌套 `npc/` 子包）改名为顶层 `npc`（`src/npc/` 扁平布局，entry point `npc.cli:main`），以便作为独立仓库供其它工程复用。同版本落地：

- **`npc verify tests`**：真实复跑测试的质量门，测试命令从 `[verify] test_cmd` 配置读取，绝不裸信 LLM RESULT 行的 `tests=pass` 自报（对应 2026-06-22 提案的"复跑测试硬轨"）
- **`npc verify routing`**：架构不变量执法——便宜层（如 MiMo）只许执行不许决策/验证，检出 review 引擎被路由到便宜后端即报 violation（见 principles.md 不变量 4）
- **`npc doctor`**：环境体检，单行 JSON 输出，外部依赖缺失 exit 4
- **coder 分层（`coder.py`）**：implement/fix 的 coder 后端可按 phase 路由到 claude 或 MiMo（`scripts/spine-coder-mimo.sh`），review 引擎不受此路由影响
- **plan/git/交付类命令**：`plan check` / `plan new-change` / `git branch-for|ensure-clean|commit` / `deliver` / `pr open` / `status` / `cost` / `clean` / `spec analyze`

### 11.8 后台任务上报与观测（task / watch）

对应 2026-07-02 watch 提案，但实现走了与提案不同的路线：**不解析 Claude Code 的 transcript jsonl**（`~/.claude/projects/.../agent-*.jsonl`，内部格式无契约、防御性解析成本高），而是先落地 npc 自有的任务上报契约——后台 agent 通过 `npc task start/update/heartbeat/finish` 把生命周期写到 `<run_dir>/tasks/<task-id>.json`（+ `.events.jsonl`），`npc watch` 只观测这份自有契约。提案设想的 `npc serve` HTTP 端未实施。取舍：观测面从"Claude Code 内部状态"收窄为"显式上报的任务"，换来零逆向工程成本与格式稳定性。

### 11.9 波次并行与通知（1.4 已落地）

服务 `/new-plan-changes-v3`（波次并行 skill），把 v3 skill 原随附的脚本全部下沉进 npc：

- **`npc plan waves`**：读 changes 依赖 DAG，切成可并行的 wave 序列（原 `waves.py` 脚本下沉）
- **`npc verify manifest`**：按 sub-agent 返回的 manifest 核对文件真实落盘（存在性 + sha），防"报告了但没写"（原 `verify_manifest.py` 下沉）
- **`npc notify`**：webhook 通知（raw/slack/feishu 三种 format），供长跑 pipeline 关键节点外呼（原 `notify.py` 下沉）
- plan-only 误判修复：implement 结果只含计划不含代码时的检测收编进 npc

### 11.10 内环与整合下沉：上下文预算契约（1.5 已落地）

设计定稿见 [optimization-proposals/2026-07-05-orchestration-context-budget.md](./optimization-proposals/2026-07-05-orchestration-context-budget.md)。核心不变量：**主 session 每推进一个 change 消耗 O(1) token（实测 ~400）；磁盘为真相，context 是缓存；异常向上冒泡，状态留在下面。**

v1.4 的账目：review-fix 循环体活在 skill 里，每 change 主 session 流量 2–3k tokens，50 change 必然 compaction。而该循环的控制流本就是纯确定性的（分支只有 blocking / stale / 轮数上限 / auto-decide action）。1.5 按 §11.2 的下沉逻辑推到底：

- **`npc change run`（change.py）**：单 change 内环状态机，复用 coder/pipeline/auto-decide 不重写。决策点分档——`--auto` 内部裁定一路跑完；交互档 exit 5 + `pending_decision` 装订，`--decision` 消费续跑。人驾驭的粒度从"盯每一轮"提升到"只在分叉点出场"。
- **`npc integrate`（integrate.py）**：v3 skill Step 9 的整合伪 bash 段（cherry-pick + sed 换 hash + record + verify tests + revert）单命令化，失败自动收拾现场、main 保持绿。
- **`npc status --brief` + `npc state note`**：compaction/续跑单命令重入契约（pending_decisions / 未消费 notes / next_action）+ steering 通道（notes.jsonl 追加式，state.notes_consumed_at 水位消费）。
- **`npc verify tasks`**：task 维度只暴露派生计数（done/total × 自报交叉验证），清单绝不进主 context。
- **telemetry `deviation` 记账**：change run 决策点 / integrate 冲突与 revert / auto-decide --apply 自动落 record（trigger/action/layer/cost_rounds/decided_by）——按宪法"先收证据后建轨"，归因升级阶梯等未来硬轨由这些数据的 hotspots 决定是否值得建。
- **`init-run --goal` + summary Goal Coverage**：run 级验收留给人（人驾驭定位），机器只产对照表。
- **new-plan-changes-v4 skill**：v3 的上下文预算重构版（~120 行纯决策文档），波次循环每 change 三条命令（spawn / integrate / change run），含 re-plan 触发点（cherry-pick 冲突暴露 DAG 漏边 → 对剩余集合重跑 waves）与 triage 纪律（失败细节走只读 sub-agent + pointer，绝不 cat 日志）。

缓建清单（触发条件见提案 §4）：自动验收 agent、归因升级阶梯 L2/L3、自适应 stale 阈值、预算控制器、fresh review。
