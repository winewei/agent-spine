# npc CLI 契约 v1.2

本文件定义 `npc` 命令行工具的稳定对外接口。所有命令默认：

- **stdout**：单行 JSON 对象（除非另有说明）
- **stderr**：人类可读消息 / 警告 / 错误详情
- **exit code**：
  - `0` 成功
  - `1` 业务失败（含 validation、pipeline 业务失败）
  - `2` 用法错误
  - `3` 环境错误（缺文件、git 不存在、未找到 run.json）
  - `4` 外部依赖缺失（codex / openspec / portable-timeout 未安装）
- **中文**：错误消息默认中文（与项目 CLAUDE.md 对齐）

## 0. 通用：运行时上下文 + 全局参数

### 0.1 运行时上下文：`run.json` + `active.json`（0.2+）

从 0.2 起，子命令**自包含 resolve** 运行时上下文，不再依赖 shell 环境变量。`npc init` 落盘两份文件：

| 文件 | 位置 | 内容 |
|---|---|---|
| `run.json` | `<run_dir>/run.json` | 该 run 的全部 deterministic 元数据（repo_root / proj_key / 各派生路径）|
| `active.json` | `<task_log_dir>/active.json` | `{"current_run_ts": "<ts>"}`，指向当前 active run |

子命令的 resolve 优先级：

1. 显式参数（`--run-ts` / `--task-log-dir` / `--state-json`）
2. `cwd → git toplevel → task_log_dir → active.json → run.json`
3. `NPC_*` 环境变量（0.1 兼容路径，仍可用）

只要任意一级 resolve 成功，命令即可工作。**`eval "$(npc init --shell-exports)"` 仪式已废弃**，但 `--shell-exports` 仍向后兼容（输出时向 stderr 打 deprecation warning）。

### 0.2 NPC_* 环境变量（0.1 兼容路径，仍可用）

| 变量 | 含义 |
|---|---|
| `NPC_REPO_ROOT` | 工程根（git toplevel）|
| `NPC_PROJ_KEY` | 工程 key（path 中 `/` → `-`）|
| `NPC_TASK_LOG_DIR` | `~/task_log/<PROJ_KEY>` |
| `NPC_RUN_TS` | 本 run 时间戳 |
| `NPC_RUN_DIR` | `$NPC_TASK_LOG_DIR/$NPC_RUN_TS` |
| `NPC_STATE_JSON` | `$NPC_TASK_LOG_DIR/$NPC_RUN_TS-plan-state.json` |
| `NPC_STATE_MD` | `$NPC_TASK_LOG_DIR/$NPC_RUN_TS-plan-state.md` |
| `NPC_INDEX_FILE` | `$NPC_TASK_LOG_DIR/index.jsonl` |
| `NPC_SCHEMA_PATH` | `~/task_log/.new-plan-review-schema.json` |
| `NPC_RUN_EVENTS` | `$NPC_RUN_DIR/run.events.jsonl` |

### 0.3 全局参数

- `--state-json PATH` 覆盖 state_json 字段（调试用 / 多 run 调试）
- `--run-ts TS` 显式指定 run timestamp（跳过 active.json 探测）
- `--task-log-dir PATH` 显式指定 task_log_dir（跳过 cwd → repo_root 推导）
- `--version` 输出版本号到 stdout（纯字符串、非 JSON）
- `--help` / `-h` 输出帮助

---

## 1. 初始化与续跑

### `npc init [--auto] [--fresh] [--shell-exports]`

初始化本次 run 的运行环境。

**做什么**：

1. 探测 `REPO_ROOT`（`git rev-parse --show-toplevel`），失败 exit 3
2. 计算 `PROJ_KEY / TASK_LOG_DIR / RUN_TS / RUN_DIR / STATE_JSON / STATE_MD / INDEX_FILE / SCHEMA_PATH / RUN_EVENTS`
3. `mkdir -p` 所有需要的目录
4. **写 `<run_dir>/run.json`**（0.2+，含本次 run 的全部派生路径快照）
5. **写 `<task_log_dir>/active.json`**（0.2+，指针指向本次 run_ts；原子写）
6. 自举 `~/task_log/.new-plan-review-schema.json`（不存在时写）
7. 自举 `~/.local/bin/portable-timeout`（不存在或非可执行时写）
8. 识别当前 cc session_id（mtime 启发 + by-cwd hook 兜底，详见 `session` 模块）
9. **不写 STATE_JSON 文件**——由后续 `npc state init-run` 或 plan 流程内首次 `state add-change` 触发写入；`init` 仅负责"环境检测 + 路径计算 + 上下文落盘"
10. 探测续跑：扫 `$TASK_LOG_DIR/*-plan-state.json` 找 `status=in-progress` 最新一份；若存在且非 `--fresh`，输出 `needs_resume: true` 与候选 `STATE_JSON` 路径（同时复用旧 run 的 `run_ts`，使后续命令仍能 resolve 到旧 run）

**参数**：

- `--auto` 标记 auto 模式（写进后续 STATE_JSON 的 `mode` 字段时使用）
- `--fresh` 忽略任何 in-progress 旧 run，强制新建
- `--shell-exports` [**Deprecated 0.2**] 输出格式从 JSON 改为 `export KEY=VALUE` 行；调用方仍可 `eval`，但 0.2+ 子命令已自包含，不再需要。该模式触发时会向 stderr 打 deprecation warning

**stdout（默认 JSON 模式）**：

