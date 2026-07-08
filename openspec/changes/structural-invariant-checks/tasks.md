## 1. telemetry emit 两层字段契约（R1，最高频）

- [x] 1.1 建 `EMIT_FIELD_CONTRACT: dict[kind, set[field]]`（emit 输出契约，代码常量，近 telemetry.py emit 函数）
- [x] 1.2 建 `PHASE_EXIT_EXTRA_CONTRACT`（调用点 handoff 契约）：声明哪些已算出并写入 phase/record 的字段 MUST 被 `_do_phase_exit` 的 telemetry 透传（覆盖 tests_verified 等被丢字段）
- [x] 1.3 `tests/test_structural_invariants.py`：monkeypatch `telemetry.emit_event` 捕获真实 emit 事件，断言含 `EMIT_FIELD_CONTRACT` 全部字段（缺则 fail 指明 kind+field）
- [x] 1.4 跑最小确定性 record/phase-exit 流程（或对 `_do_phase_exit` 透传逻辑 AST 检查），断言调用点未丢 handoff 契约中已算出的字段
- [x] 1.5 新增 emit kind / 新增已算出字段未登记两层契约 → fail
- [x] 1.6 （连带修复）`_do_phase_exit` 透传补齐 handoff 契约字段，使新测试转绿

## 2. record RESULT 必需键强制校验（R2）

- [x] 2.1 建 `RESULT_REQUIRED_KEYS: dict[phase, set]`（implement/fix/failure，单一事实源）
- [x] 2.2 `_parse_result_line`/record 引用该常量，缺任一必需键 → 返回 `ok:false` 并指明缺失键
- [x] 2.3 测试：对每个必需键构造"恰好缺该键"的 RESULT 负例，断言 record 失败并报告缺失键（不只 AST 断言引用）
- [x] 2.4 implement 与 fix 各自必需键分别用负例强制

## 3. hook fixture 静态回归（R3，收窄）

- [x] 3.1 静态回归：`plugins/agent-spine/hooks/hooks.json` 的 SubagentStop matcher == `spine-coder`
- [x] 3.2 realistic SubagentStop payload 证明 hook 触发校验路径（复用/对齐既有 test_subagent_stop_hook.py 范式）
- [x] 3.3 不实现通用 Claude Code matcher 语义引擎；语义来源限仓库内 fixture

## 4. 边界与回归

- [x] 4.1 确认未新增任何 `npc verify` 子命令、未改 verify 契约（守 CLAUDE.md 边界）
- [x] 4.2 全部确定性（monkeypatch 捕获 / 静态常量 / AST），无 LLM、无运行时随机
- [x] 4.3 `uv run pytest` 全绿（新测试 + 既有不回归；含 1.6 连带修复后转绿）
