# spine-run §finish — 收尾与最终汇报

按需读取：FINISHED 覆盖全部 NODES（或中止）时读本节。

## Step 4 — 收尾

```bash
npc state finalize && npc summary render && npc index append
# 已核验并 finish/cancel 所有执行体，list --open 为空后停止监控
npc monitor list --open
npc monitor stop
npc cost --since "$RUN_T0"
```

`finalize` 若因 `needs-user-decision` 返回 exit 1：先把悬而未决的 change 按 3d 的决策分支处理掉再重跑 finalize。收尾汇报对照 run-summary.md 的 Goal Coverage 段提示用户核对缺口（逐 change 全过 ≠ 组合达标），缺口给出新 change 建议清单。耗时汇报区分实测和估算：资源下界之外，关键路径、尾部收尾、限流与组合验证也要计入。

## 最终汇报

```
## Spine Run 完成：<final_status>

**模式**：auto | interactive        **调度**：pipeline（max-parallel N）| serial
**计划**：N changes / L 层           **结果**：archived A / failed F / skipped S
**用时**：<duration>                 **并发峰值**：<max parallel 实际达到>

### 各 change
- change-a  archived @ <commit>  (review 2 轮, 层 1)
- change-b  archived @ <commit>  (review 0 轮, 层 1)
- change-c  skipped — <reason>

### Goal Coverage 缺口
- <run-summary.md 指出的组合缺口与建议新 change>

### 轨迹与日志（供后续分析）
- 状态：<state_json 路径>
- 汇总：<run-summary.md 路径>
- 跨 run 指标：~/task_log/_telemetry/
- 想优化本 harness？跑 `/spine-analyze`
```
