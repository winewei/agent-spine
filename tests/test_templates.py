"""templates 模块单元测试（v1.0.0）。"""

from __future__ import annotations

from npc import templates


def test_template_version_is_string():
    assert isinstance(templates.TEMPLATE_VERSION, str)
    assert templates.TEMPLATE_VERSION.count(".") >= 1


def test_render_implementer_substitutes_runtime_vars():
    text = templates.render_implementer(
        change_id="add-foo",
        base="/abs/base",
        repo_root="/abs/repo",
    )
    assert "add-foo" in text
    assert "/abs/base" in text
    assert "/abs/repo" in text
    assert "implement.summary.md" in text


def test_render_implementer_includes_dual_artifact_contract():
    text = templates.render_implementer("c", "/b", "/r")
    # 双产物契约：summary 文件路径 + RESULT 行
    assert "Write" in text
    assert "RESULT:" in text
    # summary path 必须出现在 RESULT 模板里
    assert "/b/implement.summary.md" in text


def test_render_implementer_forbids_archive_in_subagent():
    text = templates.render_implementer("c", "/b", "/r")
    assert "不要 archive" in text


def test_render_fixer_substitutes_all_runtime_vars():
    text = templates.render_fixer(
        change_id="add-foo",
        round_n=3,
        implement_commit="deadbeef",
        base="/abs/base",
        repo_root="/abs/repo",
        blocking_findings_md="## F1 — [high][validation] x\n",
        categories_seen=["validation", "edge-case"],
        blocking_trend=[5, 4, 4],
    )
    assert "add-foo" in text
    assert "deadbeef" in text
    assert "FIX_ROUND=3" in text
    assert "round-3.fix.summary.md" in text
    assert "F1" in text
    assert "validation, edge-case" in text
    assert "5 → 4 → 4" in text


def test_render_fixer_first_round_empty_history():
    text = templates.render_fixer(
        change_id="c",
        round_n=1,
        implement_commit="abc",
        base="/b",
        repo_root="/r",
        blocking_findings_md="（本轮无 in_scope blocking findings）\n",
        categories_seen=[],
        blocking_trend=[],
    )
    assert "首轮" in text  # categories_seen / blocking_trend 缺省占位


def test_render_fixer_includes_root_cause_sweep_rule():
    text = templates.render_fixer(
        "c", 1, "abc", "/b", "/r", "F1\n", ["validation"], [3]
    )
    assert "Root-cause" in text
    assert "Locations Scanned" in text


def test_render_fixer_includes_real_regression_rule():
    text = templates.render_fixer(
        "c", 1, "abc", "/b", "/r", "F1\n", ["concurrency"], [3]
    )
    assert "Real Regressions" in text
    assert "concurrency" in text
    assert "race-condition" in text


def test_render_fixer_commit_format_includes_change_id_and_round():
    text = templates.render_fixer("change-x", 7, "abc", "/b", "/r", "F1\n", [], [])
    assert 'fix(change-x): review round 7' in text


def test_render_implementer_includes_atomic_add_discipline():
    text = templates.render_implementer("c", "/b", "/r")
    assert "git add -A" in text
    assert "git add ." in text
    assert "git diff --cached --name-only" in text
    assert "git restore --staged" in text
    assert "git stash" in text
    # commit 清单 ↔ summary.md 一致的自报口径
    assert "Files Modified" in text


def test_render_fixer_includes_atomic_add_discipline():
    text = templates.render_fixer(
        "c", 1, "abc", "/b", "/r", "F1\n", ["validation"], [3]
    )
    assert "git add -A" in text
    assert "git add ." in text
    assert "git diff --cached --name-only" in text
    assert "git restore --staged" in text
    assert "git stash" in text


def test_atomic_add_discipline_shared_constant_single_source():
    # 两个入口引用同一常量，文案完全一致，防止漂移
    impl = templates.render_implementer("c", "/b", "/r")
    fix = templates.render_fixer("c", 1, "abc", "/b", "/r", "F1\n", [], [])
    assert templates.ATOMIC_ADD_DISCIPLINE_MD in impl
    assert templates.ATOMIC_ADD_DISCIPLINE_MD in fix


# ============================================================
# change fix-prompt-exhaustive-sweep：连续复现升级层
# ============================================================


def _fixer(**kw):
    base = dict(
        change_id="c",
        round_n=2,
        implement_commit="abc",
        base="/b",
        repo_root="/r",
        blocking_findings_md="F1\n",
        categories_seen=["error-handling"],
        blocking_trend=[3, 3],
    )
    base.update(kw)
    return templates.render_fixer(**base)


