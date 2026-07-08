## Why

`/spine-analyze`（2026-07-08）指出主导成本是 review-fix 长尾（r0=89→r11=1，r0 仅 ~8% 一次过，fix+review ≈ 全部 token 的 ~80%），却看不见**哪类 blocking finding 反复触发再 review**——`npc telemetry hotspots` 与 `agg` 的 by-phase/by-change 输出没有 `top_blocking_categories`，无法定位"打地鼠"根因（同类反复漏扫 vs fix 引入新回归）。

数据其实齐备：`review.round` 事件已带 `blocking_categories`（`src/npc/telemetry.py:emit_review_round`，实测事件含 `"blocking_categories": [...]`）。缺口纯在 `aggregate()` 未把该字段 roll up、`hotspots()` 未透出。补上后即可针对性改 focus 模板 / coder 根因扫描规则，缩短长尾。

## What Changes

- `aggregate()`：在 by-phase / by-change / by-week 桶中累计 `blocking_categories`（按 category 计数），输出新增 `top_blocking_categories`（top-N 分布）。
- `hotspots()`：scored 项新增 `top_blocking_categories`，让 hotspot 视图直接暴露某 phase 的主要 blocking 类别。
- 仅新增派生字段，不改事件 schema、不改既有输出字段——纯增量。
- 补测试：带 `blocking_categories` 的事件聚合后 by-phase/by-change 出现正确的 `top_blocking_categories`；无该字段的事件不报错。

## Capabilities

### New Capabilities

- `blocking-category-aggregation`: review blocking category 进入 telemetry 聚合与 hotspots 的契约。

### Modified Capabilities

## Impact

- `src/npc/telemetry.py`（`aggregate` 桶累计与输出、`hotspots` 透出）
- `tests/`（聚合 top_blocking_categories、hotspots 透出、缺字段容错）
