## 1. 计算回退 + 幂等保留

- [x] 1.1 `_do_phase_exit`：`duration_ms` 计算在 `started_ms` 缺失时回退解析 `started_at`（ISO→epoch ms）；两者皆缺才 `null`
- [x] 1.2 review 孪生（pipeline.py:331 附近）同样回退
- [x] 1.3 exit 重写 phase dict 时保留 `started_ms`（连同 `started_at`），二次 exit 仍可正确计算
- [x] 1.4 抽公共 helper（如 `_resolve_started_ms(cur)`）避免两处逻辑漂移

## 2. 测试

- [x] 2.1 仅有 `started_at` 无 `started_ms` → duration 非空且正确
- [x] 2.2 两者皆缺 → duration=null，不抛错
- [x] 2.3 二次 exit（failed→done）→ duration 非空，started_at 保持最初值
- [x] 2.4 implement 端到端：单次成功 record → `duration.phases_ms.implement` 非空
- [x] 2.5 implement 端到端：失败重跑 record → duration 非空
- [x] 2.6 `pytest` 全绿（含既有 phase/pipeline 用例不回归）
