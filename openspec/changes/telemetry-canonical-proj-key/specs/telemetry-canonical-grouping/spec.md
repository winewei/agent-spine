## ADDED Requirements

### Requirement: telemetry 记录携带 canonical_proj_key

telemetry 事件记录 MUST 包含 `canonical_proj_key` 字段，使全局 `events.ndjson` 即便 `proj_key` 为 per-worktree 存储键，仍能按逻辑工程聚合。

#### Scenario: worktree 模式记录取 canonical

- **WHEN** worktree 模式下写出 telemetry 事件
- **THEN** 记录含 `canonical_proj_key`，等于 run.json 的 `canonical_proj_key`（主 checkout 的 proj_key）

#### Scenario: 非 worktree 模式回退

- **WHEN** `--no-worktree` 或旧 run（run.json 无 canonical 字段）
- **THEN** `canonical_proj_key` 等于该 run 的 `proj_key`
