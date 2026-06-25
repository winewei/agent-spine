## ADDED Requirements

### Requirement: coder 子进程环境剔除 Anthropic 计费凭据

coder 子进程（任何后端）启动时使用的环境，MUST 以"当前进程环境剔除 `ANTHROPIC_API_KEY` 与 `ANTHROPIC_AUTH_TOKEN`"为 baseline，确保 headless `claude -p` 永不因继承到的 Anthropic API 凭据而被静默路由到付费 API。

#### Scenario: claude 后端在环境含 API key 时仍不付费

- **WHEN** claude 后端启动 coder 子进程，且 npc 进程环境里设置了 `ANTHROPIC_API_KEY`（或 `ANTHROPIC_AUTH_TOKEN`）
- **THEN** 传给子进程的环境**不含** `ANTHROPIC_API_KEY` 与 `ANTHROPIC_AUTH_TOKEN`
- **AND** 其余环境变量（PATH、HOME 等）原样保留

#### Scenario: 无 Anthropic key 时行为与现状一致

- **WHEN** claude 后端启动 coder 子进程，且环境里未设置任何 Anthropic 凭据
- **THEN** 子进程环境等价于继承当前环境（回退到订阅 OAuth），行为与剔除前一致

#### Scenario: mimo 后端在 scrubbed baseline 上叠加自身凭据

- **WHEN** mimo 后端启动 coder 子进程
- **THEN** 子进程环境 = scrubbed baseline（已剔除继承的 Anthropic key）+ `mimo.env` 解析出的键值
- **AND** `mimo.env` 内声明的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`（指向 MiMo 第三方端点的凭据）正常生效，不被剔除逻辑误删