```json
{
  "repo_root": "/Users/you/code/foo",
  "proj_key": "-Users-you-code-foo",
  "task_log_dir": "/Users/you/task_log/-Users-you-code-foo",
  "run_ts": "2026-05-22-1545",
  "run_dir": "/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545",
  "state_json": "/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545-plan-state.json",
  "state_md": "/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545-plan-state.md",
  "index_file": "/Users/you/task_log/-Users-you-code-foo/index.jsonl",
  "schema_path": "/Users/you/task_log/.new-plan-review-schema.json",
  "run_events": "/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545/run.events.jsonl",
  "run_json": "/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545/run.json",
  "active_json": "/Users/you/task_log/-Users-you-code-foo/active.json",
  "session_id": "018f5c4a-...-9b3e",
  "transcript_path": "/Users/you/.claude/projects/.../018f5c4a.jsonl",
  "session_source": "mtime-1min",
  "needs_resume": false,
  "resume_state_json": null,
  "mode": "auto",
  "fresh": false
}
```

**stdout（`--shell-exports` 模式）**：

```
export NPC_REPO_ROOT='/Users/you/code/foo'
export NPC_PROJ_KEY='-Users-you-code-foo'
export NPC_TASK_LOG_DIR='/Users/you/task_log/-Users-you-code-foo'
export NPC_RUN_TS='2026-05-22-1545'
export NPC_RUN_DIR='/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545'
export NPC_STATE_JSON='/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545-plan-state.json'
export NPC_STATE_MD='/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545-plan-state.md'
export NPC_INDEX_FILE='/Users/you/task_log/-Users-you-code-foo/index.jsonl'
export NPC_SCHEMA_PATH='/Users/you/task_log/.new-plan-review-schema.json'
export NPC_RUN_EVENTS='/Users/you/task_log/-Users-you-code-foo/2026-05-22-1545/run.events.jsonl'
export NPC_SESSION_ID='018f5c4a-...-9b3e'
export NPC_TRANSCRIPT_PATH='/Users/you/.claude/projects/.../018f5c4a.jsonl'
export NPC_SESSION_SOURCE='mtime-1min'
export NPC_NEEDS_RESUME='false'
export NPC_RESUME_STATE_JSON=''
```

续跑场景：当 `needs_resume=true` 时，`NPC_STATE_JSON` 指向旧 run 的 state 文件（不是新建的）；`NPC_RUN_TS / NPC_RUN_DIR` 也指向旧 run。

**stderr**：警告（如 cc projects 目录缺失、portable-timeout 自举成功提示）

**exit**：`0` 正常；`3` 非 git 仓库或缺 `~/.claude/projects/<PROJ_KEY>` 目录（仅警告，仍出环境变量；exit 仍是 0）

---

### `npc resume detect`

详细的续跑断点判定。仅在 `npc init` 报告 `needs_resume=true` 后调用。

**stdout**：

```json
{
  "needs_resume": true,
  "state_json": "/Users/you/task_log/.../2026-05-21-2030-plan-state.json",
  "last_updated_at": "2026-05-21T20:45:11+08:00",
  "completed_changes": 5,
  "total_changes": 12,
  "next_seq": 6,
  "next_change_id": "add-bigtable-reader",
  "next_phase": "fix-r3",
  "current_round": 2,
  "blocking_trend": [5, 4, 4]
}
```

`next_phase` 取值见"phase enter / exit"段。

---

## 2. State 读写

### `npc state init-run --plan-order '<json-array>'`

首次创建本次 run 的 STATE_JSON / STATE_MD。`plan-order` 是 `["change-id-1", "change-id-2", ...]` 形式的 JSON 数组。

**做什么**：

1. 写完整 STATE_JSON header（schema_version=2 / run_ts / started_at / mode / fresh / status="in-progress" / project_root / proj_key / git_head_at_start / cc_session / plan_order / progress=[每项 status=pending, phases={}]）
2. 渲染并写 STATE_MD
3. touch `$NPC_RUN_EVENTS`

**stdout**：

```json
{"ok": true, "state_json": "...", "total_changes": 3}
```

---

### `npc state get <jq-path>`

取 STATE_JSON 中某字段。`<jq-path>` 是合法 jq 路径表达式。

**例**：

```bash
$ npc state get '.progress[0].status'
"pending"

$ npc state get '.plan_order | length'
3
```

**stdout**：jq 表达式的求值结果（JSON）

**exit**：`0` 成功；`1` jq 表达式错或字段不存在

---

### `npc state set-progress <seq> [options]`

更新某 change 的 progress 条目。`<seq>` 是 1-based 序号。

**参数**（按需传，全部可选；只更新传入的字段）：

- `--status STATUS` — 取值：pending/implementing/reviewing/in-fix-loop/archived/failed/needs-user-decision/skipped-auto
- `--reason TEXT` — 配合 status=failed/needs-user-decision/skipped-auto 使用
- `--implement-commit HASH`
- `--archive-commit HASH`
- `--total-rounds N`
- `--stale-verdict TEXT` — 取值：stale / null

**做什么**：

1. 读 STATE_JSON
2. 更新 `progress[seq-1]` 指定字段，同时更新 `last_updated_at`
3. 原子写回 STATE_JSON
4. 重新渲染并写 STATE_MD

**stdout**：

```json
{"ok": true, "seq": 2, "status": "in-fix-loop"}
```

---

### `npc state finalize`

收尾：根据所有 progress[].status 自动判定顶层 status。

**做什么**：

