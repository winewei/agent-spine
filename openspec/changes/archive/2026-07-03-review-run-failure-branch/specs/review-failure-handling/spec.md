## ADDED Requirements

### Requirement: review run 失败必须有确定性分支

spine-run 主循环 MUST 在读取 `npc review run` 返回的 blocking/stale 前先检查 `.ok`：round0 失败 MUST 不进入 review-fix 循环、循环内失败 MUST 退出循环，两者均转 3d 决策点（auto 档 `--trigger codex-failed`）。MUST NOT 让缺失字段（null）参与循环条件判断。

#### Scenario: round0 review 失败直接转决策点

- **WHEN** `npc review run --round 0` 返回 `ok=false`（如 codex-exec-failed，返回体无 blocking）
- **THEN** 主循环不进入 while 循环，auto 档调用 `npc auto-decide --trigger codex-failed`（返回 skip），按 action 执行

#### Scenario: 循环中 review 失败退出循环

- **WHEN** review-fix 循环第 N 轮的 `npc review run` 返回 `ok=false`
- **THEN** 循环立即退出并转 3d（trigger=codex-failed），不对 null blocking 做整数比较

#### Scenario: review 成功路径不变

- **WHEN** `npc review run` 返回 `ok=true`
- **THEN** 循环按 blocking/stale/轮数上限的原契约继续
