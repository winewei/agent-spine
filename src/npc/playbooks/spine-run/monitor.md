# spine-run §monitor — 登记 job 与处理 monitor 事件

按需读取：follow/tick 输出一行 `monitor: N open, M pending | #ID KIND TASK ... | detail: npc monitor list --open` 且需要判断时读本节。行内 `*` 标记本次新动作，`-> HANDLE` 表示下一步是向该句柄发询问，`asked` 表示询问已发、等待回复，`idle Nm` 表示实际产出已停 N 分钟，`probe-error` 表示探针失败。行里只有决策所需字段；完整结构用 `npc monitor list --open` 或 `--format json` 按需取，不要每次都拉。

默认节奏：最近 10 分钟没有新证据的 agent 每 10 分钟询问一次，证据持续变化的 agent 放宽到每 30 分钟一次；15 分钟无产出标记停滞（`idle`）；已发送询问给 5 分钟回复期限。follow 只在出现新动作（或动作转为无进展）时输出，未处理动作每 30 分钟重复提醒；主 session 自己的 register/ack/finish 不触发输出，空闲时不输出心跳。需要独立 monitor agent 时按事件启动短诊断任务，并计入真实槽位预算。

远程作业、长测试等**无法询问**的执行体登记为 job：由派发它的 agent 在 RESULT 中回报主机、日志、pid 文件与完成标记，主 session 登记后该 agent 直接返回，不在自身会话里等待。

```bash
npc monitor register --id "$JOB_ID" --kind job --role bench --handle "ssh:$HOST" \
  --probe "ssh $HOST 'wc -l < $LOG'" --done "ssh $HOST 'grep -q BENCH_DONE $LOG'" \
  --alive "ssh $HOST 'kill -0 \$(cat $PIDFILE)'" [--deadline-seconds 14400]
```

`--probe` 输出随工作推进变化的进度标记（结果行数、已完成条目数、结果文件大小），输出变化计为进展，首次读数只作基线；`--alive` 连续两次失败才产生 `EXITED_SIGNAL`；job 必须提供 `--done`、`--alive`、`--deadline-seconds` 至少其一。命令在登记时的工作目录中以 `sh -c` 执行，单次超时默认 30 秒（`--probe-timeout`），在锁外执行，不阻塞 ack/register。进度标记必须度量工作产物，不能用 agent 转录的 mtime/size。不属于任何 run 的长期任务用 `--owner NAME` 登记到 host 作用域（`~/task_log/_monitor/NAME`），该作用域的所有 monitor 命令都带同一 `--owner`。

**处理事件**：

1. `CHECK_IN`：对所有到期 agent 并行发送宿主原生询问：“最近一个检查周期完成了什么？提供 commit、diff、测试/review 或分析产物；当前阻塞是什么？下一步和预计完成时间？”发送成功后才执行 `npc monitor ack --action-id ID --decision sent`。相同 action id 不重复询问，`sent_at` 非空表示已发；发不出去先核对句柄和原任务，不能伪造 sent。
2. 收到回复后检查实际证据。`--decision progress --note '证据与结论'` 记录核验；长测试/调研仍合理时 `--decision wait --wait-seconds 600 --note '进程/阶段证据、原因与期限'`，最多等待 900 秒，到期重新询问。ack 和“仍在工作”的口头报告不会重置实际产出时钟。
3. `no_progress=true` 或 `CONTROL_REQUIRED`：主 session 必须诊断，不只转述警报。检查卡住的进程、依赖、重复 findings 和测试；选择缩小任务、补充信息、调整方案、继续有界等待或接替执行，并用 `--decision intervene --note '证据、已采取动作与下一检查点'` 落账。`CONTROL_REQUIRED` 只有询问确已发送且到期未处理才产生；即使发现新产物，也须核验并给出判断。
4. `STALL`（job 无进展）：检查远端日志、进程与目标系统负载，用 `--decision progress|wait|intervene --note` 落账；判定假死时按第 6 条接替。
5. `DONE_SIGNAL` / `EXITED_SIGNAL` / `DEADLINE`：monitor 已停止探测该任务，不会自行关闭它。主 session 核验结果或退出原因后执行 `npc monitor finish --id ID --result done|failed --note '证据'` 或 `npc monitor cancel --id ID --note '原因'`；重启作业或需要继续观察时 `--decision intervene --note`（恢复探测；延期同时传 `--deadline-seconds N`），暂不能判定时用有界 `--decision wait`。终态信号不能以 `progress` 处理。
6. 停止/接替前保存 worktree、提交、RESULT 和日志，确认原执行体已退出；未知状态仍占位。核验收单或退出后执行 `npc monitor finish --id ID --note '收单/退出证据'`；接替者使用新 id，并继续原 worktree。monitor 不自行 kill、不改 Git 或业务 state、不替主 session 发布代码。

文件内容/专属 worktree HEAD 或 diff 变化只是工作证据，不保证有用收敛；证据持续变化只把询问间隔放宽到 30 分钟，不取消询问。转录 mtime、心跳、工具调用次数、共享目标 HEAD 不算实际进展；同根因反复修复须主动重排。未跟踪文件需通过专属 `--artifact` 登记。`npc watch --once` 可补充发现宿主任务，核对是否漏登；不能将其它 session 的历史任务加入本 run。

恢复时先执行 `npc monitor tick` 补算离线期间到期的截止时间与终态，再用 `npc monitor list --open` 核对未关闭任务，然后重新连接唯一后台 monitor（重复 follow 会被拒绝）；清单、待处理 action id、发送记录和期限都会保留。先核验宿主存活任务与 monitor/scheduler 清单是否一致，再续闸；不得重新注册来重置计时。run 完成或中止时核实所有执行体退出、逐项 finish/cancel，确认 `npc monitor list --open` 为空后执行 `npc monitor stop`，follow 会退出。不要因一次扫描没有 agent 就认为 run 已完成。
