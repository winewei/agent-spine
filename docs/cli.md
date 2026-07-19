# npc CLI 契约 v1.4

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
8. 解析宿主（`[host]` 配置 > `CLAUDECODE` env → claude > generic，v1.7）并识别当前宿主 session_id（宿主 session 目录 mtime 启发 + by-cwd hook 兜底，详见 `session` / `hosts` 模块；generic 宿主只走 hook 路径）
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
  "host": {"name": "claude", "source": "env", "session_dir": "/Users/you/.claude/projects/-Users-you-code-foo"},
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

**stderr**：警告（如宿主 session 目录缺失、portable-timeout 自举成功提示）

**宿主分流（v1.7）**：`host.name == "claude"` 且 `--auto` 时写 `<repo>/.claude/settings.json` 授权（`auto_auth` 字段汇报结果）；其它宿主 `auto_auth = {"ok": false, "skipped": "host-<name>-no-settings-grant"}`，权限按宿主自身机制放行。`host.session_dir` 为 null 表示该宿主无 mtime 启发能力（可用 `[host].session_dir` 配置模板补上）。

**exit**：`0` 正常（缺宿主 session 目录仅警告）；`3` 非 git 仓库

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

### `npc state repair [--seqs CSV] [--auto]`

自愈 state 与 git HEAD 之间的漂移（例：用户 `git reset` 后 task_log 仍记录已 archived 的 commit 链）。把对应 progress 条目重置为 `pending`，旧 base 目录整体搬到审计区，必要时把 openspec archive 退回 active，使后续流程能从该 seq 重新 implement。

**参数**：

- `--seqs CSV`：显式目标 seq（如 `1,3,5`）；省略则自动跑 `scan_state_drift` 探测漂移 seq
- `--auto`：标记主 session 自动调用（该命令本就无交互，此参数仅做日志标记，不改变行为）

**做什么**：

1. 决定目标 seq 集合（显式 `--seqs` 或自动漂移探测）
2. 对每个目标 seq：把旧 `progress[seq-1].base` 目录整体 `mv` 到 `<run_dir>/.repaired/<basename>-<ts>/`（冲突时追加序号）留存审计；若该 change 已被 `openspec archive` 到 `archive/`，`mv` 回 active changes 目录
3. 把 `progress[seq-1]` 重置为 `{seq, change_id, status:"pending", blocking_trend:[], categories_seen:[], rounds_since_strict_decrease:0, phases:{}}`
4. 把 repair 记录追加到 STATE_JSON 的 `repair_log`（单向增长数组，不删除历史）
5. 追加 `state.repair` 事件到 `run.events.jsonl`（per-change events.jsonl 已随旧 base 搬走，不再可寻，故只写 run 级流）

**stdout（无漂移）**：

```json
{"ok": true, "repaired": [], "message": "no drift detected; nothing to repair"}
```

**stdout（有修复）**：

```json
{
  "ok": true,
  "repaired": [
    {
      "ts": "2026-05-22T16:02:03+08:00",
      "seq": 2,
      "change_id": "add-checkpoint-store",
      "previous_status": "archived",
      "audit_base": "/.../.repaired/002-add-checkpoint-store-2026-05-22T16-02-03_08-00",
      "openspec_moved_back": true
    }
  ],
  "audit_root": "/.../.repaired"
}
```

**exit**：`0` 成功（含"无需修复"）；`2` `--seqs` 含非整数 token；`3` 环境错（STATE_JSON 不存在 / git 缺失）

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

### `npc phase rotate --seq N --to <new-phase> [--prev-status done|failed] [--prev-extra '<json-obj>']`

原子完成"退出上一个 phase + 进入下一个 phase"，fix loop 每轮开始时推荐用法（替代分别调用 `phase exit` + `phase enter` 两条命令）。设计目的：避免主 session 漏调 `phase enter` 造成 `started_at=null` 漂移。

**参数**：

- `--to <new-phase>`（必填，`--to_phase` dest）：要进入的新 phase 名
- `--prev-status done|failed`（默认 `done`）：以此状态退出当前所有 `in-progress` 的 phase（正常 ≤1 个，异常情况下全部兜底关闭）
- `--prev-extra '<json-obj>'`（默认 `{}`）：合并到被关闭 phase 的字段与事件的额外信息

**做什么**：

1. 读 STATE_JSON，扫描 `progress[seq-1].phases` 中所有 `status=in-progress` 的条目，逐个按 `--prev-status` 关闭（写 `done_at` / `duration_ms` / `--prev-extra`）
2. 为每个被关闭的 phase 追加 `<phase-base>.done|failed` 事件（推导规则同 `phase exit`）
3. 设置 `progress[seq-1].phases.<to_phase> = {status:"in-progress", started_at, started_ms}`，追加 `phase.start` 事件
4. 一次性写回 STATE_JSON + STATE_MD
5. 除 `review-rN` 与 `archive`（`prev-status=done` 时）外，为每个关闭的 phase 触发 `telemetry.emit_phase_exit`（与 `phase exit` 同口径，避免重复计数）

**stdout**：

```json
{
  "ok": true,
  "seq": 3,
  "to_phase": "fix-r2",
  "started_at": "2026-05-22T16:10:00+08:00",
  "base": "/.../003-add-checkpoint-store",
  "prev_phases_closed": [{"phase": "fix-r1", "duration_ms": 340000}]
}
```

**exit**：`0` 成功；`1` seq 超出 progress 数组长度；`2` `--to` 或 `--prev-status` 非法 / `--prev-extra` 非法 JSON；`3` 环境错（STATE_JSON 不存在）

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

## 4a. Spec 一致性分析 + Plan 前置门 + Change 脚手架

三个命令都不依赖 active run（只需 git 仓库），用于 implement 之前的确定性前置校验，以及 openspec change 脚手架生成。

### `npc spec analyze --change ID`

对标 GitHub spec-kit 的 `/analyze`：实现前纯读 `openspec/changes/<id>/` 下的 `proposal.md` / `specs/*/spec.md` / `tasks.md` 做确定性一致性检查（不依赖 openspec CLI、不需要 active run）。

**参数**：

- `--change ID`（必填）：openspec change-id

**做什么**：

1. 从 `proposal.md` 的"New Capabilities"段抽取声明的 capability 列表
2. 扫描 `specs/*/spec.md`，收集每个 capability 的 `### Requirement: ...` 数
3. 解析 `tasks.md` 的 `- [ ]` / `- [x]` 任务项
4. 生成 findings：
   - `no-tasks`（high）：`tasks.md` 缺失或没有任务项
   - `capability-no-spec`（high）：proposal 声明的 capability 没有对应 `specs/<cap>/spec.md`
   - `orphan-spec`（high）：`specs/<cap>/spec.md` 存在但 proposal 未声明
   - `requirement-maybe-uncovered`（medium，关键词覆盖启发式，可能误报）：某 capability 有 Requirement 但 tasks.md 未提及
   - `tasks-all-done`（low，信息性）：全部任务已 `[x]`，但仍在 implement 前调用本命令

**stdout**：

```json
{
  "ok": false,
  "change": "add-bigtable-reader",
  "requirements_count": 3,
  "tasks_count": 5,
  "capabilities": ["bigtable-reader"],
  "findings": [
    {"kind": "requirement-maybe-uncovered", "severity": "medium", "detail": "capability `bigtable-reader` 有 3 条 Requirement，但 tasks.md 未出现该 capability 关键词（启发式覆盖检查，可能误报）"}
  ]
}
```

**exit**：`0` 无 high/medium finding；`1` 有 high/medium drift；`2` 缺 `--change`；`3` change 目录不存在 / repo_root 定位失败

---

### `npc plan check --change ID [--phase implement] [--openspec-bin PATH]`

阶段前置门：调 `openspec status --change <id> --json`，解析 `payload.applyRequires` 中每个产物 id 在 `artifacts[]` 里的 `status` 是否为 `done`。绝不裸信 LLM 自报"已就绪"。

