## Why

`npc spec fix run --change <id> --round N` 只按 `round-{N-1}.spec-review.json` 是否存在做门禁（`src/npc/spec_pipeline.py::spec_fix_run` L625-634），从不检查该 change 目录下是否已经存在更高轮次的 `round-{N}.spec-review.json` / `round-{N+1}.spec-review.json`。当人工在 review round K 之后手动修复、又直接把 review 跑到 round K+1（跳过了 fix round K+1），再回头调用 `spec fix run --round K+2` 时，函数会静默消费**过期**的 review 输入渲染 fix prompt——fixer 修的问题已经不是当前最新一轮 review 指出的问题，判据从"最新证据"退化成"任意一份存在的证据"。

`src/npc/coder.py::_render_prompt_file` 的 fix 分支（L272-302）与上述逻辑逐行同构（`round-{round_n - 1}.review.json`），同样不校验磁盘上是否存在更高轮次文件，存在相同的 stale-input 缺陷。更严重的是，它在 `round-{round_n-1}.review.json` **不存在**时（L284 `if review_path.is_file()` 为假）不报错，而是让 `findings_md` 保持空字符串、照常渲染 fix prompt（L277, L293-302）——fixer 会拿到一份空的 blocking findings 清单，误以为"无事可做"而静默跳过修复，这是比 stale 更危险的相邻缺陷。

## What Changes

- `spec_fix_run` 在确认 `round-{N-1}.spec-review.json` 存在之后、渲染 fix prompt 之前，新增新鲜度校验：扫描该 change 目录下所有 `round-*.spec-review.json`，解析各自轮次号取最大值；若最大值大于 `N-1`，视为 stale-review-input，返回结构化拒绝（`ok=false`、错误标识 `stale_review_input`），不渲染任何 prompt、不写 marker。
- `coder.py` 的 fix 渲染路径（覆盖 `npc fix run --seq <n> --round N` 的 in-session 与子进程两种分发模式）新增与 spec 侧同构的 stale-review-input 校验（同一错误标识 `stale_review_input`），扫描 `round-*.review.json`。
- `coder.py` 的 fix 渲染路径同时把"review 文件缺失"分支从静默渲染空 findings 改为结构化拒绝（新增错误标识 `prev_review_missing`），对齐 spec 侧既有 `prev_spec_review_missing` 的行为语义。
- 上述两类拒绝发生时，code 侧 MUST 把对应 seq 的 progress 状态收尾为 `needs-user-decision`（复用既有 `coder-setup-error` 收尾语义），不留下悬挂的已进入未退出的 phase。

## Non-Goals

- 不引入"review 序列与 fix 序列交叉节拍对齐"校验——只看 review 侧文件序列内部的单调性（最大轮次号），不扫描 `round-*.spec-fix.prompt.md` / fix record 等 fix 侧产物。
- 不改变 `spec_review_run` / `run_review_round` 的写入命名或行为，不改变 `[spec_review] max_rounds` 循环终止语义，不复用/引入 code review 的 `rounds_since_strict_decrease` stale 判定。
- 不改变 `npc spec fix record` / `npc fix record` 的 RESULT 装订契约与必需键集合。
- 不新增 telemetry 事件类型；新校验为确定性、无副作用的纯文件系统扫描。

## Capabilities

### New Capabilities

- `run-stale-review-guard`: `npc spec fix run` 与 `npc fix run` 在消费上一轮 review 产物渲染 fix prompt 之前，MUST 校验该产物是否为该 change 目录下轮次最新的 review 文件，且 code 侧的 review 文件缺失分支 MUST 结构化拒绝而非静默渲染空 findings。

### Modified Capabilities

- `spec-writer`: 基线 `openspec/specs/spec-writer/spec.md`「生成侧不得预知本轮评判标准」一条下的负向断言场景「fix 轮 prompt 不含当轮 review 内容」，其 fixture（`round-0.spec-review.json` 与 `round-1.spec-review.json` 同时存在时执行 `spec fix run --round 1`）与本 change 新增的 `run-stale-review-guard` 直接冲突：本 change 要求该输入返回 `stale_review_input` 并拒绝渲染任何 prompt，基线场景却要求渲染并只注入 round-0 内容。改写该场景为断言 stale 拒绝，因为"拒绝渲染"是比"渲染但过滤当轮内容"更强的不变量 1（生成侧不得预知本轮评判标准）保护——连 prompt 文件都不产出，自然不存在泄漏当轮 review 内容的可能。详见 design.md「Decisions」第 6 条。

## Impact

- `src/npc/spec_pipeline.py`（`spec_fix_run`：新增 stale-review-input 扫描与拒绝分支）
- `src/npc/coder.py`（fix 渲染路径：新增 stale-review-input 校验 + missing-review 结构化拒绝，覆盖 in-session 与子进程两个调用点）
- `tests/`（spec 侧与 code 侧的 stale/missing 校验单测，含 in-session/子进程两种分发模式）
- `openspec/specs/spec-writer/spec.md`（归档后：改写「fix 轮 prompt 不含当轮 review 内容」负向断言场景，见 Modified Capabilities）