1. 读 STATE_JSON
2. 计算顶层 status：
   - 全部 archived → `completed`
   - 部分 archived 但有 failed/skipped-auto → `completed-with-issues`
   - 任何 needs-user-decision → 不动 status（保持 in-progress），exit 1 报错
3. 写回 STATE_JSON + STATE_MD

**stdout**：

```json
{
  "ok": true,
  "final_status": "completed-with-issues",
  "archived": 14,
  "failed": 1,
  "skipped": 1,
  "total": 16
}
```

**exit**：`0` 成功；`1` 存在 needs-user-decision，不能 finalize

---

## 3. Phase 计时与事件

### `npc phase enter <seq> <phase>`

进入某 phase。

**`<phase>`** 取值：`implement` / `review-r0` / `fix-rN` / `review-rN` (N≥1) / `archive`

**做什么**：

1. 读 STATE_JSON
2. 设置 `progress[seq-1].phases.<phase> = {status:"in-progress", started_at:<now-iso>, started_ms:<now-ms>}`
   - `started_ms` 用于 exit 时计算 duration_ms（写后即用，最终落盘可保留）
3. 写回 STATE_JSON + STATE_MD
4. 追加 `phase.start` 事件到 `<base>/events.jsonl` 与 `$NPC_RUN_EVENTS`

事件 schema：

```json
{"event":"phase.start","ts":"<iso>","change_seq":<n>,"change_id":"<id>","phase":"<p>"}
```

`<base>` 取自 `progress[seq-1].base`；若不存在则计算 `$NPC_RUN_DIR/<NNN>-<change_id>` 并 mkdir + 写入 progress.base。

**stdout**：

```json
{"ok": true, "seq": 2, "phase": "review-r0", "base": "/.../002-add-foo", "started_at": "2026-05-22T15:50:11+08:00"}
```

---

### `npc phase exit <seq> <phase> --status done|failed [--extra '<json-obj>']`

退出某 phase，自动计算 duration_ms。

**参数**：

- `--status done|failed`（必填）
- `--extra '<json-obj>'`：合并到事件与 phases 字段的额外信息，如 `'{"commit":"abc1234","tasks":8,"tests":"pass"}'`

**做什么**：

1. 读 STATE_JSON 取 `progress[seq-1].phases.<phase>.started_ms`
2. `duration_ms = now_ms - started_ms`
3. 更新 `progress[seq-1].phases.<phase>` 为 `{status, done_at, duration_ms, ...extra}`（删除 started_ms 临时字段）
4. 写回 STATE_JSON + STATE_MD
5. 追加事件到 events.jsonl + RUN_EVENTS：

```json
{
  "event": "<phase-base>.done",  // implement.done / review.done / fix.done / archive.done
  "ts": "<iso>",
  "change_seq": <n>,
  "change_id": "<id>",
  "phase": "<phase>",
  "duration_ms": <n>,
  ...extra
}
```

`<phase-base>` 推导：`implement` → `implement`；`review-rN` → `review`；`fix-rN` → `fix`；`archive` → `archive`。
status=failed 时事件后缀改 `.failed`。

**stdout**：

```json
{"ok": true, "seq": 2, "phase": "review-r0", "duration_ms": 75000, "status": "done"}
```

---

## 4. Plan 登记

### `npc state add-change <seq> <change_id> [--base PATH]`

向 progress 数组追加一个 change 条目（pending 初态）。

如未提供 `--base`，自动计算 `$NPC_RUN_DIR/<NNN>-<change_id>` 并 mkdir。

**stdout**：

```json
{"ok": true, "seq": 3, "change_id": "add-bigtable-reader", "base": "/.../003-add-bigtable-reader"}
```

---

## 5. Review 解析与 trend

### `npc review parse <review.json>`

派生 review 的核心指标。

**做什么**：

1. 读 review.json（schema-validated 输出）
2. 计算：
   - `blocking` = count(findings where severity∈{critical,high} and in_scope=true)
   - `advisory` = count(findings where severity∈{medium,low} or in_scope=false)
   - `verdict` = review.json.verdict
   - `categories` = distinct findings[].category（按出现顺序）

**stdout**：

```json
{
  "verdict": "changes-requested",
  "blocking": 5,
  "advisory": 2,
  "categories": ["validation", "concurrency"],
  "blocking_findings": [
    {"id":"F1","severity":"critical","category":"validation","title":"...","file":"...","line_range":"42-58"},
    ...
  ]
}
```

`blocking_findings` 是 in_scope=true 且 severity∈{critical,high} 的列表（按 id 排序），供 Fixer prompt 使用。

---

### `npc review update-trend <seq> --metrics '<json-from-review-parse>'`

更新 progress[seq-1] 的 trend 字段。

**做什么**：

1. 读 STATE_JSON 取当前 `progress[seq-1].blocking_trend` 数组
2. 追加 `metrics.blocking` 到 blocking_trend
3. 比较新值与上一值：
   - 严格下降 → `rounds_since_strict_decrease = 0`
   - 持平或上升 → `rounds_since_strict_decrease += 1`
   - 首轮（trend 原本空）→ `rounds_since_strict_decrease = 0`
4. 合并 `metrics.categories` 到 `categories_seen`（去重，保序）
5. 写回 STATE_JSON + STATE_MD

**stdout**：

```json
{
  "ok": true,
  "blocking_trend": [5, 4, 4],
  "rounds_since_strict_decrease": 1,
  "categories_seen": ["validation", "concurrency"]
}
```

---

### `npc review check-stale <seq>`

检查 stale 判定。

**做什么**：读 STATE_JSON.progress[seq-1].rounds_since_strict_decrease，判定 ≥ 3 视为 stale。

