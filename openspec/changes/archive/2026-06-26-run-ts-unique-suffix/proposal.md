## Why

`make_run_ts` 当前是**分钟粒度**（`YYYY-MM-DD-HHMM`）。worktree-per-run 隔离落地后，同一分钟内两个 `/spine-run` init 会派生**相同 run_ts** → 相同 worktree 路径 + 相同 `spine/<run_ts>` 分支 → 硬冲突。即便没有 worktree，同分钟两 run 也会撞 state 文件名（既存潜在 bug）。需要 run_ts 全局唯一。

## What Changes

- `make_run_ts` 在分钟前缀后追加**唯一后缀**（秒 + 进程标识，如 `2026-06-23-1758-23a1`），保证并发/同分钟 init 永不撞。
- 前缀 `YYYY-MM-DD-HHMM` 保留以维持人类可读与排序。
- 确认 resume 探测（按文件名解析 run_ts）对新格式仍正确——run_ts 作为完整不透明字符串使用，不在中途按固定长度截断。

## Capabilities

### New Capabilities
- `run-ts-uniqueness`: run_ts 生成的唯一性契约。

### Modified Capabilities

## Impact

- `src/npc/paths.py`：`make_run_ts`。
- `tests/`：唯一性 + 格式 + resume 解析回归。
- 影响所有 run 目录/分支命名（向后兼容：旧 run_ts 仍可解析，新 run 多一段后缀）。