**参数**：

- `--change ID`（必填）
- `--phase PHASE`（默认 `implement`）：仅作回显，不影响判定逻辑
- `--openspec-bin PATH`（可选）：覆盖 openspec 可执行文件路径

**stdout**：

```json
{
  "ok": false,
  "change": "add-bigtable-reader",
  "phase": "implement",
  "ready": false,
  "apply_requires": ["design-doc", "test-plan"],
  "missing": ["test-plan"]
}
```

**exit**：`0` ready；`1` not ready / openspec 调用失败或输出非法 JSON；`2` 缺 `--change` / `--change` 疑似参数注入（以 `-` 开头）；`3` 非 git 仓库；`4` openspec 未安装

---

### `npc plan new-change --change ID [--description TEXT] [--schema PATH] [--openspec-bin PATH]`

脚手架一个 openspec change：调 `openspec new change <id> [--description ..] [--schema ..]`，成功后扫描生成目录列出全部文件。

**参数**：

- `--change ID`（必填）：kebab-case change-id，强校验 `^[A-Za-z0-9][A-Za-z0-9._-]*$`（挡参数注入与路径遍历），不得为 `.` / `..`
- `--description TEXT`（可选）：不得以 `-` 开头
- `--schema PATH`（可选）：不得以 `-` 开头
- `--openspec-bin PATH`（可选）

**stdout**：

```json
{
  "ok": true,
  "change": "add-bigtable-reader",
  "path": "/.../openspec/changes/add-bigtable-reader",
  "files": ["proposal.md", "tasks.md", "specs/bigtable-reader/spec.md"]
}
```

**exit**：`0` 成功；`1` openspec 调用失败（error 带 stderr 尾段）；`2` 缺 `--change` / change-id 或参数非法；`3` 非 git 仓库；`4` openspec 未安装

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

历史上 §A Implementer / §B Fixer 模板住在 skill 文档里，主 session 拼模板、Write 到 prompt 文件、再把整文件内容塞进 `Agent(prompt=...)` 字段——同一份 ~2500 tokens 的模板在主 session context 里出现两次（Write + Agent）。1.0 把模板下沉到 CLI 包资源（`npc.templates`），主 session 仅承担 ~150 tokens 的薄引导语。

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

### `npc agent timeout-budget --seq N --phase X [--base N] [--mult F] [--max-sec N]`（1.1+）

查询当前 phase 应用的 Agent 调用 wall-clock timeout（**纯查询，不写 state**）。渐进退避：主 session 每次调 `Agent(...)` 前先取一次预算；若超时则调 `record-timeout` 递增重试计数，下次再取预算——直到 `exhausted=true` 才放弃当前 change。

**参数**：

- `--seq N` / `--phase X`（必填）：`--phase` 查询的是该 phase（如 `implement` / `fix-r2`）的 `timeout_retries`
- `--base N`（可选，默认 1800）：基准秒数
- `--mult F`（可选，默认 1.2）：退避倍率
- `--max-sec N`（可选，默认 3600）：timeout 上限秒数

**做什么**：`timeout_sec = min(base * mult^retries, max_sec)`，`retries` 取 `progress[seq-1].phases.<phase>.timeout_retries`（由 `record-timeout` 递增，缺省 0）；`retries >= 5` 判定 `exhausted`（对应两次撞满 3600s 上限后仍失败）。

**stdout**：

```json
{
  "ok": true,
  "seq": 1,
  "phase": "fix-r2",
  "timeout_sec": 2592,
  "retries": 2,
  "exhausted": false,
  "max_reached": false,
  "base_sec": 1800,
  "multiplier": 1.2,
  "max_sec": 3600,
  "exhausted_at_retries": 5
}
```

**exit**：`0` 成功；`1` seq 超出 progress 数组长度；`3` 环境错（STATE_JSON 不存在）

---

### `npc agent record-timeout --seq N --phase X [--base N] [--mult F] [--max-sec N]`（1.1+）

记录一次 Agent 调用超时：把 `progress[seq-1].phases.<phase>.timeout_retries += 1` 并写 `timeout_last_ts`，返回按新 `retries` 计算的下一次 timeout 预算（公式同 `timeout-budget`）。

**参数**：同 `timeout-budget`

**stdout**：

```json
{
  "ok": true,
  "seq": 1,
  "phase": "fix-r2",
  "retries": 3,
  "next_timeout_sec": 3110,
  "exhausted": false,
  "max_reached": false
}
```

**exit**：`0` 成功；`1` seq 超出 progress 数组长度；`3` 环境错（STATE_JSON 不存在）

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

### `npc review run --seq N --round M [--retries 1] [--timeout 900] [--codex-bin PATH] [--portable-timeout PATH] [--engine codex|claude] [--config PATH]`

完整一轮 review，**支持双引擎**（codex / claude），不再是仅 codex。

**参数**：

- `--seq N` / `--round M`（必填）
- `--retries N`（默认 1）：引擎失败重试次数
- `--timeout N`（默认 900）：单次引擎调用超时秒数
- `--codex-bin PATH`（可选）：覆盖 codex 可执行文件路径（仅 `--engine codex` 时生效）
- `--portable-timeout PATH`（可选）：覆盖 portable-timeout 路径
- `--engine {codex,claude}`（可选）：覆盖本次 review 引擎；省略时读配置文件 `[review].engine`，配置也未设置则默认 `codex`
- `--config PATH`（可选）：显式指定 TOML 配置文件（只读该文件，不参与合并）；省略则按 `<repo>/.npc/config.toml` → `~/.config/npc/config.toml` → `~/task_log/config.toml` 分层**深合并**（v1.6 起：table 递归合并、标量/数组整体覆盖，低优先级打底高优先级覆盖；此前语义为命中即停）

**做什么**：

1. 加载配置（`--config` 或标准查找链），决定 `selected_engine`：`--engine` > `[review].engine` > 默认 `codex`；`--engine` 传入非 `codex`/`claude` 之外的值直接报用法错
2. 读 STATE_JSON 取 `progress[seq-1]` → change_id / base / implement_commit（round>0 用）
3. 调用 focus 模板渲染 `<base>/round-N.focus.md`
4. `phase enter review-rN`
5. 按 `selected_engine` 起子进程：
   - `codex`：`<portable-timeout> <timeout> codex exec --cd <repo_root> --sandbox read-only --skip-git-repo-check -c model_reasoning_effort=high --output-schema <schema_path> -o <base>/round-N.review.json --json -`（stdin = focus.md）
   - `claude`：等价的 headless `claude` 调用（`[review].claude_bin` / `claude_model` / `claude_extra_args` 可配置），同样把 `--output-schema` 约束的 JSON 写到 `<base>/round-N.review.json`
   - 两种引擎的 stdout/stderr 均落 `<base>/round-N.events.jsonl`
6. exit≠0 或 review.json 非合法 JSON → 删两文件，**重试 `--retries` 次**；仍失败：`phase exit review-rN failed`，返回 `{ok:false, error:"<engine>-exec-failed", ...}`（`<engine>` 是实际使用的引擎，如 `codex-exec-failed` / `claude-exec-failed`）
7. parse review → metrics（verdict / blocking / advisory / categories / blocking_findings）
8. **一次 `update_state`** 完成：`phase exit review-rN done`（带 metrics + `engine` 字段）+ update_trend + categories_seen 合并
9. blocking>0 时自动渲染下一轮 fix.findings：`<base>/round-(N+1).fix.findings.md`
10. 无论成功失败均向 telemetry emit 一条 `review.round` record（含 `engine` 字段，供 `npc cost` 按引擎拆成本）

**stdout（成功）**：

