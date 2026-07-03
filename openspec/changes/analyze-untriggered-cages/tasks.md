## 1. telemetry 统计

- [x] 1.1 `npc telemetry cages --since <window>`：扫描事件流，按笼子维度计数（stale、max-rounds、timeout、routing violation、tests 复跑失败、auto-decide 各 trigger）
- [x] 1.2 输出单行 JSON：`cages`（name→count）、`untriggered`（count=0 且有数据源）、`no_data`（事件类型从未 emit，与 0 触发区分）、`runs_observed`
- [x] 1.3 cli.py 注册子命令

## 2. spine-analyze 维度

- [x] 2.1 spine-analyze.md Step 1 增拉 `npc telemetry cages`
- [x] 2.2 Step 2 增「未触发笼子」信号识别：untriggered 且 runs_observed ≥ 阈值 → 候选删除项
- [x] 2.3 Step 3 建议格式支持「减法建议」条目（删哪个笼子、依据、验证方式），仍是只建议不实施

## 3. 测试

- [x] 3.1 构造事件流：有触发/0 触发/无数据三类笼子 → 分类正确
- [x] 3.2 时间窗口过滤（--since）生效
- [x] 3.3 空 telemetry 目录 → 全 no_data、不报错
- [x] 3.4 `pytest` 全绿
