# run-lessons-extraction

## ADDED Requirements

### Requirement: archive 成功后确定性提炼失败模式条目
`npc lessons record --seq N` SHALL 在目标 change（由 `--seq N` 指定其在 `plan_order` 中的序号，`N` 与 fix round 数字无关）已到达 `archived` 终态后可用，读取该 change 的 `<base>/events.jsonl`，筛选全部 `event == "fix.done" && phase` 匹配正则 `^fix-r\d+$`（即该 change 全部 fix 轮的成功退出事件，round 数字不做任何过滤）的记录，按 phase 中的 round 数字升序提取其 `categories_scanned` / `regressions_added` / `notes` 字段，拼接为一条 markdown 段落追加到 `<run_dir>/lessons.md`（不存在则创建）。相同输入 MUST 产出相同追加结果（不调用任何 LLM，不做语义摘要）。

> **事件契约说明（与真实落盘对齐）**：`npc phase exit <seq> fix-rN --status done`（见 `src/npc/events.py`）向 `<base>/events.jsonl` 追加的行形态为 `{"event":"fix.done","phase":"fix-rN","categories_scanned":..,"regressions_added":..,"notes":..}`——**该文件用 `event` 字段命名事件、以 `.done` 后缀编码成功退出，行内不含 `kind` 或 `status` 字段**。`kind == "phase.exit" && status == "done"` 是**另一条** telemetry 派生流（`~/.local/share/.../events.ndjson`，`telemetry.emit_phase_exit`）的形态，且该派生流**不携带** `categories_scanned` / `regressions_added` / `notes`。因此提炼源只能是 per-change `events.jsonl` 的 `event == "fix.done"` 形态。

#### Scenario: 多轮 fix 的 change 提炼出完整条目
- **WHEN** change-a 经过 2 轮 fix 才收敛，round 1/2 的 `fix-r1`/`fix-r2` done 事件分别自报 `categories_scanned=validation` 与 `categories_scanned=error-handling,validation`
- **THEN** `lessons.md` 中 change-a 的段落含两个类目的并集（去重）与两轮的 `notes` 原文（按 round 顺序）

#### Scenario: 无 fix 轮的 change 不追加条目
- **WHEN** change-b 的 round 0 review 即 `blocking == 0`（未产生任何 `fix-rN` 事件）
- **THEN** `npc lessons record --seq <b>` 返回 `{ok:true, appended:false}`，`lessons.md` 不新增 change-b 的段落

### Requirement: 提炼数据源严格限定为 fixer 自报字段，不含 reviewer 产出
`npc lessons record` 的实现 MUST NOT 读取、打开或引用 `round-N.review.json`、`round-N.focus.md`、`round-N.spec-review.json` 或其中的 category / finding / rubric 原文；提炼字段 MUST 仅取自 `<base>/events.jsonl` 中 `event == "fix.done"`（`fix-rN` 成功退出）事件里 fixer 通过 RESULT 行自报的 `categories_scanned` / `regressions_added` / `notes`。

#### Scenario: 提炼过程不访问 review 产物
- **WHEN** change-a 存在 `round-1.review.json`（含 blocking findings 原文）
- **THEN** `npc lessons record` 的实现路径不读取该文件，`lessons.md` 中不出现其 finding 原文

### Requirement: 幂等追加
对同一 `change_id`，`npc lessons record` 重复调用 MUST NOT 在 `lessons.md` 中产生重复段落；已处理的 change_id 记入 `state.lessons.entries_appended`。

#### Scenario: 重复调用不重复追加
- **WHEN** 对 change-a 连续两次调用 `npc lessons record --seq <a>`
- **THEN** `lessons.md` 中 change-a 的段落只出现一次，第二次调用返回 `{ok:true, appended:false, reason:"already-recorded"}`

### Requirement: 字段缺失时的降级格式
当 `categories_scanned` / `regressions_added` 为空字符串或 `-` 时，对应子项 SHALL 从追加的段落中省略而非留空占位；当该 change 全部三个字段在所有轮次均为空时，SHALL 仍追加一条仅含 `rounds` 计数的最简条目。

#### Scenario: 全字段皆空仍保留返工信号
- **WHEN** change-c 经过 1 轮 fix，该轮 `categories_scanned`/`regressions_added`/`notes` 均为空或 `-`
- **THEN** `lessons.md` 中 change-c 的段落至少含 `rounds: 1`，不含空的类目/notes 子项

### Requirement: 非阻塞 best-effort 契约
`npc lessons record` 的执行失败（`events.jsonl` 缺失、损坏、`lessons.md` 所在目录不可写等）MUST NOT 抛出未捕获异常，SHALL 返回结构化 `{ok:false, error:...}`；调用方（spine-run 主循环）以非阻塞方式调用，失败不回滚已提交的 archive、不重跑。

#### Scenario: events.jsonl 缺失时优雅失败
- **WHEN** 目标 change 的 `<base>/events.jsonl` 不存在
- **THEN** `npc lessons record` 返回 `{ok:false, error:"events-missing"}`，不抛栈