```json
{
  "ok": true,
  "seq": 1,
  "round": 0,
  "change_id": "add-config-loader",
  "engine": "codex",
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

**stdout（失败，引擎调用失败）**：

```json
{
  "ok": false,
  "seq": 1,
  "round": 0,
  "error": "codex-exec-failed",
  "engine": "codex",
  "detail": "exit_code=1",
  "attempts": 2,
  "events_path": "/.../round-0.events.jsonl"
}
```

`error` 也可能是 `invalid_review_schema`（review.json 内容不合法，此时 stdout 无 `engine` 字段）。

**exit**：`0` 成功；`1` 业务失败（引擎调用 / review schema 失败）；`2` 用法错（`--engine` 不是 `codex`/`claude`，或 `--config` 指向的配置文件非法）；`4` 依赖缺失（codex/claude 可执行文件或 portable-timeout 未找到）

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

### `npc implement run --seq N [--change-id ID] [--backend PROVIDER] [--timeout N] [--config PATH]`（1.3+）

把 coder 子进程编排折进 npc（对标 `review run`，但方向相反——coder 是生成体，默认路由到 `claude`）：`phase enter implement` → 渲染 §A 模板 → 起 coder 后端子进程 → 从 stdout 尾部抽 RESULT 行 → 等价于 `implement record`。后端未产出 RESULT 行不会让 phase 悬挂（合成失败 RESULT 走标准失败路径）。

**参数**：

- `--seq N`（必填）
- `--change-id ID`（可选）：与 state 中该 seq 的 change_id 做一致性校验，不一致则报错
- `--backend PROVIDER`（可选）：覆盖 coder 后端。取值 = 内置 provider（`claude`/`mimo`/`codex`）或 `[providers.*]` 自定义名（v1.6+，如 `kimi`/`deepseek`）。优先级：`--backend` > `[coder.phase].implement`（per-phase 覆盖）> `[coder].backend`（全局）> 默认 `claude`。**廉价层默认不启用**，仅显式指定时才路由过去
- `--timeout N`（可选）：coder 子进程超时秒数
- `--config PATH`（可选）：显式 TOML 配置路径

**provider 注册表（v1.6+）**：`[providers.<name>]` 定义可路由后端，字段：`runner`（`claude-cli` 默认 / `codex-cli`）、`env_file`（凭据指针，claude-cli 时 source 后注入子进程 env，用于 Anthropic 兼容端点如 kimi / qwen / deepseek / MiMo）、`model`、`bin`。内置三个 provider 无需声明、可被同名覆盖：`claude`（claude-cli 无 env_file）、`mimo`（claude-cli + `~/.config/npc/mimo.env`，model=mimo-v2.5-pro）、`codex`（codex-cli）。推荐用法：provider 定义（含 env_file）放全局 `~/.config/npc/config.toml`，项目 `.npc/config.toml` 只写 `[coder]` 路由（可入 git，无凭据）。路由引用未注册 provider 在加载期即报 `ConfigError`。

**做什么**：

1. 加载配置，解析 `selected backend`（provider 名）
2. `phase enter implement`
3. 渲染 §A Implementer 模板到 `$BASE/implement.prompt.md`
4. 按 provider.runner 起子进程：`claude-cli` 走 headless `claude -p <prompt> [--model M] --permission-mode bypassPermissions`（provider 有 env_file 时解析出的环境变量覆盖到子进程 env）；`codex-cli` 走 `codex exec --skip-git-repo-check --sandbox workspace-write [-m M] <prompt>`（model 优先级：`[coder].model` > provider.model）
5. 从 stdout 末尾抽 `RESULT:` 行（找不到则合成失败 RESULT：`commit=- tasks=0 tests=fail ... notes=coder 未产出 RESULT 行`）
6. 等价调用 `implement record` 完成 phase exit + state set-progress
7. 子进程 `TimeoutExpired`/`SubprocessError` 或配置期异常（`FileNotFoundError`/`MimoEnvError`/`ValueError`/`NotImplementedError`）全部转结构化错误，并保证 phase 以 `failed` 收尾（不会悬挂在 `in-progress`）

**stdout（成功，`implement record` 的字段 + 以下）**：

```json
{
  "ok": true,
  "seq": 1,
  "change_id": "add-config-loader",
  "commit": "e05d530...",
  "tasks": 8,
  "tests": "pass",
  "summary": "/.../implement.summary.md",
  "backend": "claude",
  "model": null,
  "coder_exit": 0
}
```

**stdout（失败，如 backend 子进程超时）**：

```json
{
  "ok": false,
  "seq": 1,
  "error": "coder-timeout",
  "reason": "coder-timeout",
  "detail": "...",
  "backend": "claude"
}
```

**exit**：`0` 成功；`1` 业务失败（`implement record` 校验不过等）；`2` 配置错误（`ConfigError`，或 `--backend` 指到未注册 provider）；`3` 环境错（缺 state / provider env 文件读取失败，如 chmod 权限问题）；`4` 依赖缺失（coder 可执行文件未在 PATH 中找到，或 provider env_file 本身不存在）

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

### `npc fix run --seq N --round M [--change-id ID] [--backend PROVIDER] [--timeout N] [--config PATH]`（1.3+）

`implement run` 的 fix-rN 版本：`phase enter fix-rN` → 渲染 §B 模板（自动注入 blocking findings + 修复历史）→ coder 后端子进程 → 抽 RESULT → 等价于 `fix record`。

**参数**：与 `implement run` 相同，另加 `--round M`（必填）

**做什么**：与 `implement run` 基本一致，区别：

1. phase 名为 `fix-r<round>`
2. per-phase 后端覆盖读 `[coder.phase].fix`
3. prompt 渲染引用 `round-(N-1).review.json` 的 blocking findings（同 `agent prompt render --phase fix`）
4. **子进程超时或配置期异常时，`state set-progress status` 落 `needs-user-decision`**（比 `implement run` 更保守——fix loop 失败通常需要人工介入，而不是像 implement 失败那样标 `failed` 后续可能自动重试）

**stdout（成功，`fix record` 的字段 + 以下）**：

```json
{
  "ok": true,
  "seq": 1,
  "round": 1,
  "change_id": "add-config-loader",
  "commit": "582e481...",
  "fixed": 2,
  "tests": "pass",
  "summary": "/.../round-1.fix.summary.md",
  "backend": "claude",
  "model": null,
  "coder_exit": 0
}
```

**exit**：`0` 成功；`1` 业务失败；`2` 配置错误（含未注册 provider）/ 缺 `--round`；`3` 环境错；`4` 依赖缺失

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

完整 JSON Schema：`src/npc/telemetry_schema_v1.json`（与首次写入时拷出的 `_telemetry/schema-v1.json` 内容一致）。

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

## 8c. 并行编排辅助（1.4+，从 /new-plan-changes-v3 skill 脚本下沉）

v3 波次并行编排原先自带四个 skill 脚本（waves.py / detect_plan_only.py / verify_manifest.py / notify.py）。1.4 起全部下沉为 npc 子命令，纳入契约与测试管理；skill 侧零脚本。

### `npc plan waves [--input FILE]`

并行波次**候选**划分：Kahn 拓扑分层 + 层内文件交集贪心着色。输入（stdin 或 `--input`）一个 JSON 对象：

```json
{
  "nodes": ["add-a", "add-b"],
  "edges": [["add-a", "add-b"]],
  "files": {"add-a": ["src/x.py"]},
  "tie_break": {"add-a": [0, 12]}
}
```

`edges` 的 `[A,B]` 表示 A 先于 B；`files` 缺省视为空集（不冲突）；`tie_break` 为 `[tier, scope]` 稳定排序键，缺省排最后。

文件冲突按**路径前缀重叠**判定而非字符串相等：归一化（剔除 `./`、尾部 `/`）后相等、或一方是另一方的组件前缀即冲突——`files` 由 LLM 从 proposal 抽取，目录级条目（如 `app/services/`）是保守冲突标识，必须命中其下所有具体文件。`nodes` 须为非空字符串且不得重复，否则 exit 2。

**stdout**：

```json
{"waves": [["add-a"], ["add-b"]], "layers": [...], "split_reasons": [{"layer": 0, "members": [...], "sub_waves": [...], "serialized_pairs": [["a","b"]], "shared_files": ["src/x.py"]}], "cycle": []}
```

`cycle` 非空表示 DAG 有环，已强制释放所列节点破环。输出是**候选**——语义耦合仍须架构师 sub-agent 裁定。

**exit**：`0` 成功；`2` 输入不合法（非 JSON / 缺 nodes / 文件不可读）。

---

### `npc verify manifest --result '<RESULT 行>' [--manifest PATH]`

并行 implementer（worktree 内 sub-agent）产出核验：plan-only 判定 + manifest 文件核对，一次完成。

RESULT 行两种格式均接受：

- npc 契约：`RESULT: commit=<hash> tasks=.. tests=.. summary=.. notes=..`（manifest 路径来自 implementer 单独输出的 `MANIFEST:` 行，经 `--manifest` 传入）
- legacy JSON（architect-swarm）：`RESULT: {"status":.., "files_written":N, "manifest":".."}`

manifest JSON 的 `files_written` 条目可为纯路径字符串或 `{"path":..,"sha256":..}` 对象；给了 sha256 就核对。

**stdout**：

```json
{"ok": true, "verdict": "code", "reason": null, "commit": "abc123", "files": {"ok": true, "reason": null, "present": 3, "missing": [], "sha_mismatch": [], "total": 3}}
```

verdict 语义：`plan_only`（无 RESULT 行 / commit=- / 自报 plan-only / manifest 缺失、为空或含非法条目——没有可信的真实产出，reason 含 `manifest_malformed_entry`）；`error`（自报 error）；`code`（有 commit 有产出；此时若文件丢失或 sha 不符，verdict 保持 code 但 `ok:false`，reason=`files_missing|sha_mismatch`）。

**exit**：`0` verdict=code 且 manifest 全部核对通过；`1` 其余；`2` 用法错。

---

### `npc notify --event KIND [--url URL] [--format raw|slack|feishu] [--kv k=v ...] [--text ...] [--timeout 5.0]`

best-effort webhook 推送。URL 解析顺序：`--url` > `$NPC_WEBHOOK` > `$NPC_V3_WEBHOOK`（兼容旧名）；都为空则静默 no-op（通知未启用）。

**参数**：

- `--event KIND`（必填）：事件类型，如 `implement-done` / `wave-done` / `run-finalized`
- `--url URL`（可选）：显式 webhook URL；省略依次读 `$NPC_WEBHOOK` / `$NPC_V3_WEBHOOK`
- `--format {raw,slack,feishu}`（默认 `raw`）：payload 形状
- `--kv k=v`（可重复）：结构化字段，合并进 `raw` payload
- `--text TEXT`（可选）：人读摘要；省略则由 event+kv 派生（`[npc] <event> k=v ...`）
- `--timeout N`（默认 `5.0`）：webhook POST 的超时秒数（float）

**stdout**：`{"ok": true, "event": "...", "url_set": true, "delivered": true}`

**exit**：**总是 `0`**。webhook 超时/拒连/4xx 只写 stderr 警告 + `delivered:false`，绝不打断 run。

---

## 8d. 质量门与环境体检（v1.3+）

`doctor` / `verify tests` / `verify routing` 是把"跑 npc 之前需要满足的前置条件"与"不裸信 sub-agent 自报"固化进 npc 的三个基础命令。均只需 git 仓库（`verify routing`/`verify tests` 还需能加载 npc 配置），无需 active run。

### `npc doctor`

环境前置体检：git / openspec / codex / claude / jq / portable-timeout（PATH 或 `~/.local/bin` 自举）/ review schema 自举情况 / mimo.env 成本路由 / npc config 可加载性 / 路由在用 provider 就绪性（v1.6+）/ `docs/principles.md`。除 `git` 外全部是 warn 级、不阻塞。

**做什么**：对 `_BIN_CHECKS`（`git` 必备，`openspec`/`codex`/`claude`/`jq` 可选）逐个查 PATH；`portable-timeout` 额外查 `~/.local/bin` 自举位置并校验可执行位；`schema` 检查 `~/task_log/.new-plan-review-schema.json` 是否存在且为合法 JSON；`mimo.env` 检查 `~/.config/npc/mimo.env` 是否存在可读；`config` 尝试 `load_config`（失败降级 warn，不阻塞）；`providers`（v1.6+）对 coder 路由实际引用的每个 provider 检查 env_file 存在可读 + runner 可执行文件可用（未被引用的定义不产生噪音，问题一律 warn 不阻塞）；`host`（v1.7+）报告解析出的宿主（名字/来源/session 识别能力，信息级恒 ok）；`principles.md` 检查 `<repo>/docs/principles.md`。

**stdout（单行，`ok` 恒真实反映 required 缺失情况；required 缺失时同一行内嵌 `error`/`message`）**：

```json
{
  "ok": true,
  "checks": [
    {"name": "git", "status": "ok", "detail": "已找到：/usr/bin/git", "required": true},
    {"name": "openspec", "status": "warn", "detail": "未在 PATH 中找到 openspec", "required": false},
    {"name": "codex", "status": "ok", "detail": "已找到：/usr/local/bin/codex", "required": false},
    {"name": "claude", "status": "ok", "detail": "已找到：/usr/local/bin/claude", "required": false},
    {"name": "jq", "status": "ok", "detail": "已找到：/usr/bin/jq", "required": false},
    {"name": "portable-timeout", "status": "ok", "detail": "已找到（自举位置）：/Users/you/.local/bin/portable-timeout", "required": false},
    {"name": "schema", "status": "ok", "detail": "已存在：...", "required": false},
    {"name": "mimo.env", "status": "warn", "detail": "成本路由 mimo.env 缺失：...；coder 将走默认 premium 层", "required": false},
    {"name": "config", "status": "ok", "detail": "使用内置默认配置（未找到配置文件）", "required": false},
    {"name": "host", "status": "ok", "detail": "宿主 claude（来源 env）；session 目录模板 .claude/projects/{proj_key}", "required": false},
    {"name": "principles.md", "status": "ok", "detail": "已存在：...", "required": false}
  ],
  "summary": {"ok": 8, "warn": 2, "missing": 0, "missing_required": []}
}
```

**stdout（required 缺失，即 `git` 不在 PATH）**：在上述结构基础上追加 `"error": "dependency_missing"` 与 `"message": "缺少必备前置：git"` 两个字段，仍是同一行 JSON。

**exit**：`0` 无 required 项缺失；`4` required 项缺失（当前仅 `git`，外部依赖缺失）

---

### `npc verify tests [--config PATH]`

按 repo 清单探测并**真实复跑测试**（不裸信 sub-agent 自报的 `tests=pass`）。

**参数**：

- `--config PATH`（可选）：显式 TOML 配置路径

**做什么**：测试命令解析优先级：`[verify].test` 配置覆盖 > 有 `pyproject.toml`/`pytest.ini`/`tests/` 目录 → `python3 -m pytest -q` > `package.json` 的 `scripts.test` 非空 → `npm test` > `Makefile` 含行首 `test:` 目标 → `make test` > 都没有 → 报错。命令经 `shlex.split` + `shell=False` 执行，杜绝命令注入。

**stdout**：

```json
{"ok": true, "cmd": "python3 -m pytest -q", "exit_code": 0, "passed": true, "tail": "...30 行内...."}
```

**exit**：`0` 测试通过；`1` 测试失败，或配置加载失败（`config_error`）；`3` 未探测到测试命令 / repo_root 定位失败

---

### `npc verify routing [--config PATH]`

校验 coder/review 路由不变量：**生成⊥验证**（coder 与 review 不得解析到同一执行身份）+ **MiMo 只许执行**（review 路由含 MiMo 一律 violation）。

**参数**：

- `--config PATH`（可选）：显式 TOML 配置路径

**做什么**：三条规则：

1. `backend_unsupported` / `engine_unsupported`：`coder.backend`（含每个 `[coder.phase]` per-phase 覆盖）必须是已注册 provider（内置 + `[providers.*]`）；`review.engine` 必须在支持列表（codex/claude，review 结构上不接受 provider 名）
2. `gen_not_orthogonal`：coder 在用的任一后端与 review 解析到同一执行身份（都是 claude 且 `bin`+`model` 相同；或都是 mimo）→ 等于自己评自己
3. `cheap_exec_only`（v1.5 名 `mimo_exec_only`）：review 路由（`engine` / `claude_bin` / `claude_model`）命中任一带 env_file 的廉价层 provider 的名字或 model → 违反「廉价层只许执行」，每个命中 provider 一条

**stdout**：

```json
{
  "ok": true,
  "coder_backend": "claude",
  "coder_phase_backends": {"fix": "mimo"},
  "review_engine": "codex",
  "violations": []
}
```

**exit**：`0` 无 violation；`1` 有 violation，或配置加载失败；`3` repo_root 定位失败

---

## 8e. Git 卫生 + 对外交付

`git branch-for` / `git ensure-clean` / `git commit` 是 SDD 流程的确定性 git 卫生基础（分支隔离 / 干净工作区 / 确定性提交）。`deliver` / `pr open` 是 npc 笼子里少数的**对外动作**——push 与开 PR 都会把工作推到远端对外可见，因此 npc 只提供纯机械命令，绝不自作主张决定要不要推；是否交付由上层 skill 的人闸拍板。以上五个命令均只需 git 仓库，无需 active run。

### `npc git branch-for --change ID`

为 change 切到确定性分支 `change/<id>`。

**参数**：`--change ID`（必填）

**做什么**：分支名固定为 `change/<change-id>`；已存在则 `git checkout <branch>`（`created=false`），否则 `git checkout -b <branch>`（`created=true`）。change-id 只允许 `[A-Za-z0-9._-]`，不含 `..`、不以 `-` 开头（防当作 flag）。

**stdout（成功）**：

```json
{"ok": true, "branch": "change/add-foo", "created": true}
```

**stdout（失败）**：

```json
{"ok": false, "branch": "change/add-foo", "created": true, "error": "git_checkout_failed", "stderr": "..."}
```

**exit**：`0` 成功；`1` git checkout 失败；`2` 缺 `--change` / change-id 非法；`3` 非 git 仓库

---

### `npc git ensure-clean`

工作树脏则拒绝。

**做什么**：`git status --porcelain -z` 解析（`-z` 用 NUL 分隔记录，天然支持含空格/特殊字符路径；对旧式无 NUL 输出按行回退解析），空则视为 clean。

**stdout**：

```json
{"ok": false, "clean": false, "dirty_files": ["src/foo.py", "docs/bar.md"]}
```

**exit**：`0` clean；`1` 脏（仍打印 `dirty_files`）/ `git status` 本身失败；`3` 非 git 仓库

---

### `npc git commit [--message TEXT] [--change ID] [--phase PHASE]`

`git add -A` + `git commit`（消息可派生）。

**参数**：

- `--message TEXT`（可选）：显式提交消息，优先级最高
- `--change ID` / `--phase PHASE`（可选）：无 `--message` 时用于派生 `chore(spine): <phase> <change>`（无 `--phase` 则省略该段，得到 `chore(spine): <change>`）

**做什么**：消息不得含换行（防注入多行/伪造 trailer）、不得以 `-`（防当作 flag）或 `#`（git 会把整行当注释丢弃）开头。无改动可提交时 `committed=false` 且视为成功（`exit 0`，不算失败）。