**stdout**：

```json
{
  "stale": false,
  "rounds_since_strict_decrease": 1,
  "blocking_trend": [5, 4, 4],
  "threshold": 3
}
```

---

## 6. Focus 文本渲染

### `npc focus render --round N --change-id ID --implement-commit HASH --output PATH [--project-context PATH]`

渲染 codex review focus 文本到文件。

**参数**：

- `--round N`（必填）
- `--change-id ID`（必填）
- `--implement-commit HASH`（round-N N≥1 必填）
- `--output PATH`（必填，输出文件路径）
- `--project-context PATH`（可选；若未传，自动从 `<repo>/openspec/project.md` 与 `<repo>/CLAUDE.md` 抽 "评审重点"/"威胁模型"/"Review Context"/"Threat Model" 章节；都无则用默认中性约束）

**stdout**：

```json
{"ok": true, "output": "/.../round-0.focus.md", "bytes": 2341, "project_context_source": "openspec/project.md"}
```

`project_context_source` 取值：`openspec/project.md` / `CLAUDE.md` / `both` / `default`

---

## 7. Fixer findings 摘录

### `npc fixer findings --review PATH --output-fragment PATH`

从 review.json 抽 in_scope=true 且 severity∈{critical,high} 的 findings，输出为 markdown 片段（每条一个 H2 段落），供 Fixer prompt 拼接。

输出文件格式：

```markdown
## F1 — [critical][validation] 输入参数未做长度校验
File: src/handlers/auth.go:42-58
Detail: 函数 ValidateInput 接收用户传入的 username 但未限制长度...
Recommendation: 添加 len(username) > 256 的判定...

## F2 — [high][concurrency] 锁未释放路径
...
```

**stdout**：

```json
{"ok": true, "output": "...", "count": 5, "categories": ["validation", "concurrency"]}
```

---

## 7a. Sub-agent Prompt 渲染（1.0+）

历史上 §A Implementer / §B Fixer 模板住在 skill 文档里，主 session 拼模板、Write 到 prompt 文件、再把整文件内容塞进 `Agent(prompt=...)` 字段——同一份 ~2500 tokens 的模板在主 session context 里出现两次（Write + Agent）。1.0 把模板下沉到 CLI 包资源（`agent_spine.npc.templates`），主 session 仅承担 ~150 tokens 的薄引导语。

### `npc agent prompt render --phase {implement|fix} --change-id ID [...]`

把完整模板渲染到 `$BASE/...prompt.md`，主 session 永不接触模板内容。

**参数**：

- `--phase {implement|fix}` 必传
- `--change-id ID` 必传；npc 按 change_id 在 `STATE_JSON.progress[]` 查 seq
- `--seq N` 可选；若给，与 state 中的 seq 必须一致（否则 exit 1）
- `--round N` fix 阶段必传；implement 阶段不允许（否则 exit 2）
- `--output PATH` 可选；默认 `$BASE/implement.prompt.md` 或 `$BASE/round-N.fix.prompt.md`
- `--review-json PATH` fix 可选；默认 `$BASE/round-(N-1).review.json`
- `--implement-commit HASH` fix 可选；默认从 `progress[].implement_commit` 取

**做什么**：

1. 从 STATE_JSON resolve seq / base / categories_seen / blocking_trend / implement_commit
2. **implement**：渲染 §A 模板（含 Runtime Variables / 必读输入 / 双产物契约 / RESULT schema）
3. **fix**：从 `--review-json` 抽 `in_scope=true && severity ∈ {critical,high}` 的 findings，渲染 §B 模板（含 Root-cause 全落点扫描 + 真实回归 + Self-Check 规则）
4. 写到 output 路径

**stdout（implement）**：

```json
{
  "ok": true,
  "phase": "implement",
  "seq": 1,
  "change_id": "add-foo",
  "output": "/Users/you/task_log/.../001-add-foo/implement.prompt.md",
  "bytes": 2344,
  "template_version": "1.0.0"
}
```

**stdout（fix）**：implement 的字段 + `round` / `blocking_count` / `review_json` / `implement_commit`。

**exit**：`0` 成功；`1` state 不一致 / review.json schema 错；`2` 用法错（缺 --round、implement 阶段传 --round、--implement-commit 缺失）；`3` 环境错（缺 state / review.json）

---

### `npc agent spawn-prompt --phase ... --change-id ID [...]`

生成给 Claude `Agent` 工具 `prompt` 字段用的薄引导语字符串（含 prompt 文件绝对路径 + 可选 extension）。

**参数**：

- `--phase {implement|fix}` 必传
- `--change-id ID` 必传
- `--seq N` 可选；同 render
- `--round N` fix 必传
- `--prompt-file PATH` 可选；默认按 phase/round 推算 `$BASE/...prompt.md`
- `--extension PATH` 可选；额外约束文件，内容追加到引导语
- `--extension-inline TEXT` 可选；直接传 extension 文本（与 `--extension` 互斥）

**做什么**：

1. 从 state resolve seq + base
2. 校验 prompt_file 存在（否则 exit 3 + `prompt_file_not_found`）
3. 拼引导语：`请先用 Read 读取并严格按 <abs-path> 里的指令执行……`，可选追加 `## 本次追加约束`

**stdout**：

```json
{
  "ok": true,
  "phase": "implement",
  "seq": 1,
  "change_id": "add-foo",
  "prompt": "请先用 Read 工具读取并严格按 /abs/path/implement.prompt.md 里的指令执行任务……",
  "prompt_file": "/abs/path/implement.prompt.md",
  "has_extension": false,
  "bytes": 287
}
```

