## Why

平台机制加固（配合审计 D 节不变量 3 的分析 + docs/research/2026-07-02-claude-code-platform-capabilities.md：permission deny 规则优先级高于 hook、且不受 context compaction 影响）：`npc init --auto` 现在会自备权限（src/npc/settings_auth.py、src/npc/init_cmd.py，见 spine-run.md Guardrails「auto 档的工具权限由 npc init --auto 自我预备」）——把 `defaultMode=acceptEdits` + Bash 白名单 + additionalDirectories 写进 settings。auto 档放宽了「允许」，却没有配套的「禁止」底线：长 run 中 compaction 后 prompt 层约束会衰减，唯一在整个 run 生命周期恒定生效的是 settings 的 deny 规则。当前 `--auto` 不写任何 deny，`git push --force`、`git reset --hard`、直改 `.git/` 内部在权限层零阻拦。

## What Changes

- `npc init --auto` 自备权限时，在 `.claude/settings.local.json` 的 `permissions.deny` 同时写入：`Bash(git push --force*)`、`Bash(git reset --hard*)`、`Edit(.git/**)`。
- 合并语义：**不动用户已有 deny**（并集追加、去重、幂等）；坏 JSON 不覆盖（沿用现有 settings_auth 容错语义）；失败不阻塞 init。
- 文档说明（spine-run.md Guardrails / docs）：deny 规则由 Claude Code 权限系统在每次工具调用时强制，不进 context，因此 **compaction 后仍恒定生效**——这是 prompt 层约束做不到的。
- 补测试：deny 写入、与用户既有 deny 的并集合并、幂等重跑、坏 JSON 不覆盖。

## Capabilities

### New Capabilities

- `auto-permission-deny-rules`: `--auto` 自备权限的破坏性操作 deny 底线契约（force push / hard reset / .git 直改）。

### Modified Capabilities

## Impact

- `src/npc/settings_auth.py`（deny 规则合并写入）
- `src/npc/init_cmd.py`（--auto 路径接入）
- `plugins/agent-spine/commands/spine-run.md` / `docs/`（compaction 生效原理说明）
- `tests/`（合并/幂等/容错用例）
