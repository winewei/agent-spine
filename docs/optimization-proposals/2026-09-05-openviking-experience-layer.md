# v1.8 迭代设计：OpenViking 经验层——轨迹回流为 coder 先验

- 日期：2026-09-05
- 状态：**待实施**（本文为实施蓝本；Phase 0 spike 是进入 Phase 1 的硬闸门）
- 关联：[principles.md](../principles.md)（四条不变量）、[design.md](../design.md) §11.6（telemetry 派生指标）/ §11.10（上下文预算契约）/ §11.12（打地鼠治理）、[2026-07-05-orchestration-context-budget.md](2026-07-05-orchestration-context-budget.md)（O(1) 主 session 不变量）
- 外部依赖：[volcengine/OpenViking](https://github.com/volcengine/OpenViking)（AGPLv3；本方案只经 HTTP API 调用，不引入其任何 Python 包）
- 基线：`main` @ `40ffa6c`（v1.7.1）

---

## 0. 问题陈述

npc 每个 run 在 `~/task_log/<PROJ_KEY>/<run>/<seq>-<change>/` 落盘完整轨迹，其中 `implement.summary.md`（Key Decisions / Issues Encountered / Files Modified）、`round-N.review.json`（独立 reviewer 的 findings 与 verdict）、`round-N.fix.summary.md`（Per-Finding Resolution / Locations Scanned / Invariant Sweep）是高信号层——实测约占轨迹总字节的 2%（其余 97% 为 codex 事件流），每 run 约 85k tokens 的蒸馏物。**这一层目前只写不读**：没有任何机制把它回流到下一个 change 的 implement / fix prompt。

后果可直接观测：同一 run 内多个 change 各自重新发现同一条环境事实（测试基线污染的判零回归方法、驱动层的类型解码陷阱、本地拓扑的端口共享约束）；review round-0 的 findings 类别高度复发（validation / test-coverage / error-handling / edge-case 合计约六成）。fix + review 占 agent 分钟约四成，是唯一值得压缩的成本池。

`/spine-analyze` 只面向 harness 自身的优化提案，不面向项目执行经验；它读 telemetry 派生指标，不读轨迹正文。

## 1. 方案选型结论

评估了三条路：自建薄经验层（确定性抽取 + 文件 + 结构化键匹配）、接入 OpenViking、接入其它记忆框架（mem0 / Letta）。结论：

- **接入 OpenViking，但只以"旁路软失败增强"形态**。它提供了自建最难做对的部分——LLM 蒸馏、相似候选检索、双层去重合并、`memory_diff.json` 审计、按 token 预算装配的召回；其经验产物是纯 markdown（`## Situation / ## Approach / ## Reflect` 三段 + 末尾 JSON 元数据注释），落在本机目录、可 grep，退出成本低。
- **地位低于 codex 一档**：codex 是不变量 1 的承载者（缺失 exit 4）；OpenViking 缺失只降低 prompt 质量、不影响正确性，doctor 该项 `required=False`，任何 phase 永不因它 exit 4。
- **自建薄层不是替代品，是对照组与种子**：从既有 summary 手工蒸 10–20 条种子经验，同时作为 A/B 基线。

不采纳的部分：OpenViking 的 server 多租户 / ACL / 加密 / MCP 工具面 / Claude Code hooks（npc 宿主中立，hook 是单宿主机制）/ VikingBot / context-compilation。

## 2. 已核实的外部事实（源码级，决定实现路径）

| 事实 | 对实现的约束 |
|---|---|
| 主仓与 Python SDK 为 AGPLv3；`crates/ov_cli` 为 Apache-2.0；npc 为 MIT 且 `dependencies = []` | **禁止 `import openviking*`**；只用 stdlib `urllib.request` 走 HTTP；CI 断言 `dependencies == []` 且 `src/` 无 `openviking` import |
| 只有 HTTP `POST /api/v1/sessions` 的 `CreateSessionRequest` 能传 `memory_policy`；`ov` CLI 与 MCP `remember` 都不能 | 提交通道**只能是 HTTP**，`ov` CLI 仅作人工排查 |
| 默认抽取链路 case → trajectory → experience 有硬依赖；case 由 LLM 判定，面向聊天语料 | 非聊天输入不可赌 LLM 判定 |
| 内建 fast path：首消息以 `# OpenViking Batch Training CaseSpec v1` 开头并附协议 `openviking.batch_train.case_spec.v1` 的 Case JSON（`name` / `task_signature` / `input` / `rubric.criteria`），配 `memory_policy={"memory_types":["experiences"]}`（服务端自动扩展为 cases + trajectories + experiences）即跳过 LLM case 判定 | npc 手写 Case，"限定只抽 experiences"与"命中 fast path"是同一配置；payload 格式错误直接报错不静默回退 |
| `agent_evolution.enabled` 默认 false，可热加载 | 服务端必须显式开启，doctor 校验 |
| `POST /api/v1/search` `mode="context"`：`max_tokens`（默认 1600）/ `quotas`（`experiences` 为一等 bucket）/ `exclude_uris` / `score_threshold`；返回 `rendered` 与带 `uri / score / text` 的 entries；`context_type` 无法精确锁定 experiences | 召回用 quota 倾斜 + 客户端按 uri 前缀 `viking://user/<uid>/memories/experiences/` 过滤；外壳由 npc 自套，不用服务端 `rendered` |
| commit 两阶段：Phase 1 同步归档返回 `task_id`，Phase 2 异步抽取；`memory_diff.json` 落 `{archive_uri}/memory_diff.json` | 写入 fire-and-forget，不等待 Phase 2 |
| 一次带 agent_evolution 的 commit ≈ 6 到十几次 LLM 调用；`search` 若开 `rewrite` 再加一次 | 抽取模型与 review 用的 codex **分离凭据或预算**；召回 `rewrite=false` |
| server 默认 `127.0.0.1:1933`；未配 `root_api_key` 即 dev 模式（本机任意进程 ROOT）；CORS 默认 `["*"]` | 部署要求配 `root_api_key`、绑本地；doctor 检出 dev 模式或非本地地址即 warn |
| embedding 默认 provider 为远端 volcengine；`local`（bge-small-zh，512 维，约 24MB，需 `openviking[local-embed]`）为 opt-in；VLM 支持 `openai-codex` | 数据流向是显式决策：推荐 `local` embedding + 独立于 review 的 VLM 凭据 |
| HTTP `POST /api/v1/resources` 拒绝本地路径，只收远程 URL 或 `temp_upload`；重复添加不显式指定 `to` 会生成 `_1 / _2` 副本 | 资源侧（Phase 2）走 git URL 或逐文件 temp_upload，**始终显式传 `to`** |

## 3. 架构

### 3.1 位置与不变量对齐

```
主 session ──O(1) 标量──► npc（experience_injected:int / experience.ok:bool）
                              │
   coder sub-agent ◄── prompt(disk) ◄── templates.render_* + experience_block
                              │                         ▲
   review（codex/claude）◄── focus（永不含经验） │ POST /api/v1/search mode=context
                              │                         │
   archive ∧ blocking==0 ──► experience.commit ──► POST /api/v1/sessions … /commit
                                                        │ Phase 2 异步
                                          viking://~/memories/experiences/*.md
```

| 不变量 | 对齐 |
|---|---|
| 1 生成⊥验证 | 经验**只注入 coder**（implement / fix），**绝不进 review focus**（`focus.py` 两个模板不改）；写入侧只提交独立 review 通过（blocking==0）且非 force-archive 的轨迹。若同一经验库同时喂 coder 与 reviewer，独立评估器退化为共享先验的相关评估器，blocking 下降成为测量假象 |
| 2 结构化契约为唯一真相 | 主 session 只看 `experience_injected` 与 `experience.ok`，永不读经验正文；每次注入的 uri / score / 当时 HEAD / sha 落盘 `<base>/<phase>.experience.json`，prompt 可完整重放；telemetry 记 pointer |
| 3 硬轨 ∝ 1/(人在回路) | 不新增闸门、停机点、`pending_decision`；默认关闭；经验失效不自动删，`npc experience status --stale` 只产候选清单交人 |
| 4 成本分层 | 抽取属"分析"，OpenViking 侧抽取模型不得低于 coder 档位，由 `[experience].extraction_model_declared` 人工声明、doctor 比对 warn；廉价 coder 读经验是执行，不违反 |

### 3.2 调用通道与降级

- HTTP `/api/v1`，stdlib `urllib.request`；超时 recall 3s / commit 5s（可配）；**单次不重试**（与 review 的重试策略刻意相反：增强项重试只拖慢主流程）。
- 读取侧失败：注入块为空串，prompt 照常渲染，`npc agent prompt render` 的 JSON 加 `experience_injected: 0` + `experience_error: <code>`，退出码不变。
- 写入侧失败：`npc archive run` 的 JSON 加 `experience: {"ok": false, "reason": ...}`，archive 本身仍判成功。
- 只有显式命令（`npc experience doctor` / `commit --strict` / `recall --strict`）对 server 不可用返回非 0。

### 3.3 写入侧

**唯一触发点**：`pipeline.run_archive` 成功路径，`_do_phase_exit(status="done")` 之后、`_telemetry.emit_archive_done` 旁，新增 `_experience_commit_hook(p, seq, entry)`。

**闸门（全部满足才提交）**：`progress[seq-1].status == "archived"` ∧ 末轮 review `blocking == 0`（`blocking_trend[-1]`）∧ 非 `force-archive` / 人工 override ∧ `experience_contaminated != true`（见 §3.6）。

**Session 组装**（`experience.build_messages(base, entry)`，复用 `focus._extract_section` 与 `focus.extract_fixed_history`）：

| # | role | 内容 | 目的 |
|---|---|---|---|
| 0 | user | CaseSpec v1 header + Case JSON：`name=<change_id>`，`task_signature=<proj_key>:<change_id>`，`input={proposal 的 Why/What 摘要, 语言栈, tasks 数}`，`rubric.criteria` 由各轮 review findings 的 `category + title` 映射（每条 `description` 非空） | 命中 fast path，跳过 LLM case 判定 |
| 1 | assistant | `implement.summary.md` 的 Key Decisions / Issues Encountered / Files Modified 三段 | 自述的决策与踩坑 |
| 2 | user | 各轮 `round-N.review.json` findings 的 `title / category / severity / file`（不含长 detail） | 独立验证者指出的错——信息密度最高 |
| 3 | assistant | 各轮 `fix.summary.md` 的 Per-Finding Resolution / Locations Scanned / Invariant Sweep | 修复手法与根因扫描 |
| 4 | user | archive 成功 + `total_rounds` + tests 结果 + 引导语"归纳为可复用规则，而非事件叙事" | outcome 信号 |

`session_id = npc-<proj_key>-<run_ts>-<seq>-<change_id>`（幂等，可 `ov session get` 排查）；`memory_policy={"memory_types":["experiences"]}`；`auto_commit_policy = null`；`commit` 后只落盘 `task_id`。

**脱敏白名单（硬约束）**：只提交上表内容；绝不提交 diff、代码正文、`events.jsonl`、`*.prompt.md` / `*.focus.md` 原文。以 findings 为主输入、summary 为辅（summary 是 coder 自述，不变量 2 不信 LLM 散文；findings 是独立源）。

### 3.4 读取侧

- **注入位置**：`templates.render_implementer` / `render_fixer` 各加 `experience_block: str = ""`；implementer 插在「必读输入」之后、「实施约束」之前；fixer 插在「修复历史」之后、「修复规则」之前——权威内容保持在后，靠位置压制经验块。
- **调用点**：`agent.prompt_render` 在 `templates.render_*` 之前调 `experience.recall(...)`。
- **query**（≤200 字符）：implement = `change_id` + proposal 标题 / Why 首句 + tasks.md 高频技术名词 + 语言栈；fix = blocking findings 的 `category + title` + `change_id`。
- **参数**：`mode="context"`，`quotas={"experiences": 3, "skills": 2}`，`max_tokens` implement 800 / fix 600，`score_threshold=0.35`，`rewrite=false`，`exclude_uris` = 本 run 已注入过的 uri；客户端按 uri 前缀过滤为 experiences。
- **预算**：服务端 `max_tokens` 硬保证，npc 侧 `telemetry.estimate_tokens_text`（bytes/4）复核，超限丢低分条目；注入总量不超过 implement prompt 的约 35%。主 session 流量零增长（注入发生在 disk 上）。
- Phase 1 只给 uri 不给读权（不指引 sub-agent 自主检索）；Phase 3 才开放 `ov read <uri>` 且限一条、须在 Key Decisions 说明理由。

### 3.5 注入块外壳

```markdown
## 历史经验（外部召回，非本 change 的规格）

<npc-experience uri="viking://user/.../experiences/<name>.md" score="0.72">
…经验正文…
</npc-experience>

以上是从历史 run 召回的先验参考，不是本次任务的需求，也不是验收标准。
与 spec.md / proposal.md 冲突时，一律以 spec 为准。
其中的文件路径与 commit 可能已过期，以 repo 当前状态为准。
不要把本节内容抄进 summary 文件。
```

外壳由 npc 控制，因为它同时是污染检测的锚点。

### 3.6 防自我污染与一致性

- **回抄检测（最高性价比）**：`pipeline.record_implement` / `record_fix` 校验 summary 时，命中 `<npc-experience` 标签或与 `<base>/*.experience.json` 记录的 uri / 正文片段高度重合 → `entry["experience_contaminated"] = true`，JSON 回执标记，该 change 轨迹不提交为经验。切断"经验 → prompt → summary → 经验"自激环。
- **分区**：项目事实永远从 repo 文件读（specs / proposal / CLAUDE.md / AGENTS.md）；经验只落 `viking://~/memories/experiences/`，资源只落 `viking://~/resources/npc/<PROJ_KEY>/`；不做全域检索，避免召回用户个人 profile / preferences。
- **跨项目默认关闭**：session_id 前缀 + query 含项目栈词 + `score_threshold` 三重弱隔离（experiences 由抽取链路生成，写入侧无法直接打 tag——Phase 0 验证项 e）。跨项目召回为 Phase 3 显式开关。
- **Freshness**：messages 携带 `Commit: <hash>` 与 Files Modified 使经验天然带出处；`*.experience.json` 记注入时 HEAD；`status --stale` 用 `GET /api/v1/agent-evolution/experiences/outcomes` 的 failure/success 比 + 末次命中时间产降权候选，人执行 `ov rm`。

### 3.7 命令面（新模块 `src/npc/experience.py`，命令风格与 `npc review` / `npc telemetry` 同构）

| 命令 | 职责 | 退出码 |
|---|---|---|
| `npc experience commit --seq N [--dry-run] [--strict]` | 手动 / 补偿提交一个 change 的轨迹；`--dry-run` 只把组装后的 messages 落 `<base>/experience.messages.json`（Phase 0 主工具） | 0 / 1（strict 且 server 不可用）/ 2 / 3 |
| `npc experience recall --phase {implement\|fix} --seq N [--round M] [--max-tokens N] [--strict]` | 只检索，输出注入块到 `<base>/<phase>.experience.md` + JSON 回执（entries / tokens / uris） | 同上 |
| `npc experience status [--seq N] [--stale]` | 各 change 经验流状态（committed / task_id / task_status / memories_extracted / injected_count）；`--stale` 产降权候选清单 | 0 / 3 |
| `npc experience doctor` | 详细探活：`/health` 版本、auth_mode、`agent_evolution`、本项目 scope 下 experiences 计数、抽取模型声明比对 | 0 / 1 |
| `npc experience sync-resources`（Phase 2） | specs / 参考文档 add_resource（显式 `to`，`processing_mode="vectors_only"`） | 0 / 1 |

### 3.8 配置、doctor、telemetry

- **config**（`config.py`）：frozen dataclass `ExperienceConfig`，字段 `enabled`（默认 false）/ `base_url`（默认 `http://127.0.0.1:1933`）/ `api_key_env` / `env_file` / `timeout_recall_ms`（3000）/ `timeout_commit_ms`（5000）/ `inject_max_tokens`（implement 800 / fix 600）/ `score_threshold`（0.35）/ `quotas` / `session_prefix` / `write_gate`（默认 `verified`）/ `extraction_model_declared`。凭据走 `api_key_env` + `env_file` 指针（对齐 `ProviderConfig`），绝不入 git。
- **doctor**（`doctor.py`）：`_check_experience()`，`required=False`：config 可解析 → `GET /health` 连通 → api key 有效 → `agent_evolution.enabled` 与 `memory_types` 含 experiences（无 API 暴露则读人工声明并 warn）→ dev 模式（无 api key）warn → `base_url` 非本地地址 warn。
- **telemetry**（`telemetry.py`）：新增 kind `experience.commit` / `experience.recall`，字段 `duration_ms` / `entries` / `injected_tokens` / `session_uri` / `task_id` / `ok` / `error` + `pointer`；复用 `emit_event` 的异常吞掉。回答：注入了多少 token、召回命中率、是否反复注入同一条。`policy_snapshot_id`（experiences 目录内容 hash，`init-run` 时记）进 record——**没有它"经验是否有用"不可测**。
- **依赖执法**：`npc verify deps`（复用 `verify routing` 思路）断言 `pyproject.toml` 的 `dependencies == []` 且 `src/` 无 `openviking` / `httpx` import；写入 principles.md 作硬约束。

## 4. 分阶段计划与验收

| 阶段 | 内容 | 硬验收 | 估算 |
|---|---|---|---|
| **P0 spike** | 本地部署 OpenViking：`root_api_key` 已配、绑 127.0.0.1、`agent_evolution.enabled=true`、embedding `local`、VLM 凭据独立于 review；取 2–3 个真实 archived change，`npc experience commit --dry-run` 组装 → 手工 HTTP 三步 → 轮询 `task_id` → 读 `memory_diff.json` 与 `ov ls viking://~/memories/experiences/` | (a) 产出 ≥1 条 experiences；(b) 正文是可复用规则而非事件叙事（人工判读）；(c) fix 场景 query 经 `mode=context, quotas={experiences:3}` 召回且 `score ≥ 0.35`；(d) `max_tokens=600` 时返回受控；(e) experiences 能否携带项目区分信息。**任一不达即不进 P1，退回自建薄层** | 0.5–1 天，0 行生产代码 |
| **P1 最小可用** | `experience.py`（urllib client + build_messages + recall + 外壳渲染，~350 行）；`ExperienceConfig`（~40）；`templates.py` 参数（~20）；`agent.prompt_render` 调 recall（~40）；`pipeline.run_archive` 调 commit（~30）；`record_*` 污染检测（~30）；`doctor`（~30）；telemetry 两个 emit + `policy_snapshot_id`（~50）；`cli.py` 注册（~60）；`verify deps`（~40）；测试（~300） | (1) `enabled=false` 时 prompt 文件与 v1.7.1 **逐字节一致**；(2) `enabled=true` 且 server 关停时全流程通过、退出码不变；(3) 真实 run 中第 2 个 change 的 implement prompt 出现 ≥1 条来自第 1 个 change 的经验；(4) A/B：同 run 相邻 change 一半开一半关，对比 `total_rounds` 中位数与同类别 findings 复发率 | 2–3 天，~1000 行含测试 |
| **P2 资源检索** | `sync-resources` + coder prompt 可选 `find` 指引，严格限参考型文档（已归档 change 的 specs、跨仓约定、长篇设计文档）。**本 change 的 `openspec/changes/<id>/specs/` 永远整读**——它是唯一验收标准，检索代替整读会漏 Requirement，此边界不可放宽 | archive 后增量同步 < 30s；`find` 对"历史某 capability 如何定义"能命中；本 change specs 仍整读（回归断言） | 1–2 天，200–300 行 |
| **P3** | 触发条件驱动，不预先承诺：sub-agent 经 HTTP 自主 `read`（限一条）；outcome 反馈给注入排序；跨项目召回开关；`/spine-analyze` 消费召回命中率与 `whack_a_mole_rounds` 的相关性 | **触发条件**：P1 telemetry 显示同类别 findings 复发率下降 ≥30% 且注入 token < 单 change 总量 8% | 未估 |

## 5. 前置条件（P1 进入生产路径前必须全部满足）

1. 集成面仅 HTTP；CI 有 import 禁令与 `dependencies == []` 断言。
2. 注入块全文落盘、sha 与 uri 列表进 run.json 与 telemetry，prompt 可重放。
3. server 配 `root_api_key` + 绑 127.0.0.1；doctor 检出 dev 模式或 `0.0.0.0` 即 warn。
4. 提交给 OpenViking 的 summary 先剥离本轮注入块；命中回抄的 change 不提交。
5. VLM / embedding 端点显式配置并在 doctor 报告；抽取凭据与 review 的 codex 配额隔离。
6. 默认关闭、缺失静默降级、任何 phase 不因它 exit 4。
7. 钉死 OpenViking 版本；其升级走 npc 的 breaking-change 政策，每次升级重测集成面（该项目 v0.3.x 处于高频重构期，experiences 子系统正被改为 policy-training 框架）。
8. principles.md 增补：经验注入只进 coder，review 引擎永不消费经验。
9. `npc experience status / --stale` 让人在不学 viking:// 的前提下审计删改。
10. 先落地手工蒸馏的种子经验集与自建薄层作为 A/B 对照基线。

## 6. 试点范围与止损

**范围**：单机、单仓库、先只做 **fix 阶段注入**（findings 已结构化，最容易验证命中）、≤600 tokens、上限 3 条；`memory_policy` 只允许 experiences（自动扩展 cases / trajectories），不写 profile / preferences / entities。

**周期**：4 周或 30 个 change，先到为准；奇数 seq 注入、偶数不注入。

**止损（触发任一即回滚并删库）**：同类别 findings 复发率下降 < 20%；出现 ≥1 次可归因于注入经验的错误修复或错误 archive；经验层月度 provider 成本 > review 成本的 30%，或造成任一次 review 因配额失败；OpenViking 升级导致集成面破坏 ≥2 次；人工审计发现 experiences 中出现密钥或非本项目路径。

若 P1 跑 3–5 个 run 后复发率无可见位移，**停在 P2（知识库检索）**——P2 的价值不依赖学习闭环成立。

## 7. 明确不做的

- 不上传 codex 事件流（97% 字节、低信号、污染检索）。
- 不让 OpenViking 参与任何 gate（archive 仍只认 `blocking == 0`）。
- 不把经验注入 review focus（不变量 1）；不让 `/spine-analyze` 以经验库替代 telemetry 作证据源（不变量 2）。
- 不做无人审的自动 PolicyUpdater 回写（一条错误经验的爆炸半径是"此后所有 change"，而人审成本约每周 2k tokens）。
- 不用 Claude Code hooks / MCP 接入（宿主中立）；不 import 其 SDK。
- 不用"训练 / epoch / accuracy"度量：工程 case 不可重复、非 i.i.d.；唯一诚实的度量是**某类 finding 在写入对应经验后的复发率变化**。

## 8. 实施顺序

1. 环境：部署 OpenViking（P0 前置），`npc experience doctor` 先于一切生产代码写出（它是 P0 的探针）。
2. P0 spike：`commit --dry-run` 组装器 + 手工 HTTP 三步，验收 (a)–(e)。
3. P1：按 §3.3 → §3.4 → §3.6 → §3.8 顺序落地；先 fix 阶段注入，再 implement 阶段。
4. 种子经验集与 A/B 基线同步准备。
5. 跑 3–5 个 run 后按 §6 判定；再决定 P2 / P3。

## 9. 部署实测备注（2026-09-05，OpenViking 0.4.17.1，实现时以此为准）

- 服务：launchd `ai.openviking.server`（`~/Library/LaunchAgents/ai.openviking.server.plist`，KeepAlive），配置 `~/.openviking/ov.conf`，数据 `~/.openviking/data`，日志 `~/.openviking/logs/`。`GET /health` 免鉴权返回 `{status, healthy, version, auth_mode}`；`GET /ready` 检查 agfs / vectordb / embedding。
- 鉴权：`auth_mode=api_key`。**root key 只能访问 `/api/v1/admin/*`，数据 API 返回 403**；数据面必须用用户 key。用户 key 由 `POST /api/v1/admin/accounts/{account}/users`（root）签发，响应 `result.user_key`。请求头 `X-API-Key: <key>`（用户 key 自带 account/user 身份，无需再传 `X-OpenViking-*` 头）。
- 凭据文件：`~/.openviking/npc-client.env`（`OPENVIKING_BASE_URL` / `OPENVIKING_ACCOUNT` / `OPENVIKING_USER` / `OPENVIKING_API_KEY`，用户 key，npc 读取）；`~/.openviking/root.env`（`OPENVIKING_ROOT_API_KEY`，仅 doctor 查 `GET /api/v1/admin/agent-evolution` 与签发用户 key 时用）。均 `chmod 600`，不入 git。
- 路由（均在 `/api/v1` 下）：`POST /sessions`（body `{session_id, memory_policy}`）→ `POST /sessions/{id}/messages`（`{role, content}`）→ `POST /sessions/{id}/commit`（`{}`，返回 `result.task_id / archive_uri`）→ `GET /tasks/{task_id}`（`result.status ∈ {..., completed}`，`result.result.memories_extracted / effective_memory_types / agent_evolution_enabled / token_usage`）。召回 `POST /search/search`（注意是 `/search/search`，不是 `/search`）；`POST /search/find` 为纯检索。
- fast path 实测：首消息为 `# OpenViking Batch Training CaseSpec v1` + ```json 围栏 Case，配 `memory_policy={"memory_types":["experiences"]}`，commit 后 `effective_memory_types = [cases, experiences, trajectories]`，一次 commit 约 13.6k tokens（Codex `gpt-5.4`，reasoning medium），约 60s 完成；产物 `viking://user/<uid>/memories/experiences/<name>.md`（Situation / Approach / Reflect 三段，末尾 `MEMORY_FIELDS` JSON 注释含 `derived_from` 链接），磁盘路径 `~/.openviking/data/viking/<account>/user/<uid>/memories/experiences/`。
- 召回实测：`{"query", "mode":"context", "quotas":{"experiences":3}, "max_tokens", "score_threshold":0.35, "rewrite":false, "query_expansion":"off", "detail":{"experiences":"full"}}`，返回 `result.entries[]`（`uri / score / category / detail(tier) / text`）与 `result.rendered`（`<memory uri= type= score= detail= />` 片段）与 `result.stats`。`max_tokens` 为硬预算：150 时降至 `uri` 档（text 为空），600 时给 `overview` 档；要拿完整正文需给到约 1000 或对单条 uri 走 `GET /content/read?uri=`。`detail` 不传时默认只给 uri 档。
- 项目隔离：experiences 以 OpenViking user 为隔离单位。P1 用单一用户 `npc`，项目信息进 Case 的 `task_signature=<proj_key>:<change_id>` 与 `input`；`[experience].user` 预留为可配置项，P3 若需硬隔离再按 `PROJ_KEY` 派生用户并用 root key 签发。
- 凭据与配额：VLM 走 Codex OAuth（读 `~/.codex/auth.json`），与 review 的 codex 共用同一账号额度——试点期接受，telemetry 记 `token_usage` 以便核算。