**调用方式**：

```bash
SPAWN=$(npc agent spawn-prompt --phase implement --change-id add-foo)
PROMPT_TEXT=$(echo "$SPAWN" | jq -r '.prompt')
# 主 session 直接调用 Agent 工具：
# Agent(subagent_type="senior-code-developer", description="Implement add-foo", prompt=$PROMPT_TEXT)
```

**exit**：`0` 成功；`2` 用法错（缺 round / 互斥参数同时给）；`3` prompt_file 或 extension 文件不存在

**注意**：spawn-prompt 自身不校验 STATE_JSON 中 phase 的状态（不要求是 reviewing / in-fix-loop），由 `npc phase enter` 等命令负责状态机；spawn-prompt 仅负责"取 prompt 文件路径 + 拼引导语"的纯字符串操作。

---

## 8. Archive 前校验

### `npc archive precheck <seq>`

检查 commit chain 是否完整。

**做什么**：

1. 读 STATE_JSON 取 `progress[seq-1].implement_commit` + 所有 `phases.fix-rN.commit`
2. 对每个 commit `git merge-base --is-ancestor <c> HEAD`
3. 收集失败者

**stdout（成功）**：

```json
{"ok": true, "expected": ["abc1234", "def5678"], "missing": []}
```

**stdout（失败）**：

```json
{"ok": false, "expected": ["abc1234", "def5678", "ef91234"], "missing": ["ef91234"]}
```

**exit**：`0` chain 完整；`1` 有缺失（仍打印 JSON，方便 shell 后续 jq 取 missing）

---

## 8a. Pipeline 高层命令（0.3+）

把 codex/openspec 子进程编排 + 多步状态装订打包成单条命令；LLM 仅看一行 JSON 输出做决策。

### `npc review run --seq N --round M [--retries 1] [--timeout 900] [--codex-bin PATH] [--portable-timeout PATH]`

完整一轮 codex review。

**做什么**：

1. 读 STATE_JSON 取 `progress[seq-1]` → change_id / base / implement_commit（round>0 用）
2. 调用 focus 模板渲染 `<base>/round-N.focus.md`
3. `phase enter review-rN`
4. `<portable-timeout> <timeout> codex exec --cd <repo_root> --sandbox read-only --skip-git-repo-check -c model_reasoning_effort=high --output-schema <schema_path> -o <base>/round-N.review.json --json -`（stdin = focus.md；stdout → events.jsonl；stderr 合并到 stdout）
5. exit≠0 或 review.json 非合法 JSON → 删两文件，**重试 `--retries` 次**；仍失败：`phase exit review-rN failed`，返回 `{ok:false, error:"codex-exec-failed", ...}`
6. parse review → metrics（verdict / blocking / advisory / categories / blocking_findings）
7. **一次 `update_state`** 完成：`phase exit review-rN done`（带 metrics）+ update_trend + categories_seen 合并
8. blocking>0 时自动渲染下一轮 fix.findings：`<base>/round-(N+1).fix.findings.md`

**stdout（成功）**：

```json
{
  "ok": true,
  "seq": 1,
  "round": 0,
  "change_id": "add-config-loader",
  "verdict": "changes-requested",
  "blocking": 2,
  "advisory": 1,
  "categories": ["validation"],
  "stale": false,
  "rounds_since_strict_decrease": 0,
  "blocking_trend": [2],
  "review_json": "/.../round-0.review.json",
  "events_path": "/.../round-0.events.jsonl",
  "focus_path": "/.../round-0.focus.md",
  "findings_path": "/.../round-1.fix.findings.md",
  "project_context_source": "openspec/project.md"
}
```

**stdout（失败）**：

```json
{
  "ok": false,
  "seq": 1,
  "round": 0,
  "error": "codex-exec-failed",
  "detail": "exit_code=1",
  "attempts": 2,
  "events_path": "/.../round-0.events.jsonl"
}
```

**exit**：`0` 成功；`1` 业务失败（codex / review schema 失败）；`4` 依赖缺失（codex / portable-timeout 未找到）

---

### `npc archive run --seq N [--openspec-bin PATH]`

archive 一站式：precheck → openspec validate --strict → openspec archive --yes → git commit → 状态装订。

**做什么**：

1. `phase enter archive`
2. `archive precheck`（commit chain 完整性）；失败 → `phase exit failed reason=commit-chain-broken` + `state set-progress status=failed`
3. `openspec validate <change_id> --strict`；失败 → 同上 reason=openspec-validate
4. `openspec archive <change_id> --yes`；失败 → reason=openspec-archive
5. `git add openspec/` + `git commit -m "chore: archive <change_id>"`；失败 → reason=git-commit
6. 取 HEAD = archive_commit；统计 total_rounds（最大 review-rN 索引）
7. `phase exit archive done` + `state set-progress status=archived archive_commit=... total_rounds=...`

**stdout（成功）**：

```json
{
  "ok": true,
  "seq": 1,
  "change_id": "add-config-loader",
  "archive_commit": "5b33379...",
  "total_rounds": 2,
  "final_status": "passed (round 2)"
}
```

**stdout（失败）**：

```json
{
  "ok": false,
  "seq": 1,
  "change_id": "add-config-loader",
  "error": "openspec-validate-failed",
  "stderr_tail": "...错误尾段..."
}
```

`error` 取值：`commit-chain-broken` / `openspec-validate-failed` / `openspec-archive-failed` / `git-commit-failed`。