**stdout（有提交）**：

```json
{"ok": true, "committed": true, "commit": "5b33379...", "message": "chore(spine): archive add-foo", "branch": "change/add-foo"}
```

**stdout（无改动）**：

```json
{"ok": true, "committed": false, "reason": "nothing-to-commit"}
```

**exit**：`0` 成功（含"无改动"情况）；`1` `git add` / `git commit` 失败；`2` 缺消息且无 `--change`/`--phase` 可派生 / message 非法；`3` 非 git 仓库

---

### `npc deliver [--remote origin] [--branch NAME] [--no-set-upstream]`

push 当前分支到远程（对外动作）。不自作主张决定要不要推，仅在被显式调用时执行一次 `git push [-u] <remote> <branch>`。

**参数**：

- `--remote NAME`（默认 `origin`）
- `--branch NAME`（可选）：省略则取当前分支；游离 HEAD 时报错要求显式传
- `--no-set-upstream`（flag，反转 `set_upstream`，默认 `true` 即带 `-u`）

**stdout（成功）**：

```json
{"ok": true, "remote": "origin", "branch": "change/add-foo", "pushed": true}
```

**stdout（失败）**：

```json
{"ok": false, "remote": "origin", "branch": "change/add-foo", "pushed": false, "error": "push_failed", "stderr_tail": "...脱敏后末尾 40 行..."}
```

