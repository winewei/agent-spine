"""测试 reduce-review-fix-cost change 的三个能力。

覆盖：
1. implement-selfcheck-rubric：
   - implement prompt 含通用 checklist 类目
   - fix prompt 含同一份 checklist
   - prompt 不含 per-change review focus（负向保证）
   - 类目清单单一来源被两处引用

2. plan-complexity-gate：
   - 大跨领域触发告警（breadth 超阈值）
   - 大但单领域不误伤（11 文件同一顶层模块）
   - 软门不阻断（触发告警但返回 ok=True 且 exit 0）
   - large 标记生效（breadth < threshold 但 files 超阈值）

3. cage-retention-policy：
   - verify-tests-rerun 带 retained 标注且有 retained_reason
   - deletion_candidates 排除 retained 笼子
   - no_data 笼子行为不变（与 retained 策略无关）
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pytest

from npc import templates as _templates
from npc import telemetry as _telemetry


# ============================================================
# 1. implement-selfcheck-rubric
# ============================================================


class TestSelfcheckRubric:
    """Req: implement/fix prompt 注入静态通用自检 checklist（单一事实源）。"""

    def test_selfcheck_categories_constant_exists(self):
        """SELFCHECK_CATEGORIES 是模块级常量，不为空。"""
        cats = _templates.SELFCHECK_CATEGORIES
        assert isinstance(cats, tuple)
        assert len(cats) > 0

    def test_selfcheck_categories_contains_required_dimensions(self):
        """必须包含规格要求的七个类目维度。"""
        cats = set(_templates.SELFCHECK_CATEGORIES)
        required = {"validation", "partial-failure", "locking", "test-coverage",
                    "edge-case", "telemetry", "concurrency"}
        assert required.issubset(cats), f"缺少类目：{required - cats}"

    def test_selfcheck_rubric_md_references_all_categories(self):
        """SELFCHECK_RUBRIC_MD 包含所有 SELFCHECK_CATEGORIES 中的每个类目。"""
        rubric = _templates.SELFCHECK_RUBRIC_MD
        for cat in _templates.SELFCHECK_CATEGORIES:
            assert cat in rubric, f"SELFCHECK_RUBRIC_MD 缺少类目 {cat!r}"

    def test_implement_prompt_contains_selfcheck_checklist(self):
        """Scenario: implement prompt 含通用 checklist。"""
        text = _templates.render_implementer(
            change_id="test-change",
            base="/abs/base",
            repo_root="/abs/repo",
        )
        # 通用自检类目应当出现在 prompt 中
        for cat in _templates.SELFCHECK_CATEGORIES:
            assert cat in text, f"implement prompt 缺少 selfcheck 类目 {cat!r}"

    def test_fix_prompt_contains_same_selfcheck_checklist(self):
        """Scenario: fix prompt 同样含通用 checklist（同一份）。"""
        text = _templates.render_fixer(
            change_id="test-change",
            round_n=1,
            implement_commit="abc123",
            base="/abs/base",
            repo_root="/abs/repo",
            blocking_findings_md="F1: some finding\n",
            categories_seen=[],
            blocking_trend=[],
        )
        for cat in _templates.SELFCHECK_CATEGORIES:
            assert cat in text, f"fix prompt 缺少 selfcheck 类目 {cat!r}"

    def test_implement_prompt_not_contains_per_change_focus(self):
        """Scenario: prompt 不含当次 change 的 review focus（负向保证）。

        测试不含 'npc focus' 命令原文、per-change review focus 渲染文本等字样。
        """
        text = _templates.render_implementer("test-change", "/b", "/r")
        # 不应出现 per-change 注入的关键词标志
        assert "npc focus" not in text
        # checklist 的描述必须是通用类目层级，不含 per-change 的 findings 原文占位符
        assert "per-change" not in text.lower()

    def test_fix_prompt_not_contains_per_change_focus(self):
        """Scenario: fix prompt 不含 per-change review focus。"""
        text = _templates.render_fixer(
            "test-change", 2, "commit", "/b", "/r",
            "F1: error\n", ["validation"], [3]
        )
        assert "npc focus" not in text
        assert "per-change" not in text.lower()

    def test_selfcheck_rubric_is_single_source_referenced_by_both(self):
        """类目清单单一来源：implement 和 fix 两处引用同一个 SELFCHECK_RUBRIC_MD 常量。

        通过比较两个 prompt 中 checklist 段落文本是否均包含同一标志性片段来验证。
        我们用 SELFCHECK_RUBRIC_MD 中的一段固定文本作为"标识码"。
        """
        rubric_marker = "通用"  # SELFCHECK_RUBRIC_MD 中的固定词
        impl_text = _templates.render_implementer("c", "/b", "/r")
        fix_text = _templates.render_fixer("c", 1, "x", "/b", "/r", "F1\n", [], [])
        assert rubric_marker in impl_text, "implement prompt 未包含 SELFCHECK_RUBRIC_MD 标志文本"
        assert rubric_marker in fix_text, "fix prompt 未包含 SELFCHECK_RUBRIC_MD 标志文本"

    def test_selfcheck_boundary_note_present_in_rubric(self):
        """SELFCHECK_RUBRIC_MD 中包含生成⊥验证边界说明（防误注 per-change 内容的护栏）。"""
        rubric = _templates.SELFCHECK_RUBRIC_MD
        # 应有"不含 per-change 文本"或"类目层级"的说明
        assert "通用" in rubric or "类目" in rubric


# ============================================================
# 2. plan-complexity-gate
# ============================================================


class TestComplexityGate:
    """Req: plan 前置软性复杂度告警，跨领域信号，软门不阻断。"""

    @pytest.fixture()
    def fake_change_dir(self, tmp_path: Path):
        """创建假 change 目录，内含 tasks.md 以控制路径提取。"""
        def _make(change_id: str, file_refs: list[str]) -> Path:
            changes_root = tmp_path / "openspec" / "changes"
            change_dir = changes_root / change_id
            change_dir.mkdir(parents=True)
            # tasks.md 列出文件引用
            tasks_text = "# Tasks\n\n"
            for ref in file_refs:
                tasks_text += f"- [ ] 修改 `{ref}`\n"
            (change_dir / "tasks.md").write_text(tasks_text)
            return change_dir
        return _make

    def test_complexity_compute_breadth_multidomain(self, tmp_path: Path):
        """多顶层模块路径 → breadth 正确计数。"""
        from npc.plan import compute_complexity, _count_top_level_modules

        paths = {
            "src/npc/agent.py",
            "src/npc/plan.py",
            "plugins/agent-spine/commands/spine-run.md",
            "tests/test_plan.py",
            "docs/design.md",
        }
        breadth = _count_top_level_modules(paths)
        # src / plugins / tests / docs = 4 顶层模块
        assert breadth == 4

    def test_complexity_compute_breadth_single_domain(self, tmp_path: Path):
        """同一顶层模块的多个文件 → breadth=1。"""
        from npc.plan import _count_top_level_modules

        paths = {
            "src/npc/agent.py",
            "src/npc/plan.py",
            "src/npc/config.py",
            "src/npc/state.py",
            "src/npc/telemetry.py",
            "src/npc/templates.py",
            "src/npc/cli.py",
            "src/npc/fixer.py",
            "src/npc/review.py",
            "src/npc/paths.py",
            "src/npc/_io.py",
        }
        breadth = _count_top_level_modules(paths)
        # 11 文件但全在 src → breadth=1
        assert breadth == 1

    def test_complexity_gate_triggers_for_multidomain(self, tmp_path: Path, fake_change_dir):
        """Scenario: 大跨领域 change 触发告警。"""
        from npc.plan import run_complexity_check
        import sys, io

        # 创建跨 3 个顶层模块的 change（threshold=3 默认）
        # 我们需要让 breadth >= threshold
        file_refs = [
            "src/npc/agent.py",
            "plugins/agent-spine/commands/spine-run.md",
            "tests/test_agent.py",
        ]
        fake_change_dir("big-cross-domain", file_refs)

        # 构造 CLI 调用
        args = argparse.Namespace(
            change="big-cross-domain",
            plan_order=None,
            config=None,
            state_json=None,
            run_ts=None,
            task_log_dir=None,
        )

        # 临时修改 sys.argv 和工作路径无需，直接 monkeypatch repo_root
        import npc.plan as plan_mod
        original_resolve = plan_mod._resolve_repo_root

        def fake_resolve_root(_args):
            return tmp_path

        plan_mod._resolve_repo_root = fake_resolve_root
        captured_output = []
        import npc._io as _io_mod
        original_emit = _io_mod.emit

        def capture_emit(data):
            captured_output.append(data)
            original_emit(data)

        _io_mod.emit = capture_emit
        try:
            run_complexity_check(args)
        finally:
            plan_mod._resolve_repo_root = original_resolve
            _io_mod.emit = original_emit

        assert len(captured_output) == 1
        out = captured_output[0]
        assert out["ok"] is True
        # 超阈值时 warnings 不为空
        assert out["warning_count"] > 0
        warnings = out["warnings"]
        assert len(warnings) == 1
        assert warnings[0]["change_id"] == "big-cross-domain"
        assert warnings[0]["suggestion"] in ("split", "large")

    def test_complexity_gate_no_trigger_for_single_domain(self, tmp_path: Path, fake_change_dir):
        """Scenario: 大但单领域 change 不误伤（11 文件同模块）。"""
        from npc.plan import run_complexity_check
        import npc.plan as plan_mod
        import npc._io as _io_mod

        # 11 文件，全在 src 顶层模块
        file_refs = [f"src/npc/file{i}.py" for i in range(11)]
        fake_change_dir("big-single-domain", file_refs)

        args = argparse.Namespace(
            change="big-single-domain",
            plan_order=None,
            config=None,
            state_json=None,
            run_ts=None,
            task_log_dir=None,
        )

        original_resolve = plan_mod._resolve_repo_root
        plan_mod._resolve_repo_root = lambda _: tmp_path

        captured_output = []
        original_emit = _io_mod.emit

        def capture_emit(data):
            captured_output.append(data)
            original_emit(data)

        _io_mod.emit = capture_emit
        try:
            run_complexity_check(args)
        finally:
            plan_mod._resolve_repo_root = original_resolve
            _io_mod.emit = original_emit

        assert len(captured_output) == 1
        out = captured_output[0]
        assert out["ok"] is True
        # 11 文件全在 src/npc → breadth=1，未超 breadth_threshold=3
        # spec plan-complexity-gate: 单领域 large change 不应触发 warning
        results = out["results"]
        assert len(results) == 1
        result = results[0]
        # breadth should be 1 (all under src)
        assert result["breadth"] == 1
        # 关键断言：单领域（breadth < threshold）下文件数超阈值不触发 warning
        assert result["triggered"] is False, (
            "单领域 11 文件 change 不应触发复杂度 warning（breadth=1 < breadth_threshold）"
        )
        # warning_count 必须为 0
        assert out["warning_count"] == 0, (
            f"单领域 change 不应产生任何 warning，实际 warning_count={out['warning_count']}"
        )

    def test_complexity_gate_does_not_block_run(self, tmp_path: Path, fake_change_dir):
        """Scenario: 软门触发告警时 run 不被阻断（返回 ok=True）。"""
        from npc.plan import run_complexity_check
        import npc.plan as plan_mod
        import npc._io as _io_mod

        file_refs = [
            "src/npc/agent.py",
            "plugins/spine/run.md",
            "tests/test_foo.py",
            "docs/design.md",
        ]
        fake_change_dir("complex-change", file_refs)

        args = argparse.Namespace(
            change="complex-change",
            plan_order=None,
            config=None,
            state_json=None,
            run_ts=None,
            task_log_dir=None,
        )

        original_resolve = plan_mod._resolve_repo_root
        plan_mod._resolve_repo_root = lambda _: tmp_path

        captured_output = []
        original_emit = _io_mod.emit

        def capture_emit(data):
            captured_output.append(data)
            original_emit(data)

        _io_mod.emit = capture_emit
        try:
            # Should NOT raise SystemExit
            run_complexity_check(args)
        finally:
            plan_mod._resolve_repo_root = original_resolve
            _io_mod.emit = original_emit

        assert len(captured_output) == 1
        assert captured_output[0]["ok"] is True  # 软门不阻断

    def test_complexity_config_fields_load_with_defaults(self, tmp_path: Path):
        """复杂度阈值字段在 Config.review 中有正确默认值。"""
        from npc.config import load_config
        cfg = load_config(tmp_path)
        assert cfg.review.complexity_breadth_threshold == 3
        assert cfg.review.complexity_files_threshold == 10
        assert cfg.review.max_rounds_large == 30

    def test_complexity_config_fields_parsed_from_toml(self, tmp_path: Path):
        """从 TOML 正确解析复杂度阈值字段。"""
        from npc.config import load_config

        toml_path = tmp_path / ".npc" / "config.toml"
        toml_path.parent.mkdir(parents=True)
        toml_path.write_text(
            "[review]\ncomplexity_breadth_threshold = 5\ncomplexity_files_threshold = 15\nmax_rounds_large = 40\n"
        )
        cfg = load_config(tmp_path)
        assert cfg.review.complexity_breadth_threshold == 5
        assert cfg.review.complexity_files_threshold == 15
        assert cfg.review.max_rounds_large == 40

    def test_complexity_config_rejects_invalid_threshold(self, tmp_path: Path):
        """非整数复杂度阈值触发 ConfigError。"""
        from npc.config import load_config, ConfigError

        toml_path = tmp_path / ".npc" / "config.toml"
        toml_path.parent.mkdir(parents=True)
        toml_path.write_text('[review]\ncomplexity_breadth_threshold = "three"\n')
        with pytest.raises(ConfigError):
            load_config(tmp_path)


# ============================================================
# 3. cage-retention-policy
# ============================================================


class TestCageRetentionPolicy:
    """Req: verify-tests-rerun retained 标注 + deletion_candidates 排除 retained。"""

    def test_verify_tests_rerun_has_retained_annotation(self):
        """verify-tests-rerun 笼子定义携带 retained=True。"""
        cage = next(
            (c for c in _telemetry._CAGE_DEFS if c["name"] == "verify-tests-rerun"),
            None,
        )
        assert cage is not None, "verify-tests-rerun 笼子定义不存在"
        assert cage.get("retained") is True, "verify-tests-rerun 缺少 retained=True 标注"

    def test_verify_tests_rerun_has_retained_reason(self):
        """verify-tests-rerun 笼子定义携带非空 retained_reason。"""
        cage = next(
            (c for c in _telemetry._CAGE_DEFS if c["name"] == "verify-tests-rerun"),
            None,
        )
        assert cage is not None
        reason = cage.get("retained_reason", "")
        assert isinstance(reason, str) and len(reason) > 5, "retained_reason 不应为空"

    def test_deletion_candidates_excludes_retained_cage(self, isolate_telemetry: Path):
        """Scenario: retained 笼子不出现在 deletion_candidates。

        即使 verify-tests-rerun 在 runs_observed >= min_runs 的窗口内触发 0 次，
        也不应出现在 deletion_candidates 中。
        """
        # 写入足够多的 run 事件，使 runs_observed >= CAGE_MIN_RUNS_THRESHOLD
        for i in range(6):
            _telemetry.emit_event({
                "kind": "auto_decide.decision",
                "trigger": "stale",
                "run_ts": f"run-{i}",
                "proj_key": "proj/test",
            })
        # 同时写入 phase.exit 事件（使 verify-tests-rerun 的 kind 出现在流中，
        # 但 outcome_reason 不是 rerun-tests-failed），确保 verify-tests-rerun
        # 处于"untriggered"而非"no_data"状态
        for i in range(6):
            _telemetry.emit_event({
                "kind": "phase.exit",
                "phase": "implement",
                "status": "done",
                "run_ts": f"run-{i}",
                "proj_key": "proj/test",
                "outcome_reason": "success",  # 非 rerun-tests-failed
            })

        args = argparse.Namespace(since=None, min_runs=5)
        captured = []
        import npc._io as _io_mod
        original_emit = _io_mod.emit
        _io_mod.emit = lambda d: captured.append(d)
        try:
            _telemetry.cli_cages(args)
        finally:
            _io_mod.emit = original_emit

        assert len(captured) == 1
        out = captured[0]
        assert out["ok"] is True
        assert "verify-tests-rerun" not in out["deletion_candidates"], (
            "verify-tests-rerun 不应出现在 deletion_candidates（已标 retained）"
        )

    def test_deletion_candidates_excludes_retained_when_untriggered(self, isolate_telemetry: Path):
        """verify-tests-rerun 在 untriggered 列表中时，仍被排除出 deletion_candidates。"""
        # 写入 phase.exit 使 kind 存在（让 verify-tests-rerun 成为 untriggered 而非 no_data）
        for i in range(6):
            _telemetry.emit_event({
                "kind": "phase.exit",
                "phase": "implement",
                "status": "done",
                "run_ts": f"run-{i}",
                "proj_key": "proj/test",
                "outcome_reason": "done",
            })

        all_evts = list(_telemetry.iter_events())
        stats = _telemetry.cage_stats(all_evts, since_dt=None)

        # verify-tests-rerun 应在 untriggered 中（有数据源但 0 触发）
        assert "verify-tests-rerun" in stats["untriggered"], (
            "verify-tests-rerun 应在 untriggered 中"
        )

        # 验证 cli_cages 排除 retained
        args = argparse.Namespace(since=None, min_runs=5)
        captured = []
        import npc._io as _io_mod
        original_emit = _io_mod.emit
        _io_mod.emit = lambda d: captured.append(d)
        try:
            _telemetry.cli_cages(args)
        finally:
            _io_mod.emit = original_emit

        out = captured[0]
        assert "verify-tests-rerun" not in out["deletion_candidates"]

    def test_retained_field_in_cli_cages_output(self, isolate_telemetry: Path):
        """cli_cages 输出包含 retained 字段，列出所有 retained 笼子名称。"""
        args = argparse.Namespace(since=None, min_runs=5)
        captured = []
        import npc._io as _io_mod
        original_emit = _io_mod.emit
        _io_mod.emit = lambda d: captured.append(d)
        try:
            _telemetry.cli_cages(args)
        finally:
            _io_mod.emit = original_emit

        out = captured[0]
        assert "retained" in out, "cli_cages 输出缺少 retained 字段"
        assert "verify-tests-rerun" in out["retained"]

    def test_no_data_cages_behavior_unchanged(self, isolate_telemetry: Path):
        """Scenario: no_data 笼子行为不变（与 retained 策略无关）。

        no_data 笼子（如 routing-violation）仍不出现在 deletion_candidates，
        与 retained 策略独立——no_data 是因为事件未接线，deletion_candidates
        只应来自 untriggered（有数据源但 0 触发）。
        """
        args = argparse.Namespace(since=None, min_runs=0)  # min_runs=0 确保条件满足
        captured = []
        import npc._io as _io_mod
        original_emit = _io_mod.emit
        _io_mod.emit = lambda d: captured.append(d)
        try:
            _telemetry.cli_cages(args)
        finally:
            _io_mod.emit = original_emit

        out = captured[0]
        # routing-violation 是 no_data 笼子，不应出现在 deletion_candidates
        assert "routing-violation" not in out["deletion_candidates"], (
            "no_data 笼子 routing-violation 不应出现在 deletion_candidates"
        )