**exit**：`0` 成功；`1` 业务失败；`4` 依赖缺失（openspec 未安装）

---

### `npc implement record --seq N (--result "<RESULT 行>" | --result-file PATH) [--no-summary-check]`

喂入 implementer sub-agent 的 RESULT 行，完成 `phase exit implement` + `state set-progress`。

**RESULT 行格式**：

```
RESULT: commit=<hash> tasks=<n> tests=<pass|fail> summary=<path> notes=<一行说明，无则填 ->
```

失败时：

```
RESULT: commit=- tasks=<已完成数> tests=fail summary=<path or -> notes=<关键错误>
```

**做什么**：

1. 解析 RESULT 行（key=value 切分，value 可含空格直到下一个 `key=`）
2. 校验：`commit != "-" && tests == "pass"`，否则 → `phase exit implement failed` + `state set-progress status=failed reason=implementer`
3. 校验 summary 文件存在（除非 `--no-summary-check`）；失败 → reason=summary-missing
4. 校验 commit 存在于 repo（`git cat-file -e`）；失败 → reason=commit-not-found
5. `phase exit implement done` 含 `{commit, tasks, tests, summary}` + `state set-progress status=reviewing implement_commit=<hash>`

**stdout（成功）**：

```json
{
  "ok": true,
  "seq": 1,
  "change_id": "add-config-loader",
  "commit": "e05d530...",
  "tasks": 8,
  "tests": "pass",
  "summary": "/.../implement.summary.md"
}
```

**exit**：`0` 成功；`1` 业务失败；`2` 参数错（既无 --result 又无 --result-file）

---

### `npc fix record --seq N --round M (--result "<RESULT 行>" | --result-file PATH) [--no-summary-check]`

喂入 fixer sub-agent 的 RESULT 行，针对 `fix-rN` 阶段。

**RESULT 行格式**：

```
RESULT: commit=<hash> fixed=<n> tests=<pass|fail> summary=<path> categories_scanned=<csv> regressions_added=<csv|-> notes=<...>
```

失败时与 implement record 类似（commit=-、tests=fail、summary 缺失、commit 不存在均会触发 phase exit failed）。

校验通过后：
- `phase exit fix-rN done` 含 `{commit, fixed, tests, summary, categories_scanned, regressions_added}`
- `state set-progress status=in-fix-loop`（等下一轮 review）

**stdout（成功）**：

```json
{
  "ok": true,
  "seq": 1,
  "round": 1,
  "change_id": "add-config-loader",
  "commit": "582e481...",
  "fixed": 2,
  "tests": "pass",
  "summary": "/.../round-1.fix.summary.md"
}
```

**exit**：`0` 成功；`1` 业务失败；`2` 参数错

---

## 8b. Telemetry：跨 run 指标流（1.2+）

设计目标见 `docs/design.md > Telemetry 第一阶段`。本节给出契约。

### 8b.1 文件布局

```
~/task_log/_telemetry/
├── events.ndjson           # append-only 派生指标流（一行一 JSON）
├── schema-v1.json          # 字段契约（首次写入时由 CLI 自动拷一份）
└── aggregates/
    ├── by-phase.json
    ├── by-change.json
    └── by-week.json
```

- 主 session **永远不读** `events.ndjson` 原文；只读 `aggregates/*.json` 与 `npc telemetry hotspots` stdout。
- 路径可通过环境变量 `NPC_TELEMETRY_ROOT` 覆盖（测试 / 隔离用）。

### 8b.2 record 字段（schema_version=1）

| 字段 | 类型 | 备注 |
|---|---|---|
| `schema_version` | int | 固定 `1` |
| `ts` | ISO 8601 | 含本地时区偏移 |
| `kind` | enum | `phase.exit` / `review.round` / `archive.done` / `agent.spawn` / `agent.timeout` |
| `proj_key` | string | 工程 mangle key |
| `run_ts` | string \| null | YYYY-MM-DD-HHMM |
| `change_seq`, `change_id`, `phase` | 可空 | |
| `status` | enum | `done` / `failed` |
| `duration_ms` | int \| null | |
| `tokens` | object \| null | `{prompt_bytes, output_bytes, est_input_tokens, est_output_tokens, method}`；估算法默认 `bytes_div_4` |
| `verdict` | enum | review.round 专用：`pass` / `should-fix` / `must-fix` |
| `blocking_count`, `blocking_categories` | review.round 专用 | |
| `engine` | string | review.round 专用：`codex` / `claude` |
| `retry_count` | int | review codex 重试次数 |
| `outcome_reason` | string \| null | failed 时的 reason |
| `archive_commit`, `total_rounds` | archive.done 专用 | |
| `pointer` | object | `{state_json, run_events, per_change_events, summary_md, review_json, focus_md, prompt_md}` 绝对路径 |

完整 JSON Schema：`src/agent_spine/npc/telemetry_schema_v1.json`（与首次写入时拷出的 `_telemetry/schema-v1.json` 内容一致）。

### 8b.3 自动 emit 时机

| 现有流程 | 触发 kind |
|---|---|
| `pipeline._do_phase_exit`（implement / fix-rN / archive failed） | `phase.exit` |
| `events.phase_exit` / `events.phase_rotate`（CLI 低层调用同口径） | `phase.exit` |
| `pipeline.run_review_round`（成功与失败均发 1 条） | `review.round` |
| `pipeline.run_archive`（成功路径） | `archive.done` |
| `agent.spawn_prompt`（生成引导语之后） | `agent.spawn` |