**exit**：`0` 成功；`1` push 失败；`2` 未给 `--branch` 且无法确定当前分支（游离 HEAD）；`3` 非 git 仓库；`4` 未装 git

---

### `npc pr open [--title TEXT] [--body TEXT] [--body-file PATH] [--base BRANCH] [--draft]`

`gh pr create`（body 可从 run-summary 派生），解析回 PR url。

**参数**：

- `--title TEXT` / `--body TEXT` / `--base BRANCH` / `--draft`（flag）：透传给 `gh pr create`
- `--body-file PATH`（可选）：优先于 `--body`——从文件读 body（如传 `run-summary.md` 路径）

**stdout（成功，能解析到 url）**：

```json
{"ok": true, "pr_url": "https://github.com/you/repo/pull/42", "title": "add foo"}
```

**stdout（成功但未解析到 url，附 `raw_stdout_tail` 供人工捞取）**：

```json
{"ok": true, "pr_url": null, "title": "add foo", "raw_stdout_tail": "..."}
```

**stdout（失败）**：

```json
{"ok": false, "title": "add foo", "error": "gh_pr_create_failed", "stderr_tail": "...脱敏后末尾 40 行..."}
```

**exit**：`0` 成功；`1` `gh pr create` 失败；`2` `--body-file` 读取失败；`3` 非 git 仓库；`4` 未装 `gh`

---

## 8f. 内环与整合下沉（v1.5+，上下文预算重构）

设计依据 [docs/optimization-proposals/2026-07-05-orchestration-context-budget.md](./optimization-proposals/2026-07-05-orchestration-context-budget.md)：主 session 每推进一个 change 消耗 O(1) token；确定性循环与多步编排全部收进 npc，主 session 只在决策分叉点出场。

### `npc change run --seq N [--from PHASE] [--decision ACTION] [--max-rounds 20] [--auto] [--backend ...] [--engine ...] [--config PATH]`

单 change 内环一条命令跑完：implement → review round-0 → (fix → review)* → archive。复用既有 pipeline（`implement/fix run` / `review run` / `archive run`）与 `auto-decide`，不重写。

**决策点分档**：

- `--auto`（或 state.mode=auto）：决策点内部调 auto-decide，一路跑完，返回一行终态 JSON。
- 交互档：跑到决策点（trigger ∈ stale / max-rounds / implementer-failed / fixer-failed / codex-failed / archive-failed）带 `status=needs-decision` 退出（**exit 5**），`pending_decision`（trigger/phase/round/suggested）装订进 state；主 session 问人后 `--decision <continue-retry|skip|force-archive|abort>` 消费续跑。存在未消费 pending_decision 时不带 `--decision` 重跑 → exit 2。

`--from {implement|review|fix|archive}` 断点重入（默认按 entry.status + blocking_trend 推导）；终态 change 重跑必须显式 `--from`。

