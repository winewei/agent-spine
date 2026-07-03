"""守卫测试：review-run-failure-branch change 的 skill 契约验证。

审计 B9 指出 spine-run.md 3b 循环直接读 `.blocking`/.stale` 而不先检查 `.ok`，
当 review 自身失败（codex-exec-failed）时 `jq` 返回 null → bash 整数比较报错。

本模块验证：
1. round0 的 `npc review run` 后存在 `.ok` 检查（失败路径不进入 while 循环）。
2. 循环内每次 `npc review run` 后同样存在 `.ok` 检查。
3. `codex-failed` 触发值出现在 3b review 失败路径中。
4. `codex-failed` 在 3d 触发场景映射表中有明确映射行。
5. 联动：`codex-failed` ∈ VALID_TRIGGERS（确保 npc auto-decide 不会 exit 2）。
"""

from __future__ import annotations

import re
from pathlib import Path

from npc import auto_decide as _auto_decide

REPO_ROOT = Path(__file__).resolve().parents[1]
SPINE_RUN_MD = REPO_ROOT / "plugins" / "agent-spine" / "commands" / "spine-run.md"


def _load_spine_run_md() -> str:
    assert SPINE_RUN_MD.exists(), f"spine-run.md 不存在：{SPINE_RUN_MD}"
    return SPINE_RUN_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Task 1.1 & 1.3：round0 后、while 前必须有 .ok 检查（不进循环路径）
# ---------------------------------------------------------------------------

def test_round0_ok_check_exists_before_while_loop():
    """round0 的 npc review run 后必须在进入 while 循环前检查 .ok。"""
    text = _load_spine_run_md()

    # 找到 "npc review run --seq $SEQ --round 0" 所在位置
    round0_pos = text.find("npc review run --seq $SEQ --round 0")
    assert round0_pos != -1, "spine-run.md 中找不到 round0 review 调用"

    # 找到 while 循环起始位置（round0 之后的第一个 while）
    while_pos = text.find("while [", round0_pos)
    assert while_pos != -1, "spine-run.md 中找不到 review-fix while 循环"

    # 在 round0 到 while 之间的片段中，必须有 .ok 检查
    between = text[round0_pos:while_pos]
    assert ".ok" in between, (
        "round0 npc review run 后到 while 循环之前，未找到 .ok 检查；"
        "违反不变量 2（必须先 .ok 再读业务字段）"
    )


def test_round0_failure_triggers_codex_failed():
    """round0 .ok 失败路径必须调用 --trigger codex-failed。"""
    text = _load_spine_run_md()

    round0_pos = text.find("npc review run --seq $SEQ --round 0")
    assert round0_pos != -1

    while_pos = text.find("while [", round0_pos)
    assert while_pos != -1

    between = text[round0_pos:while_pos]
    assert "codex-failed" in between, (
        "round0 .ok 失败路径（while 循环前）未找到 codex-failed trigger；"
        "审计 B9 要求：review 自身失败 → trigger=codex-failed"
    )


# ---------------------------------------------------------------------------
# Task 1.2：循环体内每次 npc review run 后必须有 .ok 检查
# ---------------------------------------------------------------------------

def test_loop_internal_review_run_has_ok_check():
    """循环体内的 npc review run 后必须跟随 .ok 检查。"""
    text = _load_spine_run_md()

    # 循环体内的 review run 带 --round $N（与 round0 区分）
    loop_review_pattern = re.compile(
        r"npc review run --seq \$SEQ --round \$N"
    )
    match = loop_review_pattern.search(text)
    assert match is not None, (
        "spine-run.md 循环体内找不到 `npc review run --seq $SEQ --round $N`"
    )

    # 在该调用之后（取后 300 字符）必须出现 .ok 检查
    after_review = text[match.end():match.end() + 300]
    assert ".ok" in after_review, (
        "循环体内 npc review run 之后 300 字符内未找到 .ok 检查；"
        "违反不变量 2"
    )


