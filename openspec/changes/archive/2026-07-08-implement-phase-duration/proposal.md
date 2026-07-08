## Why

`/spine-analyze`（2026-07-08）与 spec-report 双双暴露：`implement` phase 的耗时系统性缺失——`npc telemetry agg` 近 30d 内 `by.phase.implement` 有 `count=109`、`est_input_tokens_sum=98,485`，却 `p50_duration_ms=null`、`duration_ms_sum=null`；单 run 的 `spec-report.json` 里 `duration.phases_ms.implement=null`，而同 change 的 `review-r0`、`archive` 均有值。

根因在 `src/npc/pipeline.py:_do_phase_exit`（及 331 的孪生 review 版）：exit 时 `duration_ms` **只从 `started_ms`（epoch 毫秒）计算**，且重写 phase dict 时 `new_phase` **只保留 `started_at`（ISO）、丢弃 `started_ms`**。于是：

1. **二次 exit 必丢时长**：任何 phase 被 exit 两次（失败 record → 修环境 → 重跑 record；或 continue-retry 重试路径），第二次读到的 `started_ms` 已被首次 exit 抹掉 → `duration_ms=null`。本 run 实测：implement `started_at=10:23:27`、`done_at=10:42:24`（相差 19min），`duration_ms` 仍为 `null`。
2. `started_at` 一直在（new_phase 保留它），却因计算只认 `started_ms` 而白白浪费——明明能算却不算。

耗时是 hotspot 评分（`failure_rate × p50_duration × retry`）和成本分析的核心维度，implement 恒空等于让 `/spine-analyze` 对"写代码到底花多久"这一主成本项半盲。

## What Changes

- `_do_phase_exit` 与其 review 孪生：`duration_ms` 计算在 `started_ms` 缺失时 **回退解析 `started_at`（ISO → epoch ms）**；两者皆缺才 `null`。
- exit 重写 phase dict 时 **保留 `started_ms`**（连同 `started_at`），使二次 exit 仍能正确计算，幂等不丢时长。
- 补测试：单次 exit、二次 exit（重试路径）、仅有 `started_at` 无 `started_ms` 三种情形均得非空且正确的 `duration_ms`；`implement` 走 record 后 `duration.phases_ms.implement` 非空且进入 `npc telemetry agg` 的 `by.phase.implement.p50_duration_ms`。

## Capabilities

### New Capabilities

- `phase-duration`: phase 计时在 exit 路径上的鲁棒性契约（started_ms 缺失回退 started_at、二次 exit 幂等保时长）。

### Modified Capabilities

## Impact

- `src/npc/pipeline.py`（`_do_phase_exit` 及 review 孪生的 duration 计算与 phase dict 重写）
- `tests/`（二次 exit / started_at-only / implement 端到端时长）
- 无对外契约破坏：仅把恒 `null` 的 `duration_ms` 修成有值；下游读取者（spec-report / telemetry agg / summary）字段不变。
