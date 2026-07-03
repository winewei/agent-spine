# auto-permission-deny-rules Specification

## Purpose
TBD - created by archiving change auto-mode-deny-rules. Update Purpose after archive.
## Requirements
### Requirement: auto 档自备权限必须附带破坏性操作 deny 底线

`npc init --auto` 自备权限时 MUST 在 `.claude/settings.local.json` 的 `permissions.deny` 写入 `Bash(git push --force*)`、`Bash(git reset --hard*)`、`Edit(.git/**)`；写入 MUST 与用户既有 deny 取并集（不删不改既有条目）、幂等，且失败 MUST NOT 阻塞 init。

#### Scenario: auto 档 run 全程禁止 force push

- **WHEN** `npc init --auto` 完成自备权限后，run 中任一环节尝试 `git push --force`
- **THEN** Claude Code 权限系统按 deny 规则拒绝该 Bash 调用
- **AND** 由于 deny 属 settings 而非 context，context compaction 后依然生效

#### Scenario: 不动用户已有 deny

- **WHEN** 用户 settings.local.json 已有自定义 deny 条目，再运行 `npc init --auto`
- **THEN** 用户条目原样保留，三条 harness deny 以并集追加，重复运行不产生重复条目

#### Scenario: 坏 JSON 容错

- **WHEN** settings.local.json 是无法解析的 JSON
- **THEN** init 不覆盖该文件、不因此失败，仅告警

