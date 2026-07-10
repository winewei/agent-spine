## 0. 落点清单的确定性枚举

命令与匹配计数（在 REPO_ROOT 执行）：

```
$ grep -n "_render_prompt_file(" src/npc/coder.py
250:def _render_prompt_file(
438:    prompt_file, spawn_text = _render_prompt_file(
468:    prompt_file, spawn_text = _render_prompt_file(
500:    _, spawn_text = _render_prompt_file(
621:    _, spawn_text = _render_prompt_file(
```

匹配计数：5（1 处定义 + 4 处调用）。其中 fix 阶段相关调用点为 **2 处**（`L468` 在 `_do_fix_in_session`、`L621` 在 `_do_fix_body`）；`L438`、`L500` 属于 implement 阶段调用点，不在本 change 范围内。

```
$ grep -n 'round-{round_n\|round_n - 1\|round_n-1' src/npc/spec_pipeline.py src/npc/coder.py
src/npc/spec_pipeline.py:625:    prev_round = round_n - 1
src/npc/spec_pipeline.py:663:    prompt_file = base / f"round-{round_n}.spec-fix.prompt.md"
src/npc/spec_pipeline.py:844:    review_path = base / f"round-{round_n}.spec-review.json"
src/npc/spec_pipeline.py:845:    events_path = base / f"round-{round_n}.spec-review.events.jsonl"
src/npc/coder.py:275:        prompt_file = base / f"round-{round_n}.fix.prompt.md"
src/npc/coder.py:276:        review_path = base / f"round-{round_n - 1}.review.json"
```

匹配计数：6。本 change 需要改动的落点是 `spec_pipeline.py:625`（`spec_fix_run` 的 `prev_round` 取值处，新增扫描分支的锚点）与 `coder.py:276`（`_render_prompt_file` fix 分支的 `review_path` 取值处，新增扫描 + missing 结构化拒绝的锚点）；`spec_pipeline.py:663/844/845` 与 `coder.py:275` 是 review/prompt 产物写入命名，本 change 不改动其命名规则。

## 1. spec 侧：`spec_fix_run` 新增 stale-review-input 校验

- [ ] 1.1 在 `prev_spec_review_missing` 检查（L627-634）之后、`json.loads`（L636）之前，新增私有辅助函数扫描 `base.glob("round-*.spec-review.json")`，用正则解析各文件名中的轮次号，取最大值 `max_round`
- [ ] 1.2 若 `max_round > round_n - 1`，返回 `{"ok": False, "change": change_id, "round": round_n, "error": "stale_review_input", "detail": ..., "max_round": max_round}`，不渲染任何 `round-{N}.spec-fix.prompt.md`，不写 `pre_head.fix-r{N}.txt` marker
- [ ] 1.3 单测：`max_round == round_n - 1`（无更高轮次）时行为不变，正常渲染
- [ ] 1.4 单测：`max_round > round_n - 1`（存在更高轮次）时返回 `stale_review_input`，且未写出 prompt 文件与 marker
- [ ] 1.5 单测：`round-{round_n-1}` 本身缺失时仍优先返回既有 `prev_spec_review_missing`，不被 stale 分支抢先判定

## 2. code 侧：`_render_prompt_file` fix 分支新增 stale + missing 结构化拒绝

- [ ] 2.1 在 fix 分支（L272-302）内，先做既有 `review_path.is_file()` 判断：`round-{round_n-1}.review.json` 不存在时，MUST 使函数产出一条可被调用方识别的失败信息（`error="prev_review_missing"`），MUST NOT 再静默把 `findings_md` 置空后继续渲染，MUST NOT 执行 2.2 的 stale 扫描
- [ ] 2.2 仅当 2.1 的存在性检查通过（基线文件存在）后，才新增私有辅助函数扫描 `base.glob("round-*.review.json")`，取最大轮次号 `max_round`
- [ ] 2.3 `max_round > round_n - 1` 时，MUST 使函数产出失败信息（`error="stale_review_input"`），MUST NOT 渲染 prompt
- [ ] 2.3a 单测：`round-{round_n-1}.review.json` 缺失但存在更高轮次的 `round-*.review.json` 时，MUST 返回 `prev_review_missing` 而非 `stale_review_input`（验证判定顺序：missing 检查先于 stale 扫描）
- [ ] 2.4 `_render_prompt_file` 的失败信息传递机制需要让两个调用点都能感知（详见 design.md「Decisions」第 4 条），不得只在其中一个调用点生效

## 3. code 侧：两个调用点的错误处理收尾

- [ ] 3.1 `_do_fix_body`（子进程分支，L605-644）：捕获 2.x 的失败信息，构造 `{"ok": False, "seq":, "error":, "round":, "detail":}` 并走既有 `run_fix` 的 `except (FileNotFoundError, NotImplementedError, ValueError, MimoEnvError)` 收尾路径（L596-602），phase 状态置 `needs-user-decision` / `reason=coder-setup-error`
- [ ] 3.2 `_do_fix_in_session`（in-session 分支，L455-483）：当前未被 `run_fix` 的 try/except 包裹（L581-582），需单独补一条与 3.1 语义对齐的收尾（同样置 `needs-user-decision` / `coder-setup-error`），不得绕开既有收尾路径留下悬挂 phase
- [ ] 3.3 单测：`--dispatch in-session` 与非 in-session（子进程）两种模式下，stale 与 missing 拒绝均生效，且拒绝后 progress 状态均为 `needs-user-decision`
- [ ] 3.4 单测：拒绝发生时未产生任何 coder 后端子进程调用（子进程分支）/ 未产出 `deferred=True` 指令（in-session 分支）

## 4. spec-writer capability：改写与本 change 冲突的基线负向断言场景

- [ ] 4.0 `specs/spec-writer/spec.md` 以 `MODIFIED Requirements` 改写「生成侧不得预知本轮评判标准」条下的「fix 轮 prompt 不含当轮 review 内容」场景：断言改为过期输入（`round-0.spec-review.json` 与 `round-1.spec-review.json` 同时存在时执行 `--round 1`）返回 `stale_review_input` 且不写出 prompt 文件，而非渲染并过滤当轮内容；理由见 design.md「Decisions」第 6 条
- [ ] 4.0a 归档时确认 `openspec/specs/spec-writer/spec.md` 只保留改写后的场景，不残留与 `run-stale-review-guard` 互斥的旧断言

## 5. 回归与收尾

- [ ] 5.1 `openspec validate fix-run-stale-review-guard --type change --strict` 通过
- [ ] 5.2 `uv run pytest -q` 全绿
- [ ] 5.3 确认既有 `prev_spec_review_missing` / `invalid_json` / `invalid_schema` 三个分支的错误标识与返回形状未被改动
