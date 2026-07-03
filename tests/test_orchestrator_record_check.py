"""守卫测试：spine-run.md 中 implement/fix record 调用后必须紧跟 .ok 检查。

覆盖 change orchestrator-check-record-result 的 spec 契约：
- implement record 失败 → 立即转 3d 决策点（implementer-failed）
- fix record 失败 → 立即转 3d 决策点（fixer-failed）
- record 返回值是 coder 成败唯一真相，不得绕过
- Guardrails 中明确禁止在 record 失败后继续 review/archive
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def spine_run_text() -> str:
    spine_run = (
        Path(__file__).parent.parent
        / "plugins"
        / "agent-spine"
        / "commands"
        / "spine-run.md"
    )
    return spine_run.read_text(encoding="utf-8")


# ============================================================
# Task 1.1 / Scenario: implement record 失败转决策点
# ============================================================


class TestImplementRecordCheck:
    """验证 spine-run.md 在 implement record 后强制检查返回值。"""

    def test_implement_record_result_captured(self, spine_run_text: str):
        """npc implement record 的返回值必须被捕获（赋给变量），不能丢弃。"""
        # 必须有 REC=$(npc implement record ...) 形式的赋值
        assert "REC=$(npc implement record" in spine_run_text, (
            "implement record 返回值未被捕获：应使用 REC=$(npc implement record ...) 形式"
        )

    def test_implement_record_ok_checked(self, spine_run_text: str):
        """捕获 implement record 返回值后必须检查 .ok 字段。"""
        assert '"$REC"' in spine_run_text or "\"$REC\"" in spine_run_text or "$REC" in spine_run_text, (
            "REC 变量捕获后未被引用"
        )
        # 检查 .ok 读取模式
        assert "jq -r '.ok'" in spine_run_text, "spine-run.md 未包含 .ok 的 jq 检查模式"

    def test_implement_record_failure_triggers_implementer_failed(self, spine_run_text: str):
        """implement record 失败路径必须触发 implementer-failed trigger。"""
        assert "implementer-failed" in spine_run_text, (
            "spine-run.md 未包含 implementer-failed trigger"
        )

    def test_implement_record_checks_status_needs_user_decision(self, spine_run_text: str):
        """implement record 检查必须包含 needs-user-decision 状态判断。"""
        assert "needs-user-decision" in spine_run_text, (
            "spine-run.md 未包含 needs-user-decision 状态检查"
        )

    def test_implement_record_failure_before_review(self, spine_run_text: str):
        """implement record 的失败检查必须出现在进入 review 循环之前。

        检测顺序：REC=$(npc implement record ...) 出现在 npc review run --round 0 之前。
        """
        rec_pos = spine_run_text.find("REC=$(npc implement record")
        review_pos = spine_run_text.find("npc review run --seq $SEQ --round 0")
        assert rec_pos != -1, "implement record 赋值语句未找到"
        assert review_pos != -1, "npc review run round 0 语句未找到"
        assert rec_pos < review_pos, (
            "implement record 检查必须出现在 review run 之前（record 结果是进入 review 的前提）"
        )


# ============================================================
# Task 1.2 / Scenario: fix record 失败不再被静默吞掉
# ============================================================


class TestFixRecordCheck:
    """验证 spine-run.md 在 fix record 后强制检查返回值。"""

    def test_fix_record_result_captured(self, spine_run_text: str):
        """npc fix record 的返回值必须被捕获（赋给变量），不能丢弃。"""
        assert "FREC=$(npc fix record" in spine_run_text, (
            "fix record 返回值未被捕获：应使用 FREC=$(npc fix record ...) 形式"
        )

    def test_fix_record_ok_checked(self, spine_run_text: str):
        """捕获 fix record 返回值后必须检查 .ok 字段。"""
        assert "$FREC" in spine_run_text, "FREC 变量捕获后未被引用"

    def test_fix_record_failure_triggers_fixer_failed(self, spine_run_text: str):
        """fix record 失败路径必须触发 fixer-failed trigger。"""
        assert "fixer-failed" in spine_run_text, (
            "spine-run.md 未包含 fixer-failed trigger"
        )

    def test_fix_record_failure_sets_fix_exhausted(self, spine_run_text: str):
        """fix record 失败路径必须设置 FIX_EXHAUSTED=true 并执行 break 2，
        确保不会继续进入下一轮 review。"""
        # FIX_EXHAUSTED=true 与 break 2 必须共存（fix record 失败路径的关键守卫）
        assert "FIX_EXHAUSTED=true" in spine_run_text, (
            "fix record 失败路径缺少 FIX_EXHAUSTED=true 标记"
        )
        assert "break 2" in spine_run_text, (
            "fix record 失败路径缺少 break 2（无法退出内外层循环）"
        )

    def test_fix_record_check_before_next_review(self, spine_run_text: str):
        """fix record 检查（FREC）必须出现在进入下一轮 npc review run 之前。

        顺序要求：FREC=$(npc fix record ...) 出现在后续 review run 之前。
        """
        frec_pos = spine_run_text.find("FREC=$(npc fix record")
        # 找到 fix record 后下一个 review run 的位置
        next_review_pos = spine_run_text.find("R=$(npc review run --seq $SEQ --round $N)")
        assert frec_pos != -1, "FREC 赋值语句未找到"
        assert next_review_pos != -1, "下一轮 review run 语句未找到"
        assert frec_pos < next_review_pos, (
            "fix record 检查必须出现在下一轮 review run 之前"
        )


# ============================================================
# Task 1.3 / Scenario: deferred=true 时 .ok 语义澄清
# ============================================================


class TestDeferredOkSemantics:
    """验证 spine-run.md 明确说明 deferred=true 时 .ok 的含义限制。"""

    def test_deferred_true_ok_semantics_documented(self, spine_run_text: str):
        """spine-run.md 必须说明 deferred=true 时 .ok 仅代表渲染成功。"""
        # 检测澄清文字的存在（至少包含关键语义）
        has_render_note = (
            "仅代表 prompt 渲染成功" in spine_run_text
            or "只代表渲染成功" in spine_run_text
            or "仅代表渲染" in spine_run_text
        )
        assert has_render_note, (
            "spine-run.md 缺少 deferred=true 时 .ok 语义说明"
            "（应明确：.ok 仅代表 prompt 渲染成功，不代表 coder 执行成功）"
        )

    def test_record_is_sole_truth_documented(self, spine_run_text: str):
        """spine-run.md 必须说明 record 返回值是 coder 成败的唯一真相。"""
        has_sole_truth = (
            "唯一真相" in spine_run_text
            or "coder 成败" in spine_run_text
        )
        assert has_sole_truth, (
            "spine-run.md 未说明 record 返回值是 coder 成败的唯一真相"
        )


# ============================================================
# Task 2.1 / Guardrails 增补
# ============================================================


class TestGuardrailsRecordCheck:
    """验证 Guardrails 一节包含 record 检查的硬约束条目。"""

    def test_guardrails_contains_record_check_constraint(self, spine_run_text: str):
        """Guardrails 必须包含 record 返回值检查的硬约束说明。"""
        # 检查 Guardrails 区块的关键词
        guardrails_start = spine_run_text.find("## Guardrails")
        assert guardrails_start != -1, "spine-run.md 缺少 Guardrails 一节"
        guardrails_text = spine_run_text[guardrails_start:]
        assert "record 返回值" in guardrails_text or "record return" in guardrails_text.lower(), (
            "Guardrails 未包含 record 返回值约束"
        )

    def test_guardrails_prohibits_review_after_record_failure(self, spine_run_text: str):
        """Guardrails 必须明确禁止 record 失败后继续 review/archive。"""
        guardrails_start = spine_run_text.find("## Guardrails")
        assert guardrails_start != -1
        guardrails_text = spine_run_text[guardrails_start:]
        assert "绝不继续" in guardrails_text or "不继续" in guardrails_text, (
            "Guardrails 未明确禁止 record 失败后继续 review/archive"
        )

    def test_guardrails_references_implementer_failed_trigger(self, spine_run_text: str):
        """Guardrails 必须引用 implementer-failed trigger 名称。"""
        guardrails_start = spine_run_text.find("## Guardrails")
        assert guardrails_start != -1
        guardrails_text = spine_run_text[guardrails_start:]
        assert "implementer-failed" in guardrails_text, (
            "Guardrails 未引用 implementer-failed trigger"
        )

    def test_guardrails_references_fixer_failed_trigger(self, spine_run_text: str):
        """Guardrails 必须引用 fixer-failed trigger 名称。"""
        guardrails_start = spine_run_text.find("## Guardrails")
        assert guardrails_start != -1
        guardrails_text = spine_run_text[guardrails_start:]
        assert "fixer-failed" in guardrails_text, (
            "Guardrails 未引用 fixer-failed trigger"
        )
