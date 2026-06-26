## ADDED Requirements

### Requirement: run_ts 全局唯一

`make_run_ts` MUST 生成全局唯一的 run timestamp，使得同一分钟内的并发调用产出不同值，从而 worktree 路径与 `spine/<run_ts>` 分支永不冲突。run_ts MUST 以 `YYYY-MM-DD-HHMM` 为可读前缀，后接唯一性后缀。

#### Scenario: 同分钟两次调用产出不同 run_ts

- **WHEN** 在同一分钟内连续两次调用 `make_run_ts`
- **THEN** 两次返回值不相等

#### Scenario: 前缀保持可读且可排序

- **WHEN** 生成 run_ts
- **THEN** 其以 `YYYY-MM-DD-HHMM` 开头，按字典序排序与时间顺序一致

#### Scenario: 既有 run_ts 解析不破坏

- **WHEN** resume 探测从既有 state 文件名解析 run_ts
- **THEN** 新旧格式的 run_ts 均作为完整字符串正确还原（不按固定长度截断）
