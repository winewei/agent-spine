# coder-no-stub-guardrails

为 coder 侧加反 stub / 反删测硬规则：(a) spine-coder agent 契约（plugins/agent-spine/agents/spine-coder.md）Guardrails 增加「禁止以 stub/占位实现充数勾 task；禁止删除、注释或 skip 任何既有测试来换 tests=pass」；(b) src/npc/templates.py 的 SELFCHECK_CATEGORIES 增加 no-stub 类目并在 SELFCHECK_RUBRIC_MD 补对应自查要点（遵守不变量 1：类目同名、细则不共享）；(c) review focus 模板（src/npc/focus.py）增加 blocking 判据：stub/占位实现、被删除或弱化的测试 = blocking，需要多段注释自我辩护的实现视为可疑。依据 docs/optimization-proposals/2026-07-09-bun-migration-lessons.md 提案 2。
