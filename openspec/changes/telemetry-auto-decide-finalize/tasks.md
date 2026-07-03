## 1. auto-decide 决策事件

- [x] 1.1 `auto_decide.cli` 决策计算后 emit telemetry：kind=`auto_decide.decision`，字段 trigger/action/reason/seq/change_id/applied
- [x] 1.2 telemetry 写失败不影响 stdout JSON 与 exit code（容错）

## 2. finalize / ff-merge 事件

- [x] 2.1 `state.py` finalize 成功路径 emit kind=`run.finalize`：status/merged_back/worktree_removed/spine_branch/change 终态计数
- [x] 2.2 finalize 因 incomplete 报错路径也 emit（status=incomplete），便于统计悬挂率

## 3. 测试

- [x] 3.1 auto-decide 各 trigger 决策后事件落盘、字段齐全
- [x] 3.2 finalize merged_back=true / false 两态事件落盘
- [x] 3.3 telemetry 目录不可写时主流程不受影响
- [x] 3.4 `pytest` 全绿