```text
stdout（终态）:
  {"ok": true, "seq": N, "change_id": "...", "status": "archived",
   "rounds": <int>, "archive_commit": "<hash>", "blocking_trend": [...], "pointer": {...}}
stdout（决策点）:
  {"ok": false, "seq": N, "change_id": "...", "status": "needs-decision",
   "trigger": "...", "phase": "...", "round": <int>, "suggested": "<action>",
   "blocking_trend": [...], "categories_seen": [...], "pointer": {...}}

exit: 0 archived / 1 终态失败（skipped|failed|aborted）/ 5 needs-decision /
      2 用法错 / 3 环境错 / 4 依赖缺失
```

### `npc integrate --seq N --result '<RESULT行>' [--result-file PATH] [--manifest PATH] [--no-verify-tests]`

worktree 产物整合进 main 的多步编排（替代 v3 skill Step 9 伪 bash 段），任一步失败自动收拾现场、main 保持绿：

1. verify manifest（plan-only 判定 + 文件存在性/sha 核对）；
2. `git cherry-pick <worktree_commit>`，冲突 → `--abort`；
3. **hash 翻译**：RESULT 行 `commit=<worktree_hash>` → 整合后 main HEAD（必要项：archive precheck 用 merge-base --is-ancestor 校验 chain）；
4. `implement record` 装订，失败 → `git revert` 摘除；
5. verify tests 真实复跑（探测不到测试命令 → skipped 警告），失败 → revert + progress 回退 `failed/verify-tests-failed`。

冲突与 revert 均落 telemetry `deviation` record 与 run.events（`integrate.conflict` / `integrate.verify_tests_failed`）。

```text
stdout（成功）:
  {"ok": true, "seq": N, "change_id": "...", "worktree_commit": "...",
   "integrated_commit": "...", "verify_tests": "pass|skipped", "files": {"present","total"}}
stdout（失败）:
  {"ok": false, "seq": N, "step": "verify-manifest|cherry-pick|record|verify-tests",
   "reason": "...", "reverted": "<hash>|null", ...}

exit: 0 成功 / 1 任一步失败 / 2 用法错 / 3 环境错
```

### `npc status --brief`

compaction / 续跑 / 接手后的**单命令重入契约**：收掉 changes 全列表，带出重定向必需品。主 session 纪律：任何 compaction 之后第一条命令永远是它，绝不信任记忆里的进度。

```text
stdout:
  {"ok": true, "run_ts", "goal", "mode", "top_status", "total", "by_status",
   "current": {...}|null,
   "pending_decisions": [{"seq","change_id","trigger","round","suggested"}, ...],
   "notes": [{"ts","source","text"}, ...]（未消费的编排日志/steering）,
   "next_action": "<下一步动作提示>"}

exit: 0 成功 / 3 环境错
```

### `npc state note (--text TEXT [--source SRC] | --consume)`

编排日志（追加式 `<run_dir>/notes.jsonl`）：承载编排器意图备忘与**人的中途转向指令（steering）**。消费进度用 state 顶层 `notes_consumed_at` 水位；`status --brief` 只带出水位之后的 note，主循环在 change/波次边界消费后 `--consume` 打水位。

```text
stdout: {"ok": true, "path": "<notes.jsonl>", "ts": "<iso>"}   （--text）
        {"ok": true, "consumed_up_to": "<iso>"}                 （--consume）
exit: 0 成功 / 2 --text 与 --consume 都缺或同给 / 3 环境错
```

### `npc verify tasks --change ID [--seq N]`

tasks.md checkbox 完成度派生计数。change 是调度量子，task 绝不进主 context——主 session 与人只看 `tasks_done/tasks_total` 两个数。`--seq` 给定时与 state 里 implement RESULT 自报的 `tasks=` 计数交叉验证。

```text
stdout: {"ok": <bool>, "change", "tasks_done", "tasks_total",
         "claim": <int>|null, "consistent": <bool>|null}
exit: 0 一致或无 claim / 1 claim 不一致 / 2 缺 --change / 3 tasks.md 缺失
```

### telemetry `deviation` record（偏差记账）

`npc change run` 决策点、`npc integrate` 冲突/revert、`npc auto-decide --apply` 均自动落一条 `kind=deviation`：`{trigger, action, layer(implementation|decompose|design|unknown), cost_rounds, decided_by(auto|user), outcome_reason=trigger, pointer}`。这是"先收证据后建轨"的证据层——归因升级阶梯等未来硬轨是否值得建，由这些 record 的聚合（`telemetry agg` / `hotspots` 经 `outcome_reason` 计数）决定。

### `npc state init-run --goal` + summary Goal Coverage

`init-run --goal "<人设的原始目标>"` 把目标写进 state；`summary render` 据此渲染 **Goal Coverage** 段（原始目标 × 各 change 终态对照表）——run 级验收是**人**的活：逐 change 全过 ≠ 组合达标，缺口应立为新 change。

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

## 9a. 只读观测与维护（status / cost / clean）

### `npc status`

当前 run 进度一览（**只读**，纯读 STATE_JSON 派生快照，绝不写 state）。

**做什么**：按 `progress[].status` 计数得到 `by_status`；找第一个非终态（非 `archived`/`failed`/`skipped-auto`）的 change 作为 `current`；每个 change 的 `rounds` 优先取 `total_rounds`，否则取 `blocking_trend` 长度，都没有则 0。

**stdout**：

```json
{
  "ok": true,
  "run_ts": "2026-05-22-1545",
  "top_status": "in-progress",
  "total": 3,
  "by_status": {"archived": 1, "in-fix-loop": 1, "pending": 1},
  "current": {"seq": 2, "change_id": "add-bar", "status": "in-fix-loop"},
  "changes": [
    {"seq": 1, "change_id": "add-foo", "status": "archived", "rounds": 1},
    {"seq": 2, "change_id": "add-bar", "status": "in-fix-loop", "rounds": 2},
    {"seq": 3, "change_id": "add-baz", "status": "pending", "rounds": 0}
  ]
}
```

**exit**：`0` 成功；`3` 未定位到 active run / STATE_JSON 缺失

---

### `npc cost [--since DUR]`

按后端身份拆 token 成本（Claude vs MiMo/codex 等）。只读 telemetry 派生指标（`events.ndjson`），绝不碰原始 jsonl/transcript。

**参数**：`--since DUR`（可选）：如 `7d` / `24h` / `30m` / ISO 8601，省略则统计全部历史

**做什么**：`review.round` 记录按 `engine` 字段分桶（`codex`/`claude`，缺失归 `review`）；其它含 `tokens` 的记录统一归 `coder` 桶（telemetry 暂无法区分 MiMo vs Claude 在 coder 层的身份，故标注 `method:"heuristic"`）。只把含 `tokens` 估算的 record 计入分桶，避免污染 events 计数。

**stdout**：

```json
{
  "ok": true,
  "since": "7d",
  "by_bucket": {
    "codex": {"events": 6, "est_input_tokens": 42000, "est_output_tokens": 5200, "duration_ms": 360000},
    "coder": {"events": 12, "est_input_tokens": 88000, "est_output_tokens": 21000, "duration_ms": 900000}
  },
  "total": {"events": 18, "est_input_tokens": 130000, "est_output_tokens": 26200, "duration_ms": 1260000},
  "method": "heuristic"
}
```

无数据时 `total` 全 0，仍视为成功（`ok:true`）。

**exit**：`0` 成功；`2` `--since` 格式不合法

---

### `npc clean [--yes] [--keep-days 14]`

清理陈旧/已中止的 run 目录（**默认 dry-run**，只有 `--yes` 才真删）。

**参数**：

- `--yes`（flag）：真删；省略则只输出清理计划，不碰文件系统
- `--keep-days N`（默认 14）：保留窗口天数；必须 `>= 1`（太小会把 cutoff 拉到当下，`--yes` 即刻删光所有终态 run）

