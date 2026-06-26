## Why

worktree-per-run 把 `proj_key` 按 worktree 路径重键，导致 telemetry 记录里的 `proj_key` 变成 per-run 的 worktree 路径，跨 run 按逻辑工程分组会碎掉。需要在记录里补一个稳定的 `canonical_proj_key`（主 checkout 的 proj_key），让全局 `events.ndjson` 仍能按逻辑工程聚合。

## What Changes

- telemetry 记录（agent_spawn 等事件）增加 `canonical_proj_key` 字段，取自 run.json 的 `canonical_proj_key`（worktree 模式）；非 worktree 模式时等于 `proj_key`。
- 不改既有 `proj_key` 字段语义（仍是本 run 的存储键），只新增分组维度。

## Capabilities

### New Capabilities
- `telemetry-canonical-grouping`: telemetry 记录携带 canonical_proj_key 以支持跨 run 逻辑工程分组。

### Modified Capabilities

## Impact

- `src/npc/telemetry.py`：emit 路径补 `canonical_proj_key`。
- `src/npc/telemetry_schema_v1.json`：字段补充（可选/向后兼容）。
- `tests/`：worktree 模式记录含 canonical_proj_key；非 worktree 模式 = proj_key。
