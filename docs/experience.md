# 经验层（可选）：让 harness 记住上一个 change 学到的东西

> 适用版本：npc ≥ 1.8。默认关闭；不启用时 npc 的一切行为与 1.7 完全一致。

## 它解决什么问题

harness 跑一个多 change 的批次时，每个 change 由一个全新的 coder sub-agent 实施——它对前面的 change 一无所知。于是同一个仓库里反复出现同一类事：

- **重复摸索环境事实**：第 3、5、6 个 change 各自重新发现"全量测试有既有失败，要用改动前后失败集合 diff 判零回归"；各自重新踩同一个驱动 / 框架的类型陷阱。每次 5–10 分钟，随 change 数 × 陷阱数增长。
- **重复吃同一类 review finding**：某类不变量（"所有包装异常必须保留原始异常链"、"所有配置入口必须过同一校验"）在第 1 个 change 被 review 抓出并修好，第 4 个 change 的 coder 照样再犯一次，再修一轮。实测批次里 review round-0 的 findings 有六成集中在少数几个复发类别；fix + review 约占 agent 时间四成。

npc 每个 change 其实都把这些教训写在了 `~/task_log/…/implement.summary.md`（Key Decisions / Issues Encountered）和 `round-N.review.json`（独立 reviewer 的 findings）里——**只是从来没有人读它**。经验层做的事就一句话：**把已归档 change 的轨迹蒸馏成可复用规则，在下一个 change 的 coder prompt 里带上。**

## 它带来什么（以及不带来什么）

| 会发生 | 不会发生 |
|---|---|
| change 归档且 review 通过后，其轨迹自动提交到 OpenViking 抽取为 experiences（Situation / Approach / Reflect 三段规则） | **不影响任何闸门**：archive 仍只认 review `blocking == 0`；OpenViking 挂了、慢了、没配，流程照跑，只是 prompt 里少一段 |
| 下一个 change 的 implement / fix prompt 里多一段「历史经验（外部召回，非本 change 的规格）」，≤ 800 tokens，2–3 条与本任务最相关的规则 | **不进 review**：reviewer 永远看不到经验。经验只帮 coder 少犯错，验证仍然独立（否则 blocking 下降就成了测量假象） |
| 每条注入都落盘（`<base>/implement.experience.json` / `.md`），可回放、可审计；coder 若把经验原文抄进 summary，该 change 的轨迹不会被回收为经验（切断自激环） | **不自动改任何东西**：经验的增删由人审（`npc experience status`、`ov ls` / `ov rm`）；npc 不回写、不删除 |
| `npc experience doctor` / `status` 让你不学 OpenViking 也能看到经验层在做什么 | **不上传代码**：只提交 summary 的三个段、findings 的 title / category / severity / file、fix summary 的三个段；不提交 diff、代码正文、事件流、prompt 原文 |

预期收益的诚实说法：它压缩的是"重复踩坑"和"同类 finding 复发"这两块，工程语境下无法给出干净的百分比——唯一可信的度量是**某类 finding 在对应经验写入后的复发率是否下降**，以及 `total_rounds` 分布是否左移。建议按 §5 做 A/B 再决定是否长期开启。

## 安装 OpenViking（一次，机器级）

OpenViking 是字节跳动开源的"agent 上下文数据库"（AGPLv3），npc **只经 HTTP 调用**它，不引入其任何 Python 包。需要 Python ≥ 3.10。

```bash
# 1) 安装（含本地 embedding，首次启动自动下载约 24MB 模型）
uv tool install --python 3.12 "openviking[local-embed]"

# 2) 写配置 ~/.openviking/ov.conf（最小可用；root_api_key 自己生成一个随机串）
mkdir -p ~/.openviking/data && cat > ~/.openviking/ov.conf <<'EOF'
{
  "storage":   { "workspace": "/ABSOLUTE/PATH/.openviking/data",
                 "vectordb": { "name": "context", "backend": "local" },
                 "agfs": { "backend": "local" } },
  "embedding": { "dense": { "provider": "local", "model": "bge-small-zh-v1.5-f16", "dimension": 512 } },
  "vlm":       { "provider": "openai-codex", "model": "gpt-5.4",
                 "api_base": "https://chatgpt.com/backend-api/codex", "reasoning_effort": "medium" },
  "server":    { "host": "127.0.0.1", "port": 1933, "auth_mode": "api_key",
                 "root_api_key": "<random-secret>",
                 "cors_origins": ["http://127.0.0.1:1933"],
                 "agent_evolution": { "enabled": true } }
}
EOF
chmod 600 ~/.openviking/ov.conf
openviking-server doctor        # 全部 PASS 再往下
```

