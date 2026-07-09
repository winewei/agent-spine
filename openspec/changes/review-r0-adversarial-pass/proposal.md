## Why

`docs/optimization-proposals/2026-07-09-bun-migration-lessons.md` 提案 1 指出：我们每轮 review 只有 1 个 reviewer，且 round-0 的 focus 模板是**验收导向**（先读 proposal/tasks/specs/design，再查合规）。Bun 团队的实测经验是，合规评审与找 bug 是两种不同任务——找 bug 的 reviewer 需要**只拿 diff + "假设它一定有错"的对抗框架**，他们合并前抓出的 3 个真 bug（libuv double-free、负时间戳 trunc/floor、`unwrap_or` 急切求值）全部来自这种上下文极简的证伪式评审，不是合规检查抓到的。

对照本仓库 `/spine-analyze`（2026-07-08）的数据：r0 一次过率仅 ~8%，review+fix 占全部 token 的 ~80%。若对抗通道能在 r0 把深层 bug 一次性暴露，而不是靠后续轮次逐轮挤牙膏，长尾理应左移（r2+ 轮次数下降）。

## What Changes

- `npc review run --round 0` 在既有单一 compliance pass（不变）之外，新增第二个 **diff-only 对抗式 pass**：新 focus 模板不注入 project context、不指示读 proposal/tasks/specs/design，只指示 `git --no-pager diff HEAD~1..HEAD` 与「假设这段 diff 必有 bug，唯一任务是找出它；重点关注资源释放/double-free、边界与符号处理、急切求值/短路语义、并发与生命周期」。
- 两个 pass 共用同一份 `REVIEW_SCHEMA`（不改 schema 结构），各自产出独立的原始 JSON；一个纯函数将两份 findings **合并去重**（按 `(file, line_range, category)` 精确匹配去重，pass1 优先）、**重新分配 id**（`F1..Fn`，pass1 在前）、**重新计算 verdict**，写入既有的 `round-0.review.json` 路径——纯读该 JSON 结构的下游（`review.parse_review`、`fixer.render_findings`）零改动即可消费；telemetry 发射端是例外，本 change 显式为其新增两个字段（见下）。
- round-0 对抗 pass 失败（引擎报错或非法 JSON，重试耗尽）不拖垮整轮：round-0 结果降级为 pass1-only——但输出路径与双通道成功时一致：仍经合并归一化（以空 pass2 参与合并，重编 finding id、重算 verdict）后写入 `round-0.review.json`，并在 telemetry 中标记 `adversarial_pass_ran=false`。
- `round >= 1` 保持现有单通道模板与流程，完全不改。
- 新增配置开关 `[review].adversarial_round0`（默认 `true`），可显式关闭（成本考量）。
- `review.round` telemetry 事件新增 `adversarial_pass_ran` / `adversarial_blocking_count` 两个字段，供后续验证「r0 blocking 数上升、r2+ 轮次数下降」的假设。

## Capabilities

### New Capabilities

- `review-adversarial-pass`：round-0 双通道评审执行、findings 合并去重、失败降级、telemetry 透出的完整契约。

### Modified Capabilities

（无既有 capability spec 覆盖 round-0 review 流程，本次为新增 capability。）

## Impact

- `src/npc/focus.py`（新增对抗式 round-0 模板渲染函数）
- `src/npc/review.py`（新增合并去重纯函数）
- `src/npc/pipeline.py`（`run_review_round` round=0 分支执行双 pass 并合并；`_render_focus` 相关调用点）
- `src/npc/config.py`（`ReviewEngineConfig` 新增 `adversarial_round0` 字段）
- `src/npc/telemetry.py`（`EMIT_FIELD_CONTRACT["review.round"]` 与 `emit_review_round` 新增两个字段）
- `docs/cli.md`（`npc review run` 一节同步双 pass 行为、新增产物文件、新增配置项、新增 telemetry 字段）
- `tests/`（focus 渲染负向测试、合并去重纯函数测试、pipeline round-0 双 pass 集成测试、telemetry 契约测试、config 开关测试）
