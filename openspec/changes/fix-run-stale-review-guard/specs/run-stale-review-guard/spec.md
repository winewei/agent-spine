## ADDED Requirements

### Requirement: spec fix 入参新鲜度校验（stale-review-input guard）

`npc spec fix run --change <id> --round N`（`N >= 1`）在确认 `round-{N-1}.spec-review.json` 存在之后、渲染任何 fix prompt 之前，MUST 扫描该 change 目录下所有 `round-*.spec-review.json` 文件，解析各自文件名中的轮次号，取其最大值 `max_round`。若 `max_round` 大于 `N-1`，MUST 判定为过期输入，返回 `ok == false`、稳定错误标识 `stale_review_input`，MUST NOT 渲染任何 `round-{N}.spec-fix.prompt.md`，MUST NOT 写入 `pre_head.fix-r{N}.txt` marker。

该校验 MUST 在既有的 `round-{N-1}.spec-review.json` 存在性检查（返回 `prev_spec_review_missing`）**之后**执行：`round-{N-1}.spec-review.json` 本身缺失时，MUST 仍返回 `prev_spec_review_missing`，MUST NOT 被本条新增校验抢先判定或覆盖。

#### Scenario: 无更高轮次时行为不变

- **GIVEN** 某 change 目录下只存在 `round-0.spec-review.json`
- **WHEN** 执行 `npc spec fix run --change <id> --round 1`
- **THEN** 返回 `.ok == true` 且 `.deferred == true`
- **AND** `round-1.spec-fix.prompt.md` 被写出

#### Scenario: 存在更高轮次时判定为 stale 并拒绝

- **GIVEN** 某 change 目录下同时存在 `round-0.spec-review.json` 与 `round-1.spec-review.json`
- **WHEN** 执行 `npc spec fix run --change <id> --round 1`
- **THEN** 返回 `.ok == false`，错误标识 `stale_review_input`
- **AND** `round-1.spec-fix.prompt.md` 未被写出
- **AND** `pre_head.fix-r1.txt` 未被写出

#### Scenario: 基线文件缺失时优先返回既有错误标识

- **GIVEN** 某 change 目录下不存在 `round-0.spec-review.json`，但存在 `round-1.spec-review.json`
- **WHEN** 执行 `npc spec fix run --change <id> --round 1`
- **THEN** 返回 `.ok == false`，错误标识为 `prev_spec_review_missing`
- **AND** MUST NOT 为 `stale_review_input`

### Requirement: code 侧 fix 入参新鲜度校验（与 spec 侧同构）

`npc fix run --seq <n> --round N`（`N >= 1`）在确认 `round-{N-1}.review.json` 存在之后、渲染任何 fix prompt 之前，MUST 扫描该 change 目录下所有 `round-*.review.json` 文件，解析各自文件名中的轮次号，取其最大值 `max_round`。若 `max_round` 大于 `N-1`，MUST 判定为过期输入，返回 `ok == false`、稳定错误标识 `stale_review_input`，MUST NOT 渲染任何 `round-{N}.fix.prompt.md`。

该校验 MUST 在下方「code 侧 missing-review 分支改为结构化拒绝」要求所定义的 `round-{N-1}.review.json` 存在性检查（返回 `prev_review_missing`）**之后**执行：`round-{N-1}.review.json` 本身缺失时，MUST 仍返回 `prev_review_missing`，MUST NOT 被本条 stale 校验抢先判定或覆盖——即使此时磁盘上已存在轮次号更高的 `round-*.review.json` 文件。两条要求的判定顺序固定为：先 missing-review 存在性检查，仅当该检查通过（基线文件存在）后，才执行本条 stale 扫描。

该校验 MUST 对 `--dispatch in-session` 与非 in-session（子进程）两种分发模式同等生效：MUST NOT 调用 coder 后端子进程，MUST NOT 产出 `deferred == true` 的 in-session 指令。

#### Scenario: 无更高轮次时行为不变（子进程分发）

- **GIVEN** 某 change 目录下只存在 `round-0.review.json`
- **WHEN** 以非 in-session 分发模式执行 `npc fix run --seq <n> --round 1`
- **THEN** 返回 `.ok == true`
- **AND** `round-1.fix.prompt.md` 被写出

#### Scenario: 存在更高轮次时判定为 stale 并拒绝（子进程分发）