**做什么**：可清理判定（三条件全满足才删）：① 非 active run（`run_ts` ≠ `active.json` 的 `current_run_ts`）；② 顶层 `status ∈ {completed, completed-with-issues, aborted}`，或 state 文件缺失/不可读/JSON 解析失败（视为孤儿目录）；③ 目录最后修改时间（含子文件，取最大 mtime）早于 `now - keep_days` 天。只有 run_ts 匹配 `\d{4}-\d{2}-\d{2}-\d{4}` 格式的目录才会被当作 run 候选，避免误删 task_log 下的外来目录。`in-progress` 与 active run 绝不删。

**stdout（dry-run，默认）**：

```json
{"ok": true, "dry_run": true, "removable": ["2026-04-01-0900", "2026-04-15-1200"], "kept_count": 5, "freed_estimate": 2}
```

**stdout（`--yes` 真删）**：

```json
{"ok": true, "dry_run": false, "removed": ["/.../2026-04-01-0900", "/.../2026-04-01-0900-plan-state.json", "/.../2026-04-01-0900-plan-state.md"], "kept_count": 5}
```

**exit**：`0` 成功；`2` `--keep-days < 1`；`3` 非 git 仓库 / task_log_dir 定位失败

---

## 9b. Watchable Tasks / Watch

`npc task ...` 为本机后台任务提供主动上报契约。它只写当前 run 的
`<run_dir>/tasks/`，不改 `plan-state.json` 状态机；`npc watch` 只读这些文件和
active run 的 state。

文件布局：

```text
<run_dir>/tasks/
├── <task_id>.json          # 当前任务快照（权威）
└── <task_id>.events.jsonl  # 追加式任务事件历史
```

`task_id` 必须匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`，用于防止路径穿越。
任务快照会记录 `proj_key/run_ts/session_id`、`worktree{repo_root,worktree_root,git_common_dir,branch,head}`、
`check{type:"heartbeat",stale_seconds}`、`pointer{state_json,run_events,...}`。

### `npc task start`

```bash
npc task start --id implement-001 --description "Implement add-foo" \
  [--source npc|claude-subagent|bash|manual] [--phase PHASE] [--message TEXT] \
  [--stale-seconds 900] [--progress-current N --progress-total N --progress-unit UNIT] \
  [--log PATH] [--summary PATH] [--transcript PATH] [--session-id SID] [--replace]
```

登记一个可观测任务。stdout：

```json
{"ok":true,"task_id":"implement-001","status":"running","task_json":".../tasks/implement-001.json","events":".../tasks/implement-001.events.jsonl"}
```

exit：`0` 成功；`1` 同 id 已存在；`2` task id 非法；`3` 未定位当前 run。

### `npc task update`

```bash
npc task update --id implement-001 [--status running|waiting] [--phase PHASE] \
  [--message TEXT] [--progress-current N --progress-total N --progress-unit UNIT] \
  [--log PATH] [--summary PATH] [--transcript PATH]
```

更新任务快照并追加 `task.updated` 事件。

### `npc task heartbeat`

```bash
npc task heartbeat --id implement-001 [--status running|waiting] [--phase PHASE] [--message TEXT]
```

刷新 `last_heartbeat_at` 并追加 `task.heartbeat` 事件。`watch` 用
`last_heartbeat_at + stale_seconds` 派生 `observed_status=stale`。

### `npc task finish`

```bash
npc task finish --id implement-001 [--status done|failed|cancelled] \
  [--phase PHASE] [--message TEXT] [--summary PATH] [--result TEXT]
