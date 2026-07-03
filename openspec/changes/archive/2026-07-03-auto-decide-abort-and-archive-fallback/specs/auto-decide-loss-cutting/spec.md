## ADDED Requirements

### Requirement: auto 档决策空间闭合且无悬挂路径

`_decide` MUST 存在可达的 abort 路径：检测到系统性阻塞（同一 trigger 连续 N 次或 skipped-auto 比例超阈值）时返回 `action=abort`。force-archive 后 archive 再失败时主循环 MUST 触发二次决策并在 skip/abort 中收敛，保证每个 change 最终落在终态、`npc state finalize` 不因 3d 路径悬挂。

#### Scenario: 系统性阻塞触发 abort

- **WHEN** 一个 run 中同一 trigger（如 implementer-failed）在连续 3 个决策点重复出现
- **THEN** `npc auto-decide` 返回 `action=abort`、reason 标注 systemic-failure
- **AND** 主 session 标记剩余 change 为 skipped 并直接进 Step 4 finalize（worktree/分支保留）

#### Scenario: force-archive 二次失败收敛为终态

- **WHEN** 3d 决策为 force-archive，但 `npc archive run` 再次失败
- **THEN** 主循环以 `--trigger archive-failed` 二次调用 auto-decide，action 收敛为 skip（或 abort），change 状态落终态
- **AND** 随后 `npc state finalize` 不再因该 change 返回 incomplete

#### Scenario: 正常失败仍走原语义

- **WHEN** 失败未达系统性阻塞阈值
- **THEN** auto-decide 维持既有 continue-retry/skip/force-archive 语义，不误触 abort
