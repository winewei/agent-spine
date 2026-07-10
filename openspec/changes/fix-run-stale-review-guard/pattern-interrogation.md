# Pattern Interrogation — fix-run-stale-review-guard

## Analogs

- `src/npc/spec_pipeline.py::spec_fix_run`（L610-685）—— 本次改动的直接目标函数。
  当前逻辑：`prev_round = round_n - 1` → `prev_review_path = base / f"round-{prev_round}.spec-review.json"`
  （L625-626）→ 若 `prev_review_path.exists()` 为假返回结构化错误 `prev_spec_review_missing`
  （L627-634）→ 否则直接 `json.loads` + `parse_spec_review` + `render_findings` 渲染 fix prompt。
  **全程没有"这份 review 是否是该 change 目录下轮次编号最大的一份"的校验** ——
  这正是本 change 要补的缺口：只要 `round-{N-1}.spec-review.json` 文件存在（哪怕磁盘上还有
  `round-{N}.spec-review.json` / `round-{N+1}.spec-review.json` 等更高轮次），当前实现会静默消费它。

- `src/npc/spec_pipeline.py::spec_review_run`（L797-845 起）—— spec review 产物的写入点。
  确认命名恒为 `round-{round_n}.spec-review.json`（L844）+ `round-{round_n}.spec-review.events.jsonl`
  （L845），`round_n` 由调用方（CLI `--round`）显式传入，**不是**从磁盘状态自动推导。
  这意味着"轮次节拍"完全由编排者（人或主 session）手动传参维护，npc 自身不校验调用序列的
  单调性/连续性——这正是用户描述的"人工在 review round K 后手动修复、直接跳到 review round K+1，
  跳过 fix round K+1"这一操作路径之所以能发生的根本原因：CLI 层没有任何机制阻止跳号或乱序调用。

- `src/npc/coder.py::_render_prompt_file`（L250-311，fix 分支见 L272-302）—— **code 侧的同构实现**。
  `review_path = base / f"round-{round_n - 1}.review.json"`（L276）与 spec 侧的
  `prev_review_path = base / f"round-{prev_round}.spec-review.json"` 逐行同构：同样只按
  `round_n - 1` 拼路径，同样不校验磁盘上是否存在轮次更高的 `round-*.review.json`。
  但错误处理路径**比 spec 侧更弱**：spec 侧文件不存在会返回结构化错误 `prev_spec_review_missing`
  拒绝渲染；code 侧只用 `if review_path.is_file():`（L284）做存在性判断，文件不存在时
  `findings_md` 静默保持为空字符串（L277），**照常渲染 fix prompt**，不报错、不拒绝——
  这是一个比"过期 review"更严重的相邻缺陷（缺失 review 也不拦截），但不在用户原始目标描述的
  "stale-review-input" 范围内，本次盘问先如实记录，留给 Open Questions 判断是否顺手处理。

- `src/npc/pipeline.py::run_review_round`（L678 起，写入点见 `review_path = base / f"round-{round_n}.review.json"`，
  约 L749）—— code 侧 review 产物写入点，命名 `round-{round_n}.review.json` +
  `round-{round_n}.events.jsonl`，`round_n` 同样是调用方显式传入的 CLI 参数，与 spec 侧
  `spec_review_run` 的模式完全一致（一次性写入，不做序列校验）。

- `src/npc/coder.py::run_fix` → `_do_fix_body`（L553-644）—— code 侧 `npc fix run` 的完整调用链：
  `cli_fix_run`（L715）→ `run_fix`（L553，`--round` 为 `args.round_n`，缺失时报 `missing_round`，
  L721-723）→ `_do_fix_body`（L605）→ `_render_prompt_file`（L621-623，即上面第三条 analog）。
  确认了"code 侧 `npc fix run --round N` 存在与 spec 侧完全同构的过期/错位 review 输入缺陷"，
  且**没有**任何现存护栏（既没有 stale 校验，也没有 missing 时的结构化拒绝）。

- `src/npc/spec_pipeline.py` 内其它错误短路的既有写法，可作为新校验的错误 dict 形态参照：
  `prev_spec_review_missing`（L628-634）、`invalid_json`（L639-645）、`invalid_schema`（L650-656）
  均为 `{"ok": False, "change": ..., "round": ..., "error": <slug>, "detail": <str>}` 的统一形状。
  新增的 `stale-review-input` 错误应遵循同一形状（`error` 用 kebab-case 还是 snake_case 需在
  Open Questions 中与既有 3 个 slug 的命名风格核对——现存 3 个均为 snake_case，
  而用户原始目标文案写的是 `stale-review-input`，两者不一致，需要拍板统一）。

