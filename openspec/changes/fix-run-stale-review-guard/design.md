## Context

`npc spec fix run` 与 `npc fix run` 都以"上一轮 review 文件"为输入渲染 fix prompt，但两者取上一轮文件的方式都是硬编码 `round_n - 1` 拼路径，从不检查磁盘上是否已经存在轮次更高的 review 文件。人工操作序列（review K → 手动修复 → review K+1，跳过 fix K+1 → 之后再跑 `spec fix run --round K+2`）会让函数静默消费一份不再是"最新证据"的 review 输入。code 侧还有一个更严重的相邻缺陷：review 文件缺失时不报错，静默渲染空 findings。

## Goals

- spec 侧与 code 侧的 fix 渲染路径在渲染 prompt 之前，都能确定性地检测到"待消费的 review 文件不是该轮次序列里最新的一份"，并结构化拒绝。
- code 侧的 missing-review 分支从"静默空 findings"改为结构化拒绝，行为对齐 spec 侧既有 `prev_spec_review_missing`。
- 两侧新增校验使用统一的错误标识风格（snake_case），复用既有错误 dict 形状。
- code 侧拒绝时不留下悬挂的 phase 状态（已 enter 未 exit），保持 `npc resume`/`npc status` 可感知。

## Non-Goals

- 不做 review 序列与 fix 序列的交叉节拍对齐校验（只看 review 侧文件序列内部单调性）。
- 不改变 review 产物的写入命名、`max_rounds` 循环终止语义、telemetry 事件形态。
- 不引入 code review 既有的 `rounds_since_strict_decrease` stale 判定或与之复用逻辑。

## Decisions

### 1. 扫描算法：glob + 正则解析轮次号，取最大值

spec 侧扫描 `base.glob("round-*.spec-review.json")`，code 侧扫描 `base.glob("round-*.review.json")`；用正则 `round-(\d+)\.spec-review\.json` / `round-(\d+)\.review\.json` 从文件名解析轮次号（非法/不匹配的文件名忽略，不参与取最大值）。若解析出的最大轮次号 `max_round` 大于本次 fix 即将消费的轮次号（`round_n - 1`），判定为 stale，返回结构化拒绝。此写法风格参照 `src/npc/plan.py` 中 `Path.glob` 枚举候选、取最新的既有模式（`(changes_root / "archive").glob(f"*-{cid}")`），仓库里目前没有可直接复用的"扫描 round-N 文件取 max"工具函数，本 change 在 `spec_pipeline.py` 与 `coder.py` 各自新增一个小的私有辅助函数（不跨模块共享，避免为了复用两行逻辑引入新的公共模块耦合）。

### 2. 错误标识：统一 snake_case `stale_review_input`

现有 `spec_fix_run` 的 3 个错误 slug（`prev_spec_review_missing` / `invalid_json` / `invalid_schema`）均为 snake_case。用户原始目标文案中的 kebab-case `stale-review-input` 仅为口语描述，不作为契约键名——已在 pattern-interrogation.md 的 `User Decisions` 中拍板：新增错误标识统一为 `stale_review_input`，spec 侧与 code 侧共用同一个字符串，便于上层（如 spine-run 3d 决策点、telemetry）不必分侧记忆两套标识。

code 侧新增的 missing-review 结构化拒绝标识为 `prev_review_missing`（不叫 `prev_spec_review_missing`，因为 code 侧消费的是 `round-*.review.json` 而非 `round-*.spec-review.json`，沿用既有"prev_" + 产物名的构词方式，保持与 spec 侧对称但不同名，避免两套不同产物共用一个标识造成排障时误判产物类型）。

### 3. 校验时序：missing 检查先于 stale 检查

两侧都先确认 `round-{N-1}` 文件本身存在（缺失 → `prev_spec_review_missing` / `prev_review_missing`），再做"是否存在更高轮次"的 stale 扫描。理由：如果连基线文件都不存在，"是否有更新的证据"这个问题没有意义——必须先建立"消费的到底是不是一份存在的证据"这个前提，再问"是不是最新的证据"。这与 spec 侧既有 `prev_spec_review_missing` 分支的提前 return 位置保持一致，不改变其判据与错误标识。

### 4. code 侧改动落点：`_render_prompt_file` 的 fix 分支 + 两个调用点

`src/npc/coder.py::_render_prompt_file` 的 `else`（fix）分支（现 L272-302）是 stale 扫描与 missing 结构化拒绝的落点：在渲染 `templates.render_fixer` 之前完成两项校验；校验失败时该函数 MUST 有一条能让调用方感知失败的返回路径（当前签名恒为 `tuple[Path, str]` 成功元组，需要扩展为可携带失败信息，例如抛出一个携带 `error`/`detail` 的专用异常，由两个调用点分别捕获转成结构化 `{"ok": False, ...}`）。

