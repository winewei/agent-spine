## 1. 聚合累计 + 输出

- [x] 1.1 `aggregate()` 桶新增 `blocking_categories: defaultdict(int)`，遍历事件时按 category 累计（缺字段跳过）
- [x] 1.2 桶输出新增 `top_blocking_categories`（复用 `_top_n_dict`，top-N）
- [x] 1.3 `hotspots()` scored 项新增 `top_blocking_categories`

## 2. 测试

- [x] 2.1 by-phase 聚合：多条 review 事件的 blocking_categories 正确计数
- [x] 2.2 by-change 聚合同样透出
- [x] 2.3 缺 blocking_categories 字段不报错、无贡献
- [x] 2.4 hotspots 项含 top_blocking_categories
- [x] 2.5 `pytest` 全绿（既有 telemetry 用例不回归）