- `src/npc/plan.py`（L714/733/1071）——仓库里对某个 change 目录做「按 glob 通配符枚举候选、
  取最新/取匹配」的既有模式参照（`(changes_root / "archive").glob(f"*-{cid}")`）。本 change
  引入的"扫描 `base` 目录下所有 `round-*.spec-review.json` 并取最大轮次号"逻辑，在写法风格上
  可参照此处的 `Path.glob` 用法；但目前仓库里**没有**任何现成的"扫描 round-N 文件、解析出
  N、取 max"的工具函数（`_round_n_template`、`extract_fixed_history` 等 `_focus.py` 里的函数
  是处理 already-fixed 历史文本渲染，不是路径扫描，不构成可直接复用的 analog）。

## Assumptions

- **`round-*.spec-review.json` 文件名中的轮次号可通过正则从文件名可靠解析**（形如
  `round-(\d+)\.spec-review\.json`），且该目录下不会出现同轮次号的重复/冲突文件（`_spec_base`
  返回的 `base` 目录专属于该 change，不与其它 change 混放）。
- **"轮次更高"的判定只看 spec-review 侧文件本身**，不交叉核对 spec-fix 侧是否也存在对应轮次的
  `round-{K}.spec-fix.prompt.md` / fix record——即校验逻辑是"review 文件序列内部的单调性"，
  不是"review 与 fix 两条序列的交叉节拍对齐"。用户原始描述的场景（review round K+1 存在但
  fix round K+1 从未跑过）恰好是"review 序列比 fix 序列领先"，只看 review 侧最大轮次号即可
  检测到这个错位，不需要引入 fix 侧文件扫描。
- **新增校验只在 `spec_fix_run` 渲染 prompt 之前执行一次**，属于确定性、无副作用的纯文件系统
  扫描（`Path.glob` + 正则解析文件名），不引入新的 telemetry 事件类型，也不改变
  `spec_fix_record` 的 RESULT 装订契约。
- **错误返回后不写任何标记文件**（不 touch `pre_head.fix-r{round_n}.txt`），即校验失败时函数在
  `_write_pre_head_marker`（L661）之前就 return，不留下半成品 marker——这与现有
  `prev_spec_review_missing` 分支的提前 return 位置（同样在 marker 写入之前）保持一致。
- **本 change 范围默认只覆盖 spec 侧 `spec_fix_run`**；code 侧 `coder.py::_render_prompt_file` /
  `run_fix` 的同类缺陷（含比 stale 更严重的"missing review 静默渲染"问题）是否一并在本 change
  修复，还是拆分成独立 change，留待 Open Questions 由用户拍板——因为用户原始目标文案里写的是
  "同类校验若同样适用于 code 侧的 npc fix run，一并处理或明确说明为何不适用"，这是一个需要
  显式决策的分支点，不是可以由撰写者单方面假设的细节。
- **`round-N.spec-review.json` 与 `round-N.spec-review.events.jsonl` 的轮次号严格一一对应**
  （`spec_review_run` L844-845 同时以 `round_n` 命名两者），因此新校验只需扫描
  `*.spec-review.json` 后缀即可覆盖所有已完成的 review 轮次，不需要额外扫描 `.events.jsonl`。

## Open Questions

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


## User Decisions (Interactive)

- **Open Question 1（范围）→ 选 (a)**：本 change 一并覆盖 code 侧两个问题。理由：spec 侧与 code 侧的 stale-review 缺陷完全同构，分开修会留下一个修了一个没修的漂移窗口；且 code 侧的 missing-review 静默渲染空 findings 比 stale 更危险——fixer 拿到空 findings 清单会以为无事可做，静默跳过修复。三处一起改，判据与错误标识统一。

- **Open Question 2（错误键名）→ 用 snake_case `stale_review_input`**：与 `spec_fix_run` 现存的 3 个错误 slug（`prev_spec_review_missing` / `invalid_json` / `invalid_schema`）保持一致。用户原始目标文案中的 kebab-case `stale-review-input` 仅为口语表述，不作为契约键名。

- **Open Question 3（code 侧 missing 分支）→ 一并改为结构化拒绝**：`coder.py::_render_prompt_file` 在 review 文件缺失时 MUST 返回结构化错误（对齐 spec 侧 `prev_spec_review_missing` 的行为语义），MUST NOT 静默渲染空 findings。这是本 change 的显式目标之一，不是顺带重构。