两个调用点：
- `_do_fix_in_session`（L455-483，`--dispatch in-session` 分支）：捕获后直接构造 `{"ok": False, "error": ..., ...}` 返回，不再走 `deferred=True` 的 in-session 指令路径。
- `_do_fix_body`（L605-644，子进程分支）：捕获后构造同形状的失败结果；`run_fix` 包裹 `_do_fix_body` 的既有 `except (FileNotFoundError, NotImplementedError, ValueError, MimoEnvError)` 分支已经把这类"渲染前失败"统一收尾为 `_pipeline._do_phase_exit(..., status="failed", progress_updates={"status": "needs-user-decision", "reason": "coder-setup-error"})`（L596-602）——本 change 复用这条既有收尾语义，让新异常类型走同一条 except 分支，不重新发明一套 phase 收尾代码路径。
- `_do_fix_in_session` 目前没有被 `run_fix` 的 try/except 包裹（L581-582 直接 `return`），因此该分支需要单独补一条对齐 `coder-setup-error` 语义的收尾（同样置 `needs-user-decision` + `coder-setup-error`），否则 in-session 分发下的拒绝会绕开既有收尾路径，留下悬挂 phase——这是本 change 必须堵上的一个执行细节，不是可选项。

### 5. spec 侧改动落点：`spec_fix_run` 内联新增分支

`spec_fix_run`（`src/npc/spec_pipeline.py` L610-685）在既有 `prev_spec_review_missing` 检查（L627-634）之后、`json.loads`（L636）之前插入 stale 扫描分支，返回值形状与既有 3 个错误分支一致（`{"ok": False, "change":, "round":, "error":, "detail":}`），额外携带 `max_round`（诊断字段，非必需契约键，仅供人工排障）。不触碰 `_write_pre_head_marker`（L661）之前的既有提前 return 结构。

## Pattern Mapping

以下为 `pattern-interrogation.md` 中 `## Open Questions` 与 `## User Decisions (Interactive)` 两段的原文（本 change 的 pattern-interrogation.md 含 `## User Decisions (Interactive)` H2 标题，按契约原样写入本段）：

### Open Questions

- 本 change 是否需要一并修复 code 侧 `coder.py::_render_prompt_file`（`run_fix` → `_do_fix_body`
  调用链）的同类 stale-review 缺陷？该缺陷与 spec 侧完全同构（`round-{round_n-1}.review.json`
  硬编码取值，不校验磁盘上是否存在更高轮次的 `round-*.review.json`），且 code 侧还叠加了一个
  更严重的相邻问题（review 文件缺失时静默渲染空 findings，而不是像 spec 侧那样结构化拒绝）。
  是（a）本 change 一并覆盖 code 侧两个问题，（b）本 change 只覆盖 spec 侧、code 侧的
  stale/missing 问题拆成独立 change，还是（c）本 change 只覆盖 spec 侧、code 侧问题记录但明确
  声明不处理（需要给出不处理的理由）？
- 新增的结构化错误键名，应采用用户原始目标文案中的 `stale-review-input`（kebab-case），还是
  与 `spec_fix_run` 现存的 3 个错误 slug（`prev_spec_review_missing` / `invalid_json` /
  `invalid_schema`，均为 snake_case）保持一致改为 `stale_review_input`？
- 若决定一并处理 code 侧（Open Question 1 选 a），`coder.py` 里对应的"missing review 静默渲染"
  问题是否也要在本 change 里同步修成结构化拒绝（对齐 spec 侧的 `prev_spec_review_missing`
  行为），还是维持现状只加 stale 校验、不动 missing 分支的既有行为？

### User Decisions (Interactive)

- **Open Question 1（范围）→ 选 (a)**：本 change 一并覆盖 code 侧两个问题。理由：spec 侧与 code 侧的 stale-review 缺陷完全同构，分开修会留下一个修了一个没修的漂移窗口；且 code 侧的 missing-review 静默渲染空 findings 比 stale 更危险——fixer 拿到空 findings 清单会以为无事可做，静默跳过修复。三处一起改，判据与错误标识统一。

- **Open Question 2（错误键名）→ 用 snake_case `stale_review_input`**：与 `spec_fix_run` 现存的 3 个错误 slug（`prev_spec_review_missing` / `invalid_json` / `invalid_schema`）保持一致。用户原始目标文案中的 kebab-case `stale-review-input` 仅为口语表述，不作为契约键名。

- **Open Question 3（code 侧 missing 分支）→ 一并改为结构化拒绝**：`coder.py::_render_prompt_file` 在 review 文件缺失时 MUST 返回结构化错误（对齐 spec 侧 `prev_spec_review_missing` 的行为语义），MUST NOT 静默渲染空 findings。这是本 change 的显式目标之一，不是顺带重构。

### 6. `spec-writer` capability 的契约演进：改写基线负向断言场景，而非新增互斥能力