```

标记终态并追加 `task.finished` 事件。

### `npc watch`

```bash
npc watch [--once] [--all] [--project PATH] [--interval 2] [--stale-seconds N]
```

- 默认：观测当前 cwd 所属项目的 active run。
- `--once`：输出一次单行 JSON 快照后退出，适合脚本/测试。
- 无 `--once`：循环刷新终端视图。
- `--all`：只扫描 `~/task_log/*/active.json` 指向的 active run，不扫全部历史。
- `--project PATH`：按指定 worktree/project 的 active run 观测。
- 如需指定历史 run，使用全局参数：`npc --task-log-dir PATH --run-ts TS watch --once`。

stdout（`--once`）：

```json
{"ok":true,"schema_version":1,"generated_at":"...","scope":"project","runs":[{"proj_key":"...","run_ts":"...","state":{...},"tasks":[{"task_id":"implement-001","observed_status":"running","heartbeat_age_seconds":2}]}]}
```

---

## 9c. Auto 模式决策器：`npc auto-decide`

`--auto` 模式下的主 session 决策器：把原本 `needs-user-decision` 的触发点统一改成调 `npc auto-decide`，由 npc 基于 progress 数据给出确定性 action（`continue-retry` / `skip` / `force-archive`），主 session 只负责执行，不再向用户问"continue/skip/abort"。

### `npc auto-decide --seq N --trigger <kind> [--apply]`

**参数**：

- `--seq N`（必填）
- `--trigger <kind>`（必填）：`stale` / `max-rounds` / `agent-timeout-exhausted` / `codex-failed` / `implementer-failed` / `fixer-failed` / `summary-missing` / `commit-not-found`
- `--apply`（flag）：直接把建议的 `set_status` / `reason` / `auto_retry_<trigger>` 写回 state；省略则只建议不落盘

**做什么**（纯 state 决策，不读 git、不调 sub-agent；同一 state + trigger 总返回同一 action）：

- `stale` / `max-rounds`：blocking 末值 ≤ 2 且 trend 长度 ≥ 3 → `force-archive`（可接受收尾）；blocking 末值 ≥ 3 或 categories_seen ≥ 6 → `skip`（`oversized-change`）；否则 → `skip`（`<trigger>-cannot-converge`）
- `agent-timeout-exhausted` / `codex-failed`：直接 `skip`（`skipped-auto`），因为 pipeline 已内部重试过
- 其余软失败 trigger（`implementer-failed` / `fixer-failed` / `summary-missing` / `commit-not-found`）：第一次给一次 `continue-retry` 机会（记 `auto_retry_<trigger>` 计数）；再次触发同一 trigger → `skip`

**stdout**：

```json
{
  "ok": true,
  "trigger": "stale",
  "action": "force-archive",
  "reason": "stale-acceptable-blocking-1",
  "set_status": null,
  "seq": 2,
  "change_id": "add-bar",
  "blocking_trend": [5, 3, 1],
  "categories_seen": ["validation"],
  "applied": false
}
```

`continue-retry` 时额外含 `"increment_retry_key": "auto_retry_implementer-failed"`。

**exit**：`0` 成功；`1` seq 超出 progress 数组长度；`2` `--trigger` 不在合法集；`3` 环境错（STATE_JSON 不存在）

---

## 9d. Playbook 分发（v1.7+，去 plugin 化）

v1.7 起仓库不再发布 Claude Code plugin；原 plugin 的 commands / skills / agents 以宿主中立措辞收编为包资源 `src/npc/playbooks/`，经以下命令分发。

### `npc playbook list`

**stdout**：

```json
{"ok": true, "playbooks": [
  {"name": "spine-run", "kind": "command", "summary": "...", "bytes": 11630},
  {"name": "new-plan-changes-v3", "kind": "skill", "summary": "...", "bytes": 40777},
  {"name": "spine-coder", "kind": "agent", "summary": "...", "bytes": 4683}
]}
```

**exit**：`0`

### `npc playbook show <name>`

输出 playbook 原文 markdown 到 stdout——**stdout 单行 JSON 契约的唯一例外**，设计给任意宿主把工作流直接拉进 context 执行。错误路径（未知名字）仍是单行 JSON + exit 2。

**exit**：`0` 成功；`2` 未知 playbook 名

### `npc playbook install (--host claude|codex | --dest DIR) [--name N ...]`

物化 playbooks 到宿主目录（**写盘副作用**；幂等覆盖，升级 npc 后重跑即同步内容）。

- `--host claude`：command → `~/.claude/commands/<name>.md`；skill → `~/.claude/skills/<name>/SKILL.md`；agent → `~/.claude/agents/<name>.md`
- `--host codex`：command/skill → `~/.codex/prompts/<name>.md`；agent 无对应机制 → 记入 `skipped`
- `--dest DIR`：全部平铺为 `DIR/<name>.md`（任意其它宿主自行挂载）
- `--name` 可多次，只装子集；缺省装全部

**stdout**：

```json
{"ok": true, "host": "claude",
 "installed": [{"name": "spine-run", "kind": "command", "path": "/Users/you/.claude/commands/spine-run.md", "replaced": false}],
 "skipped": []}
```

**exit**：`0` 成功；`2` 用法错（--host/--dest 二选一、未知 host 或 playbook 名）；`3` 写盘失败

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

当前契约版本：`v1.5`。

新增命令（v1.5，内环与整合下沉 + 上下文预算契约，详见 §8f）：

- **`npc change run`**：单 change 内环（implement→review→fix 循环→archive）一条命令；决策点 `--auto` 走 auto-decide / 交互档 exit 5 + `--decision` 续跑。
- **`npc integrate`**：worktree 产物整合（verify manifest→cherry-pick→hash 翻译→record→verify tests，失败自动 revert）。
- **`npc status --brief`** / **`npc state note`**：compaction 单命令重入契约 + 编排日志/steering 通道。
- **`npc verify tasks`**：tasks.md 完成度派生计数 × implement 自报交叉验证。
- **`npc state init-run --goal`** + summary **Goal Coverage** 段：run 级人工验收对照表。
- telemetry 新 kind **`deviation`**：决策点/冲突/revert 偏差记账（change run / integrate / auto-decide --apply 自动落）。

新增命令（v1.4，v3 skill 脚本下沉，详见 §8c）：

- **`npc plan waves`**：并行波次候选划分（Kahn 拓扑分层 + 文件交集着色），原 v3 skill 的 `waves.py`。
- **`npc verify manifest`**：implementer 产出核验（RESULT 行双格式 plan-only 判定 + manifest 文件存在性/sha256 核对），合并原 `detect_plan_only.py` + `verify_manifest.py`。
- **`npc notify`**：best-effort webhook 推送（总是 exit 0），原 `notify.py`；URL 支持 `$NPC_WEBHOOK` / `$NPC_V3_WEBHOOK`。

新增命令（v1.3，基础加固，全部经独立 review + 充分测试；完整契约见 §8d / §8a）：

- **`npc doctor`**：环境前置体检，详见 §8d。
- **`npc verify tests`** / **`npc verify routing`**：真实复跑测试 + 路由不变量校验，详见 §8d。
- **`npc implement run --seq N` / `npc fix run --seq N --round M`**：把 coder 子进程编排折进 npc（对标 `review run`），详见 §8a。
- **`npc init --auto`** 新增 `auto_auth`：自动给 `<repo>/.claude/settings.json` 授权（`defaultMode=acceptEdits` + harness 工具 Bash 白名单），**合并保留既有 deny 与其它键**、幂等、坏 JSON 不覆盖、失败不阻塞。交互档不授权。payload 多 `auto_auth` 字段。

配置新增 `[coder]`（backend/mimo.env_file/model/bin）+ `[coder.phase]`（per-phase 后端，如 `fix="mimo"`）+ `[verify]`（test/lint/typecheck/build 覆盖）。MiMo 默认不启用，须显式配置。

| 版本 | 关键变化 |
|---|---|
| **1.7.1** | 文档：宿主支持列表明确为 Claude Code / Kimi CLI / Qwen Code / Codex / OpenCode（README / INSTALL / usage / playbook 宿主适配表口径统一） |
| **1.7** | 宿主中立化 + 去 plugin 发布：新增 `hosts.py` 宿主抽象与 `[host]` 配置（name/session_dir；探测顺序 config > CLAUDECODE env > generic），init payload 增 `host` 字段、generic 宿主跳过 auto 授权、session 识别按宿主分流（generic 只走 by-cwd hook）；focus/templates 项目上下文 `CLAUDE.md`→`AGENTS.md` fallback、prompt 措辞去工具专名；新增 `playbook list/show/install`（§9d），原 plugin 内容收编进包资源，删除 marketplace/plugin manifest；`doctor` 新增 `host` 检查 |
| **1.6** | Provider 注册表：config 新增 `[providers.*]`（runner/env_file/model/bin，内置 claude/mimo/codex），coder 可路由到任意 Anthropic 兼容端点（kimi/qwen/deepseek/...）与 `codex exec`（coder 的 codex-cli 路径补齐）；配置查找链改为分层深合并（全局定义 provider、项目只写路由）；`--backend` 接受 provider 名；`verify routing` 规则 3 更名 `cheap_exec_only` 并泛化到全部带 env_file 的 provider；`doctor` 新增 `providers` 检查 |
| **1.5** | 内环与整合下沉（§8f）：新增 `change run`（单 change 内环编排）与 `integrate`（worktree 产物整合进 main），上下文预算重构 |
| **1.4** | 新增 `plan waves` / `verify manifest` / `notify`：/new-plan-changes-v3 的全部 skill 脚本下沉为契约化子命令，skill 侧零脚本 |
| **1.3** | 新增 `doctor` / `verify tests` / `verify routing` / `implement run` / `fix run`；`init --auto` 自动授权 `.claude/settings.json`；config 增 `[coder]`/`[verify]`。把成本路由、独立验证、复跑测试、auto 授权固化进确定性核心 |
| **1.2** | 新增 `telemetry` 子命令族（emit/tail/agg/hotspots/estimate-tokens）+ events/pipeline/agent 自动 emit 钩子；`~/task_log/_telemetry/events.ndjson` 派生指标流落盘，主 session 仍零接触 |
| 1.1 | 文档与版本对齐（初始 release 即包含 1.0 全部能力） |
| 1.0 | 新增 `agent prompt render` / `agent spawn-prompt`，§A Implementer / §B Fixer 模板从 skill 文档下沉到 CLI 包资源；主 session 不再 Write 模板内容、不再把模板传给 Agent 工具 |
| 0.3 | 新增 pipeline 章节（review run / archive run / implement record / fix record） |
| 0.2 | 子命令自包含；新增 run.json / active.json + 全局 --run-ts / --task-log-dir；`--shell-exports` 标 deprecated |
| 0.1 | 初始契约（NPC_* 环境变量 + 细粒度命令） |

Breaking change（命令更名 / 参数语义变化 / stdout schema 字段移除）触发 major 版本递增；新增字段或新增子命令属于向后兼容。1.0 仅新增能力，未删除 0.x 的任何命令——0.x 调用方可平滑升级。

### 12.1 契约补记（本次修订，非新版本）

一致性审计发现实现（`src/npc/cli.py`）已注册但本文件此前缺失完整契约小节的命令，本次逐一核实实现后补齐（**不算新增能力**，这些命令在各自实现版本即已存在，只是文档漏记）：

- `state repair`（§2）、`phase rotate`（§3）——均为 v1.0 附近既有能力
- `spec analyze` / `plan check` / `plan new-change`（§4a）
- `git branch-for` / `git ensure-clean` / `git commit` / `deliver` / `pr open`（§8e）
- `status` / `cost` / `clean`（§9a）
- `auto-decide`（§9c）
- `agent timeout-budget` / `agent record-timeout`（§7a，1.1+ 渐进退避 timeout 预算）

同时修正两处已实现但文档描述滞后的参数漂移：`review run` 补记 `--engine {codex,claude}`（§8a，默认读 `[review].engine`，缺省 `codex`；stdout 增 `engine` 字段，失败 error 码随引擎变化为 `<engine>-exec-failed`）与 `--config PATH`；`notify` 补记 `--timeout`（默认 `5.0`，§8c）。契约版本仍为 `v1.4`。