def test_render_fixer_no_escalation_byte_equivalent_to_baseline():
    # 未传升级参数 vs 传空升级参数（未达阈值、无复现）→ 与基线逐字等价
    baseline = _fixer()
    with_empty = _fixer(
        category_streaks={}, recurred_categories=[], category_streak_threshold=2
    )
    assert baseline == with_empty
    assert "连续复现升级" not in baseline


def test_render_fixer_below_threshold_no_escalation():
    text = _fixer(
        category_streaks={"error-handling": 1},
        recurred_categories=[],
        category_streak_threshold=2,
    )
    assert "连续复现升级" not in text


def test_render_fixer_streak_at_threshold_triggers_exhaustive():
    text = _fixer(
        category_streaks={"error-handling": 2},
        recurred_categories=[],
        category_streak_threshold=2,
    )
    assert "连续复现升级" in text
    assert "强制穷举清单" in text
    assert "error-handling" in text
    assert "已覆盖" in text and "新增覆盖" in text and "确认不可达" in text
    assert "连续 2 轮" in text


def test_render_fixer_recurred_below_threshold_still_escalates():
    # unsubstantiated（复现）即使 streak 未达阈值仍强制穷举
    text = _fixer(
        category_streaks={"error-handling": 1},
        recurred_categories=["error-handling"],
        category_streak_threshold=2,
    )
    assert "连续复现升级" in text
    assert "复现/未被证实" in text


def test_render_fixer_escalation_uses_recurrence_not_refutation_wording():
    # design D7：文案用"复现/未被证实"，不用"证伪/为假"
    text = _fixer(
        category_streaks={"error-handling": 3},
        recurred_categories=["error-handling"],
        category_streak_threshold=2,
    )
    assert "证伪" not in text
    assert "为假" not in text


def test_render_fixer_custom_threshold_shifts_trigger():
    # 阈值 3：streak=2 不触发，streak=3 触发
    t2 = _fixer(category_streaks={"error-handling": 2}, category_streak_threshold=3)
    assert "连续复现升级" not in t2
    t3 = _fixer(category_streaks={"error-handling": 3}, category_streak_threshold=3)
    assert "连续复现升级" in t3


def test_render_spawn_prompt_implement_minimal():
    s = templates.render_spawn_prompt(
        phase="implement",
        change_id="add-foo",
        prompt_file="/abs/prompt.md",
    )
    assert "/abs/prompt.md" in s
    assert "add-foo" in s
    assert "RESULT" in s
    # 不能包含 §A 模板本体（这正是优化点：模板内容不能流过主 session）
    # 引导语可以"提及"双产物契约这个名词以提示 sub-agent，但不能展开成完整 schema：
    assert "Implement Summary —" not in s  # summary 文件 schema header
    assert "Key Decisions" not in s  # summary 段名
    assert "Issues Encountered" not in s  # summary 段名


def test_render_spawn_prompt_fix_includes_phrase():
    s = templates.render_spawn_prompt(
        phase="fix",
        change_id="add-foo",
        prompt_file="/abs/p.md",
    )
    assert "review findings" in s
    assert "add-foo" in s


def test_render_spawn_prompt_with_extension():
    s = templates.render_spawn_prompt(
        phase="implement",
        change_id="add-foo",
        prompt_file="/p.md",
        extension="- 跑 make race-test 而不是 go test\n- commit 前必须签名",
    )
    assert "追加约束" in s
    assert "make race-test" in s


def test_render_spawn_prompt_size_under_400_bytes_no_extension():
    """spawn-prompt 引导语应该极薄——这是 v1.0 设计目标之一。"""
    s = templates.render_spawn_prompt(
        phase="implement",
        change_id="add-foo",
        prompt_file="/some/abs/path/to/implement.prompt.md",
    )
    assert len(s.encode("utf-8")) < 400


def test_implementer_template_significantly_larger_than_spawn_prompt():
    """证明优化的实际价值：完整模板 vs 薄引导语。"""
    impl = templates.render_implementer("add-foo", "/b", "/r")
    spawn = templates.render_spawn_prompt(
        "implement", "add-foo", "/b/implement.prompt.md"
    )
    # 模板内容应该至少是引导语的 5 倍（实际是 10x+）
    assert len(impl) > 5 * len(spawn)