要点：
- **VLM**（做抽取的模型）：示例用 Codex OAuth（已登录 Codex CLI 即可，无需 key）；也可换 `openai` 兼容端点 / `kimi` / `glm`，见 OpenViking 配置文档。抽取属"分析"，模型档位不应低于 coder。
- **embedding** 用内置 `local`：向量不出机器。默认值是远端 provider，请显式写 `local`。
- **必须配 `root_api_key` 并绑 `127.0.0.1`**：不配 key 即 dev 模式，本机任意进程都是 ROOT。
- **`agent_evolution.enabled: true`** 是产出 experiences 的前提，默认关。

启动并常驻（macOS 用 launchd；Linux 用 systemd user unit 同理）：

```bash
nohup openviking-server > ~/.openviking/server.log 2>&1 &      # 先手工验证
curl -s http://127.0.0.1:1933/health                             # {"status":"ok",...,"auth_mode":"api_key"}
```

给 npc 签发一个**用户 key**（root key 只能访问管理面，数据 API 会 403）：

```bash
ROOT=<你的 root_api_key>
curl -s -H "X-API-Key: $ROOT" -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:1933/api/v1/admin/accounts -d '{"account_id":"npc","admin_user_id":"npc-admin"}'
curl -s -H "X-API-Key: $ROOT" -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:1933/api/v1/admin/accounts/npc/users -d '{"user_id":"npc","role":"user"}'
# 把响应里的 result.user_key 写入：
umask 077; cat > ~/.openviking/npc-client.env <<EOF
OPENVIKING_BASE_URL=http://127.0.0.1:1933
OPENVIKING_ACCOUNT=npc
OPENVIKING_USER=npc
OPENVIKING_API_KEY=<user_key>
EOF
printf 'OPENVIKING_ROOT_API_KEY=%s\n' "$ROOT" > ~/.openviking/root.env     # 仅 doctor 查开关用，可选
```

## 在工程里启用（项目级）

```toml
# <repo>/.npc/config.toml
[experience]
enabled = true
extraction_model_declared = "gpt-5.4"   # 与 ov.conf 的 vlm.model 一致，供 doctor 比对
```

```bash
npc experience doctor      # health / agent_evolution / experiences_count 全绿即可
npc doctor                 # 总体检里多一项 experience（required=false，永不阻塞）
```

之后**无需任何额外操作**：`npc archive run` 成功且 review 通过时自动提交轨迹；`npc agent prompt render` 自动召回注入。观察：

```bash
npc experience status              # 各 change：committed / task_status / injected_count / injected_tokens
npc experience recall --phase implement --seq 3     # 手动看某个 change 会被注入什么
ov ls viking://user/npc/memories/experiences/        # 经验库全貌（纯 markdown，可直接读 / 删）
```

## 成本与边界

- **每次提交**约 10–15k tokens 的抽取模型调用、约 1 分钟异步完成；召回本身不调用 LLM（`rewrite` / `query_expansion` 已关）。用 Codex OAuth 时与 review 共用同一账号额度。
- **主 session 零增量**：注入发生在磁盘上的 prompt 文件里，主 session 只看到 `experience_injected` 一个整数。
- **可复现**：每次注入的 uri / score / 当时 HEAD / 内容 sha 都在 `<base>/*.experience.json`；`policy_snapshot_id` 记录本 run 首次召回时的经验库指纹。
- **退出成本低**：experiences 是 `~/.openviking/data/.../memories/experiences/*.md` 纯文本；关闭 `enabled` 即回到 1.7 行为。

## 怎么衡量它有没有用（建议的试点）

1. 先只在 **fix 阶段**受益最明显的工程试点；`enabled = true` 跑 3–5 个 run。
2. 奇数 seq 的 change 开、偶数关（或相邻两个 run 一开一关），对比 `npc telemetry hotspots` 里的 `whack_a_mole_rounds`、review round-0 的 `blocking_count`、`total_rounds` 中位数。
3. 触发任一即关闭并清库：同类 findings 复发率下降 < 20%；出现可归因于注入经验的错误修复；抽取成本 > review 成本的 30%；experiences 中出现密钥或不该有的路径。

## 排错

| 现象 | 处理 |
|---|---|
| `npc experience doctor` 报 `health` 不通 | server 没起或端口不对：`curl 127.0.0.1:1933/health`；检查 `~/.openviking/npc-client.env` 的 `OPENVIKING_BASE_URL` |
| `agent_evolution: false` | `ov.conf` 的 `server.agent_evolution.enabled` 置 true（热加载，无需重启） |
| `status` 里 `task_status` 一直 `running` | 抽取模型慢或凭据失效：看 `~/.openviking/server.log`；`openviking-server doctor` 查 VLM |
| commit 返回 `reason: http_403` | 用了 root key；换用户 key |
| prompt 里没有经验段、回执 `experience_injected: 0` 且无 error | 经验库为空或相关度低于 `score_threshold`（默认 0.35）；先归档几个 change 积累，或 `npc experience recall --seq N` 看命中 |
| 回执 `experience_contaminated: true` | coder 把注入的经验抄进了 summary；该 change 不回收为经验，不影响归档 |