def test_loop_internal_review_failure_triggers_codex_failed():
    """循环体内 review run 失败路径必须调用 --trigger codex-failed。"""
    text = _load_spine_run_md()

    loop_review_pattern = re.compile(
        r"npc review run --seq \$SEQ --round \$N"
    )
    match = loop_review_pattern.search(text)
    assert match is not None

    # 在该调用之后（取后 400 字符）必须出现 codex-failed
    after_review = text[match.end():match.end() + 400]
    assert "codex-failed" in after_review, (
        "循环体内 npc review run 失败路径未找到 codex-failed trigger；"
        "审计 B9 要求：循环内 review 失败 → trigger=codex-failed"
    )


# ---------------------------------------------------------------------------
# Task 1.3：while 条件必须注明 .ok=true 前提，避免 null 整数比较
# ---------------------------------------------------------------------------

def test_while_condition_guarded_by_ok_check():
    """while 循环条件（读 .blocking/.stale）必须在 .ok=true 的保护下执行。"""
    text = _load_spine_run_md()

    # 找到 while 循环入口
    while_pos = text.find("while [ \"$(printf '%s' \"$R\" | jq -r '.blocking')")
    assert while_pos != -1, "spine-run.md 中找不到 while [ .blocking 条件"

    # while 循环前 500 字符内必须有 .ok 检查（由 round0 guard 提供）
    before_while = text[max(0, while_pos - 500):while_pos]
    assert ".ok" in before_while, (
        "while 条件读 .blocking 前 500 字符内未找到 .ok 检查；"
        ".ok=true 的保护缺失，null 参与整数比较风险未消除"
    )


# ---------------------------------------------------------------------------
# Task 1.4：3d 触发场景映射表必须包含 codex-failed 行
# ---------------------------------------------------------------------------

def test_3d_trigger_table_contains_codex_failed():
    """3d 触发场景映射表必须有 codex-failed 行（review 自身失败场景）。"""
    text = _load_spine_run_md()

    # 找到 3d 决策点章节
    section_3d_pos = text.find("### 3d.")
    assert section_3d_pos != -1, "spine-run.md 中找不到 ### 3d. 决策点章节"

    section_3d_text = text[section_3d_pos:]

    # 必须有以反引号包裹的 codex-failed 值（表格第二列形态）
    assert "`codex-failed`" in section_3d_text, (
        "3d 触发场景映射表中未找到 `codex-failed`；"
        "任务 1.4 要求：补充 codex-failed（review 自身失败）映射行"
    )


# ---------------------------------------------------------------------------
# Task 2.3 联动：codex-failed ∈ VALID_TRIGGERS
# ---------------------------------------------------------------------------

def test_codex_failed_in_valid_triggers():
    """codex-failed 必须在 auto_decide.VALID_TRIGGERS 中（联动词表守卫）。"""
    assert "codex-failed" in _auto_decide.VALID_TRIGGERS, (
        "codex-failed 不在 auto_decide.VALID_TRIGGERS 中；"
        "主 session 调用 `npc auto-decide --trigger codex-failed` 会 exit 2"
    )


# ---------------------------------------------------------------------------
# Task 2.1：所有 npc review run 出现处均有 .ok 分支
# ---------------------------------------------------------------------------

def test_all_review_run_calls_have_ok_check():
    """spine-run.md 中所有 `npc review run` 调用后均有 .ok 相关守卫。"""
    text = _load_spine_run_md()

    review_run_pattern = re.compile(r"npc review run --seq \$SEQ --round (?:\d+|\$N)")
    matches = list(review_run_pattern.finditer(text))
    assert matches, "spine-run.md 中找不到任何 npc review run 调用"

    failures = []
    for m in matches:
        # 往后取 500 字符，检查是否有 .ok 相关内容
        after = text[m.end():m.end() + 500]
        has_ok_check = ".ok" in after
        if not has_ok_check:
            # 也允许在调用之前的 500 字符内有保护（round0 guard if 结构）
            before = text[max(0, m.start() - 500):m.start()]
            has_ok_check = ".ok" in before
        if not has_ok_check:
            failures.append(
                f"  位置 {m.start()}: `{m.group()}` 周围未找到 .ok 检查"
            )

    assert not failures, (
        "以下 npc review run 调用缺少 .ok 守卫：\n" + "\n".join(failures)
    )