约束：review-rN / archive done **不重复发** `phase.exit`，由专用 kind 接管，避免 phase 计数膨胀。

任何 emit 失败都被 swallow（写 stderr warning），不影响主流程。

### 8b.4 子命令

#### `npc telemetry emit`

```
npc telemetry emit --kind <K> [--seq N] [--change-id CID] [--phase X] [--status done|failed] \
                   [--duration-ms N] [--proj-key K] [--run-ts TS] [--extra '<JSON>']
```

手动追加一条 record（排错用）。`--proj-key` 缺省时按 cwd → repo_root 推。`--extra` 合并到 record（与已有字段同名 key 不覆盖）。stdout：

```json
{"ok":true,"kind":"phase.exit","path":"<events.ndjson 绝对路径>"}
```

#### `npc telemetry tail`

```
npc telemetry tail [--kind K] [--last N=20]
```

输出最近 N 条 record（可按 kind 过滤）。stdout：

```json
{"ok":true,"count":N,"total":过滤后总数,"events":[...]}
```

#### `npc telemetry agg`

```
npc telemetry agg [--by phase|change|week] [--since 7d|24h|30m|ISO] [--no-write]
```

`--by` 省略时三个维度全跑；`--no-write` 只输出 stdout 不写 `aggregates/`。

每个维度返回：`{count, done, failed, failure_rate, duration_ms{p50,p95,max,sum}, est_input_tokens_sum, est_output_tokens_sum, retry_count_sum, blocking_total, review_rounds, kinds, reasons, verdicts}`。

#### `npc telemetry hotspots`

```
npc telemetry hotspots [--top N=5] [--since DUR]
```

按 `(failure_rate + 0.1) × (p50_duration_ms + 1) × (1 + retry_count_sum)` 排序，给出最值得优化的前 N 个 phase。`+0.1` / `+1` 是常数项，防止全成功 phase 永远 score=0 把高 retry 的项压住。stdout：

```json
{
  "ok": true,
  "since": "7d",
  "top": 5,
  "events_considered": 42,
  "hotspots": [
    {"phase":"fix-r0","score":33000.0,"count":2,"failure_rate":1.0,
     "p50_duration_ms":55000,"p95_duration_ms":60000,"retry_count_sum":3,
     "top_reasons":[["fixer",2]],"top_verdicts":[]}
  ]
}
```

#### `npc telemetry estimate-tokens <file>`

单文件 token 估算（bytes ÷ 4）。返回 `{ok, file, bytes, est_tokens, method}`。

### 8b.5 与第二阶段 meta-agent 的衔接

`aggregates/*.json` 与 `npc telemetry hotspots` 输出是后续 meta-agent 的唯一输入：meta-agent 不读 `events.ndjson`、不读 transcript、不读 jsonl 原文。这样每次自动迭代只需消耗 < 5KB 派生数据。

---

## 9. 收尾

### `npc summary render`

从 STATE_JSON + RUN_EVENTS 派生 `$NPC_RUN_DIR/run-summary.md`。

**stdout**：

```json
{"ok": true, "output": "/.../run-summary.md", "duration_ms": 9132000, "archived": 14, "failed": 1, "skipped": 1}
```

---

### `npc index append`

追加一行到 `$NPC_INDEX_FILE`，记录本 run 概要（供跨 run 学习）。

**stdout**：

```json
{"ok": true, "index_file": "...", "appended_line_bytes": 421}
```

---

## 10. 出错语义示例

所有命令失败时：

- stdout 仍输出 JSON `{"ok": false, "error": "<code>", "message": "<中文消息>"}`（除 `--shell-exports` 模式直接退出 1）
- stderr 输出补充上下文
- exit code 按本文件约定（业务失败 1 / 用法 2 / 环境 3）

示例：

```bash
$ npc state set-progress 99 --status archived
{"ok": false, "error": "seq_out_of_range", "message": "seq=99 超出 progress 数组长度 (total=3)"}
$ echo $?
1
```

---

## 11. 调用顺序参考（在 skill v2 中，1.0 推荐写法）

