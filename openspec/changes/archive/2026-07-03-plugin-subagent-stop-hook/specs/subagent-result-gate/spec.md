## ADDED Requirements

### Requirement: spine-coder 出口必须通过 RESULT 与 commit 真实性硬闸

plugin MUST 注册 SubagentStop hook：spine-coder 结束时其最后消息 MUST 含合法 `RESULT:` 行（三套 schema 之一），且 `commit=<sha>` 非 `-` 时该 sha MUST 真实存在于 cwd 的 git 对象库（`git cat-file` 验证）；不合规 MUST exit 2 阻断回灌。hook stdout MUST 保持纯 JSON/空，诊断只走 stderr。非 spine-coder subagent MUST 放行。

#### Scenario: 合法 RESULT 与真实 commit 放行

- **WHEN** spine-coder 最后消息以合法 implement RESULT 行结束且 `commit=<sha>` 在 git 中存在
- **THEN** hook exit 0，RESULT 正常回灌主 session

#### Scenario: 谎报 commit 被阻断

- **WHEN** spine-coder RESULT 行报 `commit=abc1234` 但该 sha 在 cwd git 中不存在
- **THEN** hook exit 2 硬阻断，stderr 说明「commit 不存在」，主 session 不把该 RESULT 当真相装订

#### Scenario: 缺失 RESULT 行被阻断

- **WHEN** spine-coder 最后消息不含 `RESULT:` 行或必需 key 缺失
- **THEN** hook exit 2 阻断并说明缺失项

#### Scenario: 其他 subagent 与异常环境不误伤

- **WHEN** 结束的 subagent 非 spine-coder，或 hook 脚本自身遇异常（非 git 目录）
- **THEN** hook exit 0 放行，不阻断正常流程
