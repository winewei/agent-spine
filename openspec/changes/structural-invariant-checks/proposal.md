## Why

`/spine-analyze` + scout-run 调研（`../spine-scout/scout-runs/2026-07-08T1120/report.md`）确认：`validation` 是 review 的真实最高频方差点（blocking_categories 30d 每轮霸榜 r0=65→r1=57→r2=38→r3=29…），是 review-fix 长尾（~80% token 成本）的头号收敛杀手。

调研的关键 pivot（经 critic 证伪 + rebuttal 确认）：**coder 侧的 prompt 自检脚手架结构上拦不住最高频的两类 validation 缺陷**——
1. **跨模块接线/消费缺失**：值算出来了却在 emit 前被丢、或写进 state 却无消费方（样本里跨 4 个 change 复发：`tests_verified` 未透传 telemetry、cage 统计未连真实数据源、DAG 字段未暴露、诊断字段跨三轮未 emit）。coder 自省矩阵问"guard 覆盖什么条件"，与"输出被谁消费"维度不匹配。
2. **schema 只查部分必需键**：RESULT/契约解析只校验子集字段，漏 `tasks`/`summary`/`categories_scanned` 等。

这两类**不是 coder 自检能防的**（盲区与缺陷同源），也**不该靠 reviewer 反复揪**。真正高杠杆是**确定性自动化结构检查**——静态、可复现、非 coder 自报、非 reviewer 主观判据，因此不违反核心不变量 1「生成⊥验证」。

**边界（CLAUDE.md）**：这些是对 agent-spine 自身代码的**结构检查**，非跨项目通用原子操作，故 **MUST NOT 进 `npc verify`**（那里只放路由不变量）。落在 agent-spine **自己的检查/测试套件**（pytest 结构不变量测试，跑在既有 `uv run pytest` 内），与 npc harness 职责边界隔离。

## What Changes

- 新增 `tests/test_structural_invariants.py`：确定性结构不变量测试（monkeypatch 捕获真实输出 / 静态常量比对 / AST），违反即 fail，跑在既有测试套件内。
- **R1 telemetry emit 两层字段契约**（最高频；经 codex review 收紧）：真正的缺陷发生在**调用点**（`record_implement` 算出 `tests_verified` 写入 phase extra，但 `_do_phase_exit` 只把 `engine` 透传给 `emit_phase_exit`，字段在调用点丢失），单靠"对 emit 构造样本事件"抓不住。故拆两层代码常量契约：`EMIT_FIELD_CONTRACT`（emit 输出）+ `PHASE_EXIT_EXTRA_CONTRACT`（调用点 handoff）；测试用 monkeypatch `telemetry.emit_event` 捕获**真实 emit 产出**并跑最小 record/phase-exit 流程，断言调用点未丢已算出字段。单一事实源为**代码常量**（近 emit），非 JSON schema。
- **R2 record RESULT 必需键强制校验**（经 codex review 收紧）：现有 `_parse_result_line(text, keys)` 收了 `keys` 却不校验、直接返回——正是失败形态。改为 `RESULT_REQUIRED_KEYS: dict[phase, set]` 单一事实源，解析器 MUST 引用它并对缺键**返回失败+指明缺失键**；测试用"恰好缺一个键"的负例验证 parser/record 失败，不只 AST 断言常量被引用。
- **R3 hook fixture 静态回归**（经 codex review 收窄）：不做会随外部 Claude Code 语义漂移的通用 matcher 引擎；改为静态回归——`plugins/agent-spine/hooks/hooks.json` 的 SubagentStop matcher MUST == `spine-coder`，并用 realistic payload 证明 hook 会触发。语义来源为仓库内 fixture。
- 明确不做：进 `npc verify`（撞边界）；coder 侧新 prompt 脚手架（rebuttal 证其对这两类无效）；通用 hook 语义引擎（漂移风险）；心智错误类（校验错对象）与需领域语义判断的逻辑精确度——这些仍靠 `npc review run` 独立引擎兜底。

## Capabilities

### New Capabilities

- `structural-invariant-checks`: agent-spine 自身代码的确定性结构不变量检查族（telemetry-emit 字段契约、record 必需键完整性、hook-matcher 语义），落项目测试套件、不进 npc verify。

### Modified Capabilities

## Impact

- `tests/test_structural_invariants.py`（新增结构不变量测试）
- `src/npc/telemetry.py` / `telemetry_schema_v1.json`（如需引入 per-kind emit 字段契约单一事实源）
- `src/npc/pipeline.py` / `templates.py`（如需把 RESULT 必需键抽为单一事实源常量供测试引用）
- 无 npc 对外契约破坏；不改 `npc verify` 语义
- 溯源：`../spine-scout/scout-runs/2026-07-08T1120/report.md`（建议段）、`docs/optimization-proposals/2026-07-08.md`
