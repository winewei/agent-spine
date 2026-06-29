## 1. canonical_proj_key 注入

- [x] 1.1 `telemetry.py`：emit 事件时补 `canonical_proj_key`，取自 run.json（worktree 模式）
- [x] 1.2 回退：无 canonical 字段时 = `proj_key`
- [x] 1.3 `telemetry_schema_v1.json`：补字段（向后兼容、可选）

## 2. 测试

- [x] 2.1 worktree 模式事件含 canonical_proj_key = canonical
- [x] 2.2 非 worktree / 旧 run 回退 = proj_key
- [x] 2.3 `pytest` 全绿