- **GIVEN** 某 change 目录下同时存在 `round-0.review.json` 与 `round-1.review.json`
- **WHEN** 以非 in-session 分发模式执行 `npc fix run --seq <n> --round 1`
- **THEN** 返回 `.ok == false`，错误标识 `stale_review_input`
- **AND** `round-1.fix.prompt.md` 未被写出
- **AND** 未产生任何 coder 后端子进程调用

#### Scenario: 存在更高轮次时判定为 stale 并拒绝（in-session 分发）

- **GIVEN** 某 change 目录下同时存在 `round-0.review.json` 与 `round-1.review.json`
- **WHEN** 以 `--dispatch in-session` 执行 `npc fix run --seq <n> --round 1`
- **THEN** 返回 `.ok == false`，错误标识 `stale_review_input`
- **AND** 返回体 MUST NOT 含 `deferred == true`
- **AND** `round-1.fix.prompt.md` 未被写出

#### Scenario: 基线文件缺失但存在更高轮次时优先返回既有错误标识

- **GIVEN** 某 change 目录下不存在 `round-0.review.json`，但存在 `round-1.review.json`
- **WHEN** 执行 `npc fix run --seq <n> --round 1`
- **THEN** 返回 `.ok == false`，错误标识为 `prev_review_missing`
- **AND** MUST NOT 为 `stale_review_input`
- **AND** `round-1.fix.prompt.md` 未被写出

### Requirement: code 侧 missing-review 分支改为结构化拒绝

`npc fix run --seq <n> --round N`（`N >= 1`）当 `round-{N-1}.review.json` 不存在时，MUST 返回 `ok == false`、稳定错误标识 `prev_review_missing`，MUST NOT 渲染任何 `round-{N}.fix.prompt.md`，MUST NOT 以空 blocking findings 静默继续渲染。此行为对齐 spec 侧既有 `prev_spec_review_missing` 的语义，对 in-session 与子进程两种分发模式同等生效。

本条 missing-review 存在性检查 MUST 先于上方「code 侧 fix 入参新鲜度校验（与 spec 侧同构）」要求所定义的 stale 扫描执行：即使磁盘上已存在轮次号更高的 `round-*.review.json` 文件，只要 `round-{N-1}.review.json` 本身缺失，MUST 返回 `prev_review_missing` 而非 `stale_review_input`。两者冲突时以本条 missing-review 判定结果为准。

#### Scenario: review 文件缺失时拒绝而非静默渲染空 findings（子进程分发）

- **GIVEN** 某 change 目录下不存在 `round-0.review.json`
- **WHEN** 以非 in-session 分发模式执行 `npc fix run --seq <n> --round 1`
- **THEN** 返回 `.ok == false`，错误标识 `prev_review_missing`
- **AND** `round-1.fix.prompt.md` 未被写出
- **AND** 未产生任何 coder 后端子进程调用

#### Scenario: review 文件缺失时拒绝而非静默渲染空 findings（in-session 分发）

- **GIVEN** 某 change 目录下不存在 `round-0.review.json`
- **WHEN** 以 `--dispatch in-session` 执行 `npc fix run --seq <n> --round 1`
- **THEN** 返回 `.ok == false`，错误标识 `prev_review_missing`
- **AND** 返回体 MUST NOT 含 `deferred == true`

### Requirement: 拒绝时不留下悬挂的 phase 状态

`npc fix run` 因 `stale_review_input` 或 `prev_review_missing` 拒绝时，MUST 把该 seq 对应 progress 条目的状态收尾为 `needs-user-decision`（沿用既有 `coder-setup-error` 收尾语义），MUST NOT 使该 phase 停留在"已进入未退出"的悬挂态。此要求对 `--dispatch in-session` 与子进程两种分发模式同等生效。此要求仅适用于 code 侧——`npc spec fix run` 无 phase enter/exit 状态机概念，不适用本条。

#### Scenario: stale 拒绝后 progress 状态可被 resume 感知

- **GIVEN** 某 change 目录下同时存在 `round-0.review.json` 与 `round-1.review.json`
- **WHEN** 执行 `npc fix run --seq <n> --round 1` 并被 `stale_review_input` 拒绝
- **THEN** 读取 state 中该 seq 的 progress 条目，其 `status` 等于 `needs-user-decision`

#### Scenario: missing 拒绝后 progress 状态可被 resume 感知

- **GIVEN** 某 change 目录下不存在 `round-0.review.json`
- **WHEN** 执行 `npc fix run --seq <n> --round 1` 并被 `prev_review_missing` 拒绝
- **THEN** 读取 state 中该 seq 的 progress 条目，其 `status` 等于 `needs-user-decision`