```bash
# 1. 初始化（落 run.json + active.json；不再 eval shell exports）
INIT=$(npc init ${AUTO:+--auto} ${FRESH:+--fresh})
NEEDS_RESUME=$(echo "$INIT" | jq -r '.needs_resume')

# 2. 判定续跑
if [ "$NEEDS_RESUME" = "true" ]; then
  RESUME=$(npc resume detect)
  # 主 session 用 jq 取 next_seq / next_phase 决定续跑入口
fi

# 3. 首次创建 plan-state（plan 排好序后）
npc state init-run --plan-order '["add-foo","add-bar","add-baz"]'

# 4. 每个 change 的循环（1.0 形态：模板下沉 + 高层 pipeline）
for SEQ in 1 2 3; do
  CID=$(npc state get ".plan_order[$((SEQ-1))]" | tr -d '"')
  npc state add-change $SEQ "$CID"

  # 4.1 Implement
  #   - npc 把 §A Implementer 模板渲染到 disk（主 session 看不到模板内容）
  #   - npc 生成 ~150 tokens 引导语，主 session 拿到后调 Agent 工具
  npc agent prompt render --phase implement --change-id "$CID"
  SPAWN=$(npc agent spawn-prompt --phase implement --change-id "$CID")
  PROMPT_TEXT=$(echo "$SPAWN" | jq -r '.prompt')
  # 主 session: Agent(subagent_type="senior-code-developer", prompt=$PROMPT_TEXT)
  # sub-agent 返回 RESULT 行后：
  IMPL=$(npc implement record --seq $SEQ --result "$RESULT_LINE")
  [ "$(echo "$IMPL" | jq -r '.ok')" = "true" ] || continue

  # 4.2 Review-Fix Loop — review run 一行；fix prompt 同样下沉
  R=$(npc review run --seq $SEQ --round 0)
  N=0
  while [ "$(echo "$R" | jq -r '.blocking')" -gt 0 ] \
     && [ "$(echo "$R" | jq -r '.stale')" = "false" ] \
     && [ $N -lt 20 ]; do
    N=$((N+1))
    # 主 session 不再自己拼 fix prompt——npc 自动注入 findings + 修复历史
    npc agent prompt render --phase fix --change-id "$CID" --round $N
    SPAWN=$(npc agent spawn-prompt --phase fix --change-id "$CID" --round $N)
    PROMPT_TEXT=$(echo "$SPAWN" | jq -r '.prompt')
    # 主 session: Agent(subagent_type="senior-code-developer", prompt=$PROMPT_TEXT)
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

**1.0 主 session 注意力账本**（vs 0.3）：

| 单次 sub-agent 调用 | 0.3 写法 | 1.0 写法 |
|---|---|---|
| 主 session Write prompt 内容 | ~2500 tokens | 0（npc 直接落盘） |
| Agent 工具 prompt 字段 | ~2500 tokens（完整模板） | ~150 tokens（引导语） |
| 单次调用 token 占用 | ~5000 | ~150 |
| 整 run 节省（6 changes × 4 轮） | — | ~120k tokens |

---

## 12. 版本

当前契约版本：`v1.3`。

新增命令（v1.3，基石加固，全部经独立 review + 充分测试）：

- **`npc doctor`**：环境前置体检（git/openspec/codex/claude/jq/portable-timeout/schema/mimo.env/config/principles.md）。输出 `{ok, checks[], summary}`；required(git) 缺失 exit 3。
- **`npc verify tests`**：按 repo 清单（pyproject/pytest.ini/tests、package.json scripts.test、Makefile test:）探测并**真实复跑测试**（`shlex.split` + shell=False，杜绝注入）。`{ok, cmd, exit_code, passed, tail}`；passed→0 / fail→1 / 无命令→3。可被 `[verify].test` 覆盖。这是"不裸信 RESULT 自报"硬轨的家。
- **`npc verify routing`**：把路由不变量编进代码——**生成⊥验证**（coder 与 review 不同源）+ **MiMo 只许执行**（review.engine/bin/model 含 mimo 即 violation，无条件顶层挡）。`{ok, coder_backend, review_engine, violations[]}`；有 violation→1。
- **`npc implement run --seq N` / `npc fix run --seq N --round M`**：把 coder 子进程编排折进 npc（对标 `review run`）。后端：`--backend` > `[coder.phase].<phase>`（per-phase，如只把 fix 给 mimo）> `[coder].backend` > 默认 `claude`。**MiMo 默认不启用**（较慢，按需显式开）。内部 render prompt → headless `claude -p`（mimo 注入 env 路由到 MiMo）→ 抽 RESULT → record。TimeoutExpired/ConfigError/PermissionError 全部转结构化错误、phase 不悬挂。record ok→0 / 业务失败→1 / 用法→2 / env→3 / 依赖缺失→4。
- **`npc init --auto`** 新增 `auto_auth`：自动给 `<repo>/.claude/settings.json` 授权（`defaultMode=acceptEdits` + harness 工具 Bash 白名单），**合并保留既有 deny 与其它键**、幂等、坏 JSON 不覆盖、失败不阻塞。交互档不授权。payload 多 `auto_auth` 字段。

配置新增 `[coder]`（backend/mimo.env_file/model/bin）+ `[coder.phase]`（per-phase 后端，如 `fix="mimo"`）+ `[verify]`（test/lint/typecheck/build 覆盖）。MiMo 默认不启用，须显式配置。

| 版本 | 关键变化 |
|---|---|
| **1.3** | 新增 `doctor` / `verify tests` / `verify routing` / `implement run` / `fix run`；`init --auto` 自动授权 `.claude/settings.json`；config 增 `[coder]`/`[verify]`。把成本路由、独立验证、复跑测试、auto 授权焊进确定性核心 |
| **1.2** | 新增 `telemetry` 子命令族（emit/tail/agg/hotspots/estimate-tokens）+ events/pipeline/agent 自动 emit 钩子；`~/task_log/_telemetry/events.ndjson` 派生指标流落盘，主 session 仍零接触 |
| 1.1 | 文档与版本对齐（初始 release 即包含 1.0 全部能力） |
| 1.0 | 新增 `agent prompt render` / `agent spawn-prompt`，§A Implementer / §B Fixer 模板从 skill 文档下沉到 CLI 包资源；主 session 不再 Write 模板内容、不再把模板传给 Agent 工具 |
| 0.3 | 新增 pipeline 章节（review run / archive run / implement record / fix record） |
| 0.2 | 子命令自包含；新增 run.json / active.json + 全局 --run-ts / --task-log-dir；`--shell-exports` 标 deprecated |
| 0.1 | 初始契约（NPC_* 环境变量 + 细粒度命令） |

Breaking change（命令更名 / 参数语义变化 / stdout schema 字段移除）触发 major 版本递增；新增字段或新增子命令属于向后兼容。1.0 仅新增能力，未删除 0.x 的任何命令——0.x 调用方可平滑升级。
