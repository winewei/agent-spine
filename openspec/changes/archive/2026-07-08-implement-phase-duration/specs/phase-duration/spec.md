# phase-duration Specification

## Purpose

保证 phase exit 计算的 `duration_ms` 在两类既有缺陷下仍然正确：(1) 同一 phase 被 exit 两次（重试路径）；(2) phase 仅有 `started_at`（ISO）而无 `started_ms`（epoch）。目标是消除 `implement` 等经由 `_do_phase_exit` 的 phase 的系统性 `duration_ms=null`。纯确定性，不改对外字段契约。

## ADDED Requirements

### Requirement: duration_ms 计算在 started_ms 缺失时回退 started_at

`_do_phase_exit`（及其 review 孪生）计算 `duration_ms` 时，MUST 优先用 `started_ms`；当 `started_ms` 缺失但 `started_at`（ISO 时间串）存在时，MUST 由 `started_at` 解析出 epoch 毫秒再计算 `duration_ms = max(0, done_ms - started_ms)`。仅当 `started_ms` 与 `started_at` 皆缺失时 `duration_ms` 才为 `null`。

#### Scenario: 仅有 started_at 仍能算出时长

- **WHEN** 某 phase 的 state 记录只有 `started_at` 而无 `started_ms`，此时对其 `_do_phase_exit`
- **THEN** `duration_ms` 为非空正整数，等于 `done` 时刻与 `started_at` 之差（毫秒）

#### Scenario: 两者皆缺才为 null

- **WHEN** 某 phase 既无 `started_ms` 也无 `started_at`
- **THEN** `duration_ms` 为 `null`（不抛错）

### Requirement: phase exit 幂等保留 started 基准

`_do_phase_exit` 重写 phase dict 时，MUST 保留可用于重算的 started 基准（`started_ms` 与 `started_at`），使同一 phase 被 exit 第二次时仍能得到与首次一致的正确 `duration_ms`，而非 `null`。

#### Scenario: 二次 exit 不丢时长

- **WHEN** 某 phase 先被 `_do_phase_exit`（status=failed），随后（如修复环境后重跑 record）再次被 `_do_phase_exit`（status=done）
- **THEN** 第二次 exit 的 `duration_ms` 为非空正整数，反映从最初 enter 到本次 exit 的时长
- **AND** 该 phase 的 `started_at` 保持为最初 enter 的时刻

### Requirement: implement phase 时长贯通到派生指标

经 `npc implement run` → coder → `npc implement record` 正常交付（含"失败重跑一次 record"路径）后，该 change 的 `implement` phase MUST 具备非空 `duration_ms`，且该值 MUST 出现在 `spec-report.json` 的 `duration.phases_ms.implement` 与 `npc telemetry agg` 的 `by.phase.implement.p50_duration_ms`。

#### Scenario: 单次成功 record 后 implement 有时长

- **WHEN** 一个 change 正常 implement→record（record 一次成功）后渲染 spec-report
- **THEN** `duration.phases_ms.implement` 为非空正整数

#### Scenario: 失败重跑 record 后 implement 仍有时长

- **WHEN** implement record 首次因 `rerun-tests-failed` 失败、修复后二次 record 成功
- **THEN** `implement` phase 的 `duration_ms` 非空，反映从 enter 到二次 record 的时长
- **AND** `npc telemetry agg` 的 `by.phase.implement.p50_duration_ms` 非空
