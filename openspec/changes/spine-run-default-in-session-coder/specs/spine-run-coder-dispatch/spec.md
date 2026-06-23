## ADDED Requirements

### Requirement: 编排者按 deferred 指令分发 coder

`spine-run` 编排 skill MUST 在 implement/fix 阶段读取 `npc implement/fix run` 返回的 `deferred` 字段：为 `true`（in-session）时走 subagent 三步，否则（headless）走一行跑完。premium（claude）后端 MUST 默认走 in-session 三步。

#### Scenario: claude 后端默认走 in-session 三步

- **WHEN** 编排者对 claude 后端跑 implement
- **THEN** skill 文档指示：`npc implement run` 返回 `deferred=true` 指令 → `Agent(subagent_type=spine-coder, prompt=spawn_prompt)` → 抽末尾 `RESULT:` 行 → `npc implement record`

#### Scenario: mimo 后端走 headless 一行跑完

- **WHEN** 编排者对 mimo 后端跑 implement/fix
- **THEN** skill 文档指示走 headless：`npc implement/fix run` 一行内完成 spawn→record，无需编排者 spawn subagent

#### Scenario: 计费理由在文档中可追溯

- **WHEN** 读者查阅 `spine-run.md` 或 `docs/principles.md` 不变量 4
- **THEN** 能看到 premium coder 走 in-session 的理由：对冲 headless `claude -p` 被切出订阅的计费风险（in-session subagent 属交互式、官方豁免）
