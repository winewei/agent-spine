## 1. 修 ensure_schema 的 write-once 缺陷（先做，否则后续全部静默无效）

- [x] 1.1 写测试（RED）：`schema_path` 存在但内容缺 `spec_attribution` → 调用 `ensure_schema` 后文件被重写，`json.loads(...)` 等于 `REVIEW_SCHEMA`
- [x] 1.2 写测试（RED）：内容已等于 `REVIEW_SCHEMA` 时连续两次调用 → `st_mtime` 不变（幂等，不重写）
- [x] 1.3 写测试（RED）：内容为 `REVIEW_SCHEMA` 的不同缩进/不同键序序列化 → 不重写（语义相等判定）
- [x] 1.4 写测试：`schema_path` 不存在 → 创建文件与父目录，内容等于 `REVIEW_SCHEMA`
- [x] 1.5 在 `src/npc/schema.py` 改 `ensure_schema`：读盘 → `json.loads` → 与 `REVIEW_SCHEMA` 比对 → 不等则重写；解析失败视为不等（重写）
- [x] 1.6 跑 1.1–1.4 确认 GREEN

## 2. 扩 REVIEW_SCHEMA（TDD）

- [x] 2.1 写测试（RED）：`REVIEW_SCHEMA` 的 finding `properties.spec_attribution.enum == ["spec-silent","spec-ambiguous","spec-contradicted","impl-deviation"]`，且 `"spec_attribution" in required`，且 `additionalProperties is False`
- [x] 2.2 写测试（RED）：用 `jsonschema` 校验 `spec_attribution == "maybe-spec"` 的 finding → 校验失败
- [x] 2.3 在 `REVIEW_SCHEMA` 加 `spec_attribution` 属性（含四值 enum 与逐值语义的 `description`），并加入 `required`
- [x] 2.4 跑 2.1–2.2 确认 GREEN

## 3. prompt 与 schema 同步（否则 reviewer 不知道要填什么）

- [x] 3.1 在 `src/npc/focus.py` 给 reviewer 的输出要求文案中，补充 `spec_attribution` 的四值语义说明（对照 `focus.py:212` 既有的「每条 finding 必须包含 id / severity / …」一行同步扩写）
- [x] 3.2 确认 Round 0 与后续轮次的输出要求文案**同源**，不出现两份可能漂移的枚举说明

## 4. parse_review 派生 + 向后兼容（TDD）

- [x] 4.1 写测试（RED）：全部 findings 无 `spec_attribution` 键、其中 2 条 in_scope high → 不抛异常，`.spec_attribution_counts["unknown"] == 2`，`.blocking == 2`
- [x] 4.2 写测试（RED）：in_scope blocking 归因为 `spec-silent`×2 + `impl-deviation`×1 → 对应计数为 2/1，`unknown == 0`
- [x] 4.3 写测试（RED）：仅有 1 条 `severity=="low"` 且带归因的 finding → `spec_attribution_counts` 全部值之和为 `0`（advisory 不计入）
- [x] 4.4 写**回归**测试：两份 findings 完全相同、仅归因值不同的 review JSON → `.blocking` 与 `.advisory` 相等（证明归因不参与 blocking 判定）
- [x] 4.5 在 `parse_review()` 加 `spec_attribution_counts` 派生：仅统计 `in_scope and severity in BLOCKING_SEVERITIES` 的 finding；缺字段计入 `unknown`
- [x] 4.6 跑 4.1–4.4 确认 GREEN

## 5. telemetry 贯通（TDD）

- [x] 5.1 写测试（RED）：monkeypatch `emit_event` 捕获真实 emit 的 `review.round` 事件字典 → 其键集合等于 `EMIT_FIELD_CONTRACT["review.round"]`，且含 `spec_attribution_counts`（**必须捕获真实事件，不得只断言样例 dict**）
- [x] 5.2 在 `telemetry.EMIT_FIELD_CONTRACT["review.round"]` 加 `spec_attribution_counts`
- [x] 5.3 在 `src/npc/telemetry_schema_v1.json` 声明 `spec_attribution_counts` 为 object 类型
- [x] 5.4 在 `src/npc/pipeline.py` 的 review.round emit 处透传该字段（**核查是否存在与 `tests_verified` 同类的「算了但没 emit」漏传缺陷**）
- [x] 5.5 跑 5.1 确认 GREEN

## 6. 聚合与比率（TDD）

- [x] 6.1 写测试（RED）：两条事件 `{"spec-silent":2,"impl-deviation":2,"unknown":0}` 与 `{"spec-ambiguous":1,"impl-deviation":3,"unknown":5}` → `spec_attributable_blocking_rate == 0.375`
- [x] 6.2 写测试（RED）：全部事件仅含 `unknown` → 比率为 `null`（**不得为 0**）
- [x] 6.3 写测试（RED）：混入不含 `spec_attribution_counts` 键的历史事件 → `npc telemetry agg` exit 0，该事件被忽略
- [x] 6.4 在 `telemetry.aggregate()` 累加 `spec_attribution_counts`（参照既有 `blocking_categories` 的 `defaultdict(int)` 模式，不另造一套），输出 `spec_attributable_blocking_rate`
- [x] 6.5 跑 6.1–6.3 确认 GREEN

## 7. 守不变量 1 的负向防护（TDD）

- [x] 7.1 写**负向**测试（RED）：某 in_scope blocking finding 的 `spec_attribution == "spec-silent"` → `npc fixer findings` 的渲染输出**不含**子串 `spec_attribution`，**不含**子串 `spec-silent`
- [x] 7.2 确认 `src/npc/fixer.py` 的渲染逻辑按白名单取字段（而非把整个 finding dict 倾泻出去）；若为后者，改为显式白名单
- [x] 7.3 跑 7.1 确认 GREEN

## 8. 非目标守护（防止实现期悄悄越界）

- [x] 8.1 断言 `npc auto-decide` 的 `VALID_TRIGGERS` 集合**未新增任何项**
- [x] 8.2 断言本 change 未修改 `BLOCKING_SEVERITIES` 常量
- [x] 8.3 断言本 change 未给 `category` 字段添加 `enum`（属独立 change）
- [x] 8.4 grep 确认代码中不存在任何基于 `spec_attributable_blocking_rate` 的比较/阈值/分支

## 9. 收尾

- [x] 9.1 跑全量 `uv run pytest -q`
- [x] 9.2 用一份真实历史 `review.json`（如 `~/task_log/.../round-1.review.json`）跑 `npc review parse`，确认不抛异常且 `unknown` 计数正确
- [x] 9.3 跑 `npc telemetry agg --since 90d`，确认在混有大量历史事件时 exit 0 且 `spec_attributable_blocking_rate` 为 `null`（此时尚无新事件）