基线 `openspec/specs/spec-writer/spec.md`「生成侧不得预知本轮评判标准」一条下有一个负向断言场景「fix 轮 prompt 不含当轮 review 内容」：GIVEN 磁盘上同时存在 `round-0.spec-review.json` 与 `round-1.spec-review.json`，WHEN 执行 `npc spec fix run --change <id> --round 1`，THEN prompt 含 round-0 的 finding `detail` 且 MUST NOT 含 round-1 的。

该 fixture 与本 change 新增的 `run-stale-review-guard` 能力直接冲突：同一输入下，前者要求"渲染 prompt、过滤当轮内容"，后者要求"整体拒绝、不渲染"。若本 change 仅以 `ADDED Requirements` 形式在独立 capability（`run-stale-review-guard`）里新增拒绝语义，归档后 `openspec/specs/` 下会同时存在两条互斥的真相源——`spec-writer` 能力仍记载"该输入应渲染"，`run-stale-review-guard` 能力记载"该输入应拒绝"，任何后续读者（人类或 LLM）都无法确定实际行为，形成规范漂移。

**时序核实**：真实轮次时序恒为 `review --round 0` → `fix --round 1` → `review --round 1` → `fix --round 2` → …（`npc spec review run --round N` 写入 `round-N.spec-review.json`；`npc spec fix run --round N` 消费 `round-(N-1).spec-review.json`）。因此在严格前向流程中，`fix --round 1` 执行时 `round-1.spec-review.json` 不可能存在——它只能在 `review --round 1` 之后才产生。该 fixture 描述的状态之所以仍是**可达**的，不是因为前向时序会产生它，而是因为存在重放/误重跑路径：例如编排层因 crash-recovery、人工误操作或脚本错误，在 `review --round 1` 已经跑完之后又重新调用了 `fix --round 1`（用了过期的轮次号）。这正是本 change 要拦截的场景本身，而不是一个不可达的合成状态——所以问题不是"该不该管这个状态"，而是"这个状态发生时，正确行为是渲染过滤后的 prompt，还是整体拒绝"。

**结论：改写而非并存。** 本 change 在 `specs/spec-writer/spec.md` 中以 `MODIFIED Requirements` 的方式改写该场景，断言改为"返回 `.ok == false` 且 `stale_review_input`，未写出任何 `round-1.spec-fix.prompt.md`"。理由：

- **不变量 1（生成侧不得预知本轮评判标准）不会被削弱，反而被加强**：原场景的保护手段是"渲染但过滤掉当轮内容"，依赖过滤逻辑本身正确无误；新行为的保护手段是"整体不渲染"——不存在的文件不可能含有任何内容，因此不存在过滤逻辑出错导致当轮内容泄漏的风险面。更强的不变量蕴含更弱的不变量，删除原断言不会打开任何原本被其单独把守的口子。
- **两条能力语义上是同一决策的两个层面**，不应分裂成两个独立 capability 各说各话：`run-stale-review-guard` 定义"什么时候拒绝"，`spec-writer` 定义"生成侧的时点边界"，而"过期输入下这两者如何交汇"必须由二者共同的真相源给出唯一答案——挂在 `spec-writer` capability 下，与既有"fix 轮 prompt 可含上一轮已签发 findings"这条正向场景放在同一 Requirement 内对照阅读，而不是让读者去 `run-stale-review-guard` 里另找一条互斥表述。
- **保留而非删除相邻的正向/缺失场景**：「fix 轮 prompt 可含上一轮已签发 findings」场景补充了 GIVEN 前提"磁盘上不存在轮次号大于 0 的其它 review 文件"，明确其只覆盖非 stale 输入；「上一轮 review 未落盘时 fix 拒绝渲染」（`prev_spec_review_missing`）场景不受影响，继续保留——两者与 stale 场景合起来覆盖了 `spec_fix_run` 三个错误分支的完整判定优先级（missing 优先于 stale，二者都通过才轮到正常渲染）。

## Risks / Trade-offs

- **新增私有辅助函数不跨模块共享**：spec 侧与 code 侧各自维护一份"扫描+取最大轮次号"的小函数，牺牲了少量 DRY，换取两个模块（`spec_pipeline.py` / `coder.py`）不因为两行逻辑产生新的相互依赖——符合既有代码库两条 fix 流水线本就分属不同模块、不共享内部实现细节的现状。
- **`_render_prompt_file` 签名变化**：从恒定成功的 `tuple[Path, str]` 变为可能携带失败信息，两个调用点都需要改动错误处理路径；implement 分支不受影响（stale/missing 校验只加在 fix 分支）。
- **诊断字段 `max_round` 非契约必需键**：spec 层不要求测试对其做强断言，只要求 `error == "stale_review_input"` 这一稳定标识存在；`max_round` 仅为人工排障提供额外上下文，避免过度约束实现细节。

## Migration

无数据迁移。纯行为收紧（原本会被静默消费/静默渲染的输入现在被拒绝），不改变任何已落盘 review/fix 产物的文件格式或命名。
