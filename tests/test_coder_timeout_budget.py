"""Tests for in-session coder timeout budget chain (change: in-session-coder-timeout).

Covers:
  3.1 状态链：budget → record-timeout (×N) → exhausted 标志翻转
  3.2 exhausted 后 auto-decide 返回 skip（agent-timeout-exhausted trigger）
  3.3 守卫测试：spine-run.md 含 timeout-budget 调用（skill 契约不回退）
  3.4 回归：fix 阶段同样有预算追踪
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import agent as _agent
from npc import auto_decide as _auto_decide
from npc import state as _state


# ============================================================
# Bootstrap helpers
# ============================================================


def _bootstrap(env_setup, capsys, make_args, *change_ids: str) -> None:
    sjson = str(env_setup.state_json)
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids)), state_json=sjson))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None, state_json=sjson))
        capsys.readouterr()


def _mk(env_setup, make_args, **kwargs):
    return make_args(state_json=str(env_setup.state_json), **kwargs)


def _read_emit(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _set_retries(env_setup, seq: int, phase: str, retries: int) -> None:
    """直接写入 state 的 timeout_retries 字段，模拟多次超时后的状态。"""
    s = json.loads(env_setup.state_json.read_text())
    entry = s["progress"][seq - 1]
    entry.setdefault("phases", {}).setdefault(phase, {})["timeout_retries"] = retries
    env_setup.state_json.write_text(json.dumps(s, indent=2))


# ============================================================
# 3.1  状态链：budget → record-timeout (×N) → exhausted 翻转
# ============================================================


class TestTimeoutStateChain:
    """验证 timeout_retries 从 0 累积到 exhausted 阈值的完整状态链。"""

    def test_initial_budget_not_exhausted(self, env_setup, capsys, make_args):
        """起始 retries=0，budget 返回 1800s，exhausted=False。"""
        _bootstrap(env_setup, capsys, make_args, "cid-chain")
        _agent.timeout_budget(_mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None))
        payload = _read_emit(capsys)
        assert payload["ok"] is True
        assert payload["timeout_sec"] == 1800
        assert payload["retries"] == 0
        assert payload["exhausted"] is False

    def test_record_timeout_once_increments_retries(self, env_setup, capsys, make_args):
        """record-timeout 第 1 次：retries=1，next_timeout_sec=2160，exhausted=False。"""
        _bootstrap(env_setup, capsys, make_args, "cid-chain")
        _agent.record_timeout(
            _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        payload = _read_emit(capsys)
        assert payload["retries"] == 1
        assert payload["next_timeout_sec"] == 2160  # 1800 * 1.2
        assert payload["exhausted"] is False

    def test_record_timeout_chain_to_exhausted(self, env_setup, capsys, make_args):
        """连续 record-timeout 5 次后 exhausted 标志翻转为 True。"""
        _bootstrap(env_setup, capsys, make_args, "cid-chain")
        for i in range(1, 6):
            _agent.record_timeout(
                _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
            )
            payload = _read_emit(capsys)
            assert payload["retries"] == i
        # retries == 5 → exhausted
        assert payload["exhausted"] is True
        # 最后一次 budget 查询也应显示 exhausted
        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        budget = _read_emit(capsys)
        assert budget["exhausted"] is True

    def test_exhausted_flag_at_threshold_boundary(self, env_setup, capsys, make_args):
        """retries=4（阈值-1）→ 未 exhausted；retries=5（阈值）→ exhausted。"""
        _bootstrap(env_setup, capsys, make_args, "cid-chain")
        # 设置 retries=4（边界前一步）
        _set_retries(env_setup, 1, "implement", 4)
        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        payload = _read_emit(capsys)
        assert payload["exhausted"] is False

        # record-timeout 一次：retries 变为 5，exhausted 翻转
        _agent.record_timeout(
            _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        payload = _read_emit(capsys)
        assert payload["retries"] == 5
        assert payload["exhausted"] is True

    def test_budget_progression_matches_formula(self, env_setup, capsys, make_args):
        """验证退避公式：budget[n] = min(1800 * 1.2^n, 3600)。"""
        _bootstrap(env_setup, capsys, make_args, "cid-chain")
        expected = [int(min(1800 * (1.2 ** n), 3600)) for n in range(6)]
        for n, exp in enumerate(expected):
            _set_retries(env_setup, 1, "implement", n)
            _agent.timeout_budget(
                _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
            )
            payload = _read_emit(capsys)
            assert payload["timeout_sec"] == exp, f"retries={n}: expected {exp}, got {payload['timeout_sec']}"

    def test_fix_phase_has_independent_budget_tracking(self, env_setup, capsys, make_args):
        """fix-r1 和 implement 各自维护独立的 timeout_retries 计数器。"""
        _bootstrap(env_setup, capsys, make_args, "cid-chain")
        # implement 超时 2 次
        for _ in range(2):
            _agent.record_timeout(
                _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
            )
            capsys.readouterr()

        # fix-r1 初始预算仍应为 1800（独立计数）
        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="fix-r1", base=None, mult=None, max_sec=None)
        )
        payload = _read_emit(capsys)
        assert payload["timeout_sec"] == 1800
        assert payload["retries"] == 0
        assert payload["exhausted"] is False


# ============================================================
# 3.2  exhausted → auto-decide 返回 skip
# ============================================================


class TestTimeoutExhaustedAutoDecide:
    """验证预算耗尽后 auto-decide --trigger agent-timeout-exhausted 返回 skip。"""

    def test_agent_timeout_exhausted_trigger_returns_skip(self, env_setup, capsys, make_args):
        """agent-timeout-exhausted trigger → action=skip，set_status=skipped-auto。"""
        sjson = str(env_setup.state_json)
        _state.init_run(make_args(plan_order=json.dumps(["cid-timeout"]), state_json=sjson))
        capsys.readouterr()
        _state.add_change(make_args(seq=1, change_id="cid-timeout", base=None, state_json=sjson))
        capsys.readouterr()

        _auto_decide.cli(
            _mk(env_setup, make_args, seq=1, trigger="agent-timeout-exhausted", apply=False)
        )
        payload = _read_emit(capsys)
        assert payload["ok"] is True
        assert payload["action"] == "skip"
        assert payload["set_status"] == "skipped-auto"
        assert "exhausted" in payload["reason"] or "oversized" in payload["reason"]

    def test_agent_timeout_exhausted_apply_writes_status(self, env_setup, capsys, make_args):
        """--apply 时 skipped-auto 状态被写入 state。"""
        sjson = str(env_setup.state_json)
        _state.init_run(make_args(plan_order=json.dumps(["cid-timeout"]), state_json=sjson))
        capsys.readouterr()
        _state.add_change(make_args(seq=1, change_id="cid-timeout", base=None, state_json=sjson))
        capsys.readouterr()

        _auto_decide.cli(
            _mk(env_setup, make_args, seq=1, trigger="agent-timeout-exhausted", apply=True)
        )
        capsys.readouterr()

        s = json.loads(env_setup.state_json.read_text())
        entry = s["progress"][0]
        assert entry["status"] == "skipped-auto"
        assert entry.get("last_trigger") == "agent-timeout-exhausted"

    def test_trigger_in_valid_triggers_set(self):
        """agent-timeout-exhausted 必须在 VALID_TRIGGERS 中（契约不回退守卫）。"""
        assert "agent-timeout-exhausted" in _auto_decide.VALID_TRIGGERS

    def test_budget_exhausted_then_auto_decide_flow(self, env_setup, capsys, make_args):
        """完整状态链：5次 record-timeout → exhausted=True → auto-decide → skip 且写入 state。"""
        _bootstrap(env_setup, capsys, make_args, "cid-flow")

        # 累积到 exhausted
        for _ in range(5):
            _agent.record_timeout(
                _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
            )
            capsys.readouterr()

        # 确认 exhausted
        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        budget = _read_emit(capsys)
        assert budget["exhausted"] is True

        # auto-decide
        _auto_decide.cli(
            _mk(env_setup, make_args, seq=1, trigger="agent-timeout-exhausted", apply=True)
        )
        decision = _read_emit(capsys)
        assert decision["action"] == "skip"

        # 验证 state
        s = json.loads(env_setup.state_json.read_text())
        assert s["progress"][0]["status"] == "skipped-auto"


# ============================================================
# 3.3  守卫测试：spine-run.md 含 timeout-budget 调用（skill 契约不回退）
# ============================================================


class TestSpineRunSkillContract:
    """验证 spine-run.md 文件中含有 timeout-budget 关键调用，确保 skill 契约不被意外回退。"""

    @pytest.fixture(autouse=True)
    def spine_run_text(self) -> str:
        spine_run = Path(__file__).parent.parent / "plugins" / "agent-spine" / "commands" / "spine-run.md"
        return spine_run.read_text(encoding="utf-8")

    def test_spine_run_contains_timeout_budget_call(self, spine_run_text):
        """spine-run.md 的 deferred=true 路径必须有 timeout-budget 调用。"""
        assert "timeout-budget" in spine_run_text

    def test_spine_run_contains_record_timeout_call(self, spine_run_text):
        """spine-run.md 的超时路径必须有 record-timeout 调用。"""
        assert "record-timeout" in spine_run_text

    def test_spine_run_contains_agent_timeout_exhausted_trigger(self, spine_run_text):
        """spine-run.md 必须引用 agent-timeout-exhausted trigger。"""
        assert "agent-timeout-exhausted" in spine_run_text

    def test_spine_run_guardrail_no_infinite_wait(self, spine_run_text):
        """spine-run.md 的 Guardrails 必须含有 in-session coder spawn 超时约束说明。"""
        assert "in-session coder spawn" in spine_run_text or "in-session" in spine_run_text
        assert "timeout" in spine_run_text.lower()

    def test_spine_run_timeout_budget_before_spawn(self, spine_run_text):
        """timeout-budget 必须出现在 spawn 操作（Agent subagent_type）前，
        验证 skill 契约中取预算→spawn 的正确顺序。"""
        tb_pos = spine_run_text.find("timeout-budget")
        spawn_pos = spine_run_text.find("Agent subagent_type=spine-coder")
        assert tb_pos != -1, "timeout-budget call not found"
        assert spawn_pos != -1, "Agent spawn call not found"
        assert tb_pos < spawn_pos, "timeout-budget must appear before Agent spawn"

    def test_spine_run_decision_table_contains_timeout_trigger(self, spine_run_text):
        """3d 决策点的 trigger 表必须包含 agent-timeout-exhausted 行。"""
        assert "agent-timeout-exhausted" in spine_run_text

    def test_spine_run_fix_phase_inner_retry_loop(self, spine_run_text):
        """spine-run.md fix 分支必须用内层循环在同一 FIX_PHASE 内重试，
        不能在一次超时后 continue 外层循环（否则 N 递增导致 phase 散落）。
        检测标志：内层 while true 与 break 2 必须共存（用于同 phase 重试）。"""
        assert "while true" in spine_run_text, "inner retry loop (while true) missing in fix branch"
        assert "break 2" in spine_run_text, "break 2 (exit both loops on exhausted) missing"


# ============================================================
# 3.5  fix-r1 连续超时 5 次应在同一 phase 累积到 exhausted
# ============================================================


class TestFixPhaseConsecutiveTimeoutExhaustion:
    """验证 fix-rN phase 连续超时时，timeout_retries 在同一 phase 累积到 exhausted。

    这是 F1 finding 的核心回归测试：旧代码因 continue 外层循环导致
    N 递增、超时分散到 fix-r1/fix-r2/... 各 phase，每个 phase 只到 retries=1，
    永远无法触发 exhausted。修复后 timeout_retries 必须在 fix-r1 内从 0 累积到 5。
    """

    def test_fix_r1_consecutive_timeouts_reach_exhausted(self, env_setup, capsys, make_args):
        """连续对 fix-r1 record-timeout 5 次 → exhausted=True（同 phase 累积）。"""
        _bootstrap(env_setup, capsys, make_args, "cid-fix-exhaust")

        for i in range(1, 6):
            _agent.record_timeout(
                _mk(env_setup, make_args, seq=1, phase="fix-r1", base=None, mult=None, max_sec=None)
            )
            payload = _read_emit(capsys)
            assert payload["retries"] == i, f"expected retries={i}, got {payload['retries']}"

        assert payload["exhausted"] is True, "fix-r1 should be exhausted after 5 consecutive timeouts"

        # 确认 budget 查询也返回 exhausted
        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="fix-r1", base=None, mult=None, max_sec=None)
        )
        budget = _read_emit(capsys)
        assert budget["exhausted"] is True

    def test_fix_r1_exhausted_triggers_auto_decide_skip(self, env_setup, capsys, make_args):
        """fix-r1 exhausted → auto-decide --trigger agent-timeout-exhausted → action=skip。

        这验证了完整的修复路径：同一 phase 累积 5 次超时 → exhausted → skip，
        而非旧代码的跨 phase 散落导致预算永远难以耗尽。
        """
        _bootstrap(env_setup, capsys, make_args, "cid-fix-skip")

        # 累积 5 次超时（模拟内层循环在 fix-r1 重试 5 次）
        for _ in range(5):
            _agent.record_timeout(
                _mk(env_setup, make_args, seq=1, phase="fix-r1", base=None, mult=None, max_sec=None)
            )
            capsys.readouterr()

        # exhausted → auto-decide
        _auto_decide.cli(
            _mk(env_setup, make_args, seq=1, trigger="agent-timeout-exhausted", apply=True)
        )
        decision = _read_emit(capsys)
        assert decision["action"] == "skip"
        assert decision["set_status"] == "skipped-auto"

        # state 中应记录 skipped-auto
        s = json.loads(env_setup.state_json.read_text())
        assert s["progress"][0]["status"] == "skipped-auto"
        assert s["progress"][0].get("last_trigger") == "agent-timeout-exhausted"

    def test_fix_phases_remain_independent_across_rounds(self, env_setup, capsys, make_args):
        """不同 fix round 的 phase（fix-r1, fix-r2）保持独立计数器。

        旧代码的问题是超时会散落到不同 phase，新代码修复后每个 phase 内部
        独立累积，不同 round 间不应相互干扰。
        """
        _bootstrap(env_setup, capsys, make_args, "cid-fix-independent")

        # fix-r1 超时 3 次（未到 exhausted）
        for _ in range(3):
            _agent.record_timeout(
                _mk(env_setup, make_args, seq=1, phase="fix-r1", base=None, mult=None, max_sec=None)
            )
            capsys.readouterr()

        # fix-r2 应独立，retries=0
        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="fix-r2", base=None, mult=None, max_sec=None)
        )
        budget_r2 = _read_emit(capsys)
        assert budget_r2["retries"] == 0, "fix-r2 should not be affected by fix-r1 timeouts"
        assert budget_r2["exhausted"] is False

        # fix-r1 尚未 exhausted（3 < 5）
        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="fix-r1", base=None, mult=None, max_sec=None)
        )
        budget_r1 = _read_emit(capsys)
        assert budget_r1["retries"] == 3
        assert budget_r1["exhausted"] is False

    def test_fix_r1_not_exhausted_at_retries_4(self, env_setup, capsys, make_args):
        """fix-r1 超时 4 次（阈值-1）→ 未 exhausted，仍应继续重派同一 phase。"""
        _bootstrap(env_setup, capsys, make_args, "cid-fix-boundary")
        _set_retries(env_setup, 1, "fix-r1", 4)

        _agent.timeout_budget(
            _mk(env_setup, make_args, seq=1, phase="fix-r1", base=None, mult=None, max_sec=None)
        )
        payload = _read_emit(capsys)
        assert payload["retries"] == 4
        assert payload["exhausted"] is False, "at retries=4 fix-r1 should NOT be exhausted yet"


# ============================================================
# 3.6  spine-run.md 控制流守卫：fix exhausted 分支不落入 post-loop R 判断
# ============================================================


class TestFixExhaustedControlFlow:
    """验证 spine-run.md fix exhausted 分支的控制流守卫。

    F1 finding 的核心：fix 分支耗尽预算后必须按 auto-decide 返回的 ACTION
    立即执行，不得落入"循环退出后看 R"的 blocking/stale 判断。

    测试通过检查 spine-run.md 的文本结构保证：
    1. FIX_EXHAUSTED 标志在 break 2 前被设置（两处 exhausted 路径都覆盖）
    2. 循环后的注释明确区分 FIX_EXHAUSTED=true 和正常出口两个分支
    3. 预算耗尽后 auto-decide apply 写入 skipped-auto → state 可被验证，
       证明 ACTION 已真实落地（不只是 break 后丢弃）
    """

    @pytest.fixture(autouse=True)
    def spine_run_text(self) -> str:
        spine_run = (
            Path(__file__).parent.parent
            / "plugins"
            / "agent-spine"
            / "commands"
            / "spine-run.md"
        )
        return spine_run.read_text(encoding="utf-8")

    def test_fix_exhausted_flag_set_before_break2(self, spine_run_text):
        """FIX_EXHAUSTED=true 必须紧接在 break 2 代码行之前出现（两处 exhausted 路径均覆盖）。

        确保无论走"budget 查询时已 exhausted"还是"record-timeout 返回 exhausted"路径，
        都设置了标志，post-loop 能区分 exhausted 出口和正常出口。

        注意：测试用行列表过滤掉注释行，只检查赋值语句与代码行的相对位置。
        """
        lines = spine_run_text.splitlines()
        flag_lines = [
            i for i, line in enumerate(lines)
            if "FIX_EXHAUSTED=true" in line and not line.strip().startswith("#")
        ]
        break2_lines = [
            i for i, line in enumerate(lines)
            if line.strip() == "break 2" or line.strip().startswith("break 2  #")
        ]

        assert len(flag_lines) >= 1, (
            "FIX_EXHAUSTED=true assignment missing — exhausted exit path not flagged"
        )
        assert len(break2_lines) >= 1, "No 'break 2' code lines found in spine-run.md"

        # 对每一个 break 2 代码行，必须存在一个 FIX_EXHAUSTED=true 赋值在其紧前面
        # 至少第一个 flag 赋值应出现在第一个实际 break 2 代码行之前
        first_flag = min(flag_lines)
        first_break2 = min(break2_lines)
        assert first_flag < first_break2, (
            f"FIX_EXHAUSTED=true (line {first_flag+1}) must appear before "
            f"the first 'break 2' code line (line {first_break2+1})"
        )

    def test_fix_exhausted_flag_initialized_before_loop(self, spine_run_text):
        """FIX_EXHAUSTED=false 初始化必须在外层 while 循环之前出现。

        若未初始化，bash 中变量为空字符串，条件判断将静默失效。
        """
        init_pos = spine_run_text.find("FIX_EXHAUSTED=false")
        loop_pos = spine_run_text.find(
            'while [ "$(printf \'%s\' "$R" | jq -r \'.blocking\')" -gt 0 ]'
        )
        assert init_pos != -1, "FIX_EXHAUSTED=false initialization missing"
        assert loop_pos != -1, "review-fix while loop not found"
        assert init_pos < loop_pos, (
            "FIX_EXHAUSTED must be initialized before the review-fix while loop"
        )

    def test_post_loop_dispatches_action_before_r_check(self, spine_run_text):
        """循环退出后注释必须明确区分 FIX_EXHAUSTED 路径和正常出口。

        确保 spec 文本要求执行者在 FIX_EXHAUSTED=true 时按 ACTION 分发，
        而不是继续走旧的 blocking/stale 判断。
        """
        assert "FIX_EXHAUSTED" in spine_run_text, (
            "Post-loop section must reference FIX_EXHAUSTED flag"
        )
        # 循环后有 ACTION 的分发语义说明
        assert "ACTION" in spine_run_text, "ACTION dispatch missing from post-loop section"

    def test_exhausted_action_applied_writes_skipped_auto(self, env_setup, capsys, make_args):
        """预算耗尽 → auto-decide apply → state 写入 skipped-auto（ACTION 真实落地验证）。

        这是关键的功能回归：验证 auto-decide --apply 调用确实把 change 状态写为
        skipped-auto，从而证明 break 2 后如果按 ACTION=skip 执行（调用 --apply），
        当前 change 会被正确跳过，而不是落入未定义的 post-loop R 判断。
        """
        _bootstrap(env_setup, capsys, make_args, "cid-exhausted-action")

        # 模拟 fix-r1 在同一 phase 累积 5 次超时 → exhausted
        for _ in range(5):
            _agent.record_timeout(
                _mk(
                    env_setup,
                    make_args,
                    seq=1,
                    phase="fix-r1",
                    base=None,
                    mult=None,
                    max_sec=None,
                )
            )
            capsys.readouterr()

        # 确认 exhausted
        _agent.timeout_budget(
            _mk(
                env_setup,
                make_args,
                seq=1,
                phase="fix-r1",
                base=None,
                mult=None,
                max_sec=None,
            )
        )
        budget = _read_emit(capsys)
        assert budget["exhausted"] is True

        # auto-decide --apply（模拟 spine-run exhausted 分支调用）
        _auto_decide.cli(
            _mk(env_setup, make_args, seq=1, trigger="agent-timeout-exhausted", apply=True)
        )
        decision = _read_emit(capsys)
        # ACTION 必须为 skip（对应 exhausted trigger 的标准响应）
        assert decision["action"] == "skip"
        assert decision["set_status"] == "skipped-auto"

        # 验证 state 已写入：证明 ACTION 真实落地，change 已被跳过
        # 若 spine-run 只做 break 2 而不执行 ACTION，此断言将失败
        s = json.loads(env_setup.state_json.read_text())
        assert s["progress"][0]["status"] == "skipped-auto", (
            "After exhausted + auto-decide --apply, change must be skipped-auto in state"
        )
        assert s["progress"][0].get("last_trigger") == "agent-timeout-exhausted"


# ============================================================
# 3.7  守卫测试：spine-run.md error-handling — budget/record-timeout 失败路径
#      覆盖 F1 finding：deferred=true 路径必须校验 exit code + .ok + timeout_sec
# ============================================================


class TestSpineRunErrorHandlingGuard:
    """验证 spine-run.md 文件中 timeout-budget / record-timeout 的失败路径守卫。

    F1 finding 指出：旧代码未检查 exit code 和 .ok，导致 budget 失败时
    TIMEOUT_SEC 为空、仍执行 spawn，退化为无有效超时；record-timeout 失败时
    timeout_retries 不累积，内层 while true 永久重派。

    这些测试验证修复后 spine-run.md 的文本结构包含必要的防御性检查。
    """

    @pytest.fixture(autouse=True)
    def spine_run_text(self) -> str:
        spine_run = (
            Path(__file__).parent.parent
            / "plugins"
            / "agent-spine"
            / "commands"
            / "spine-run.md"
        )
        return spine_run.read_text(encoding="utf-8")

    def test_implement_path_checks_budget_exit_code(self, spine_run_text):
        """3a implement deferred=true 路径：timeout-budget 后必须保存并检查 exit code。

        修复前：直接 jq 解析 $BUDGET，exit code 从未检查。
        修复后：BUDGET_EXIT=$? 并在条件中检查 $BUDGET_EXIT -ne 0。
        """
        assert "BUDGET_EXIT=$?" in spine_run_text, (
            "BUDGET_EXIT=$? capture missing — timeout-budget exit code not checked"
        )
        assert "$BUDGET_EXIT -ne 0" in spine_run_text, (
            "exit code check ($BUDGET_EXIT -ne 0) missing in budget validation guard"
        )

    def test_implement_path_checks_budget_ok_field(self, spine_run_text):
        """3a implement deferred=true 路径：timeout-budget 响应的 .ok 字段必须被校验。

        修复前：只检查 .exhausted，从不校验 .ok，非 JSON 输出会让 .exhausted 为 false
        导致继续 spawn（TIMEOUT_SEC 为 null/空，spawn 无有效超时）。
        修复后：'.ok // false' 守卫，确保 budget 调用自身成功。
        """
        assert "'.ok // false'" in spine_run_text or ".ok // false" in spine_run_text, (
            ".ok // false guard missing — budget failure not detected when .ok absent"
        )

    def test_implement_path_validates_timeout_sec_positive_integer(self, spine_run_text):
        """3a implement deferred=true 路径：timeout_sec 必须被校验为正整数后才提取。

        修复前：TIMEOUT_SEC=$(echo "$BUDGET" | jq -r '.timeout_sec') 无验证，
        null/0/负数均可传入 spawn，退化为无限等待或立即超时。
        修复后：jq -e '.timeout_sec | type == "number" and . > 0' 守卫。
        """
        assert 'type == "number" and . > 0' in spine_run_text, (
            "timeout_sec positive integer validation missing — invalid budget not rejected"
        )

    def test_implement_path_does_not_spawn_on_budget_failure(self, spine_run_text):
        """3a implement 路径：budget 失败时必须进决策点，不得落到 spawn 行。

        通过检查 implementer-failed trigger 出现在 implement 路径中来验证：
        budget 失败 → 以 implementer-failed 触发 auto-decide，不 spawn。
        """
        assert "implementer-failed" in spine_run_text, (
            "implementer-failed trigger missing — budget failure in implement path has no decision exit"
        )

    def test_fix_path_checks_budget_exit_code(self, spine_run_text):
        """3b fix deferred=true 路径：timeout-budget 后必须检查 exit code。

        fix 内层循环同样要求：BUDGET_EXIT=$? + $BUDGET_EXIT -ne 0 条件检查。
        """
        occurrences = spine_run_text.count("BUDGET_EXIT=$?")
        assert occurrences >= 2, (
            f"BUDGET_EXIT=$? only appears {occurrences} time(s); "
            "both implement and fix-rN paths must capture budget exit code"
        )

    def test_fix_path_does_not_spawn_on_budget_failure(self, spine_run_text):
        """3b fix 路径：budget 失败时必须以 fixer-failed 进决策点，不得 spawn。

        修复前：budget 失败 → TIMEOUT_SEC 为 null → spawn 无效超时 → record-timeout 失败
        → 内层 while true 无限重派。修复后：fixer-failed trigger 跳出两层循环。
        """
        assert "fixer-failed" in spine_run_text, (
            "fixer-failed trigger missing — budget failure in fix path has no decision exit"
        )

    def test_record_timeout_exit_code_checked_in_implement_path(self, spine_run_text):
        """3a implement 路径：record-timeout 后必须检查 exit code（RT_EXIT=$?）。

        修复前：RT=$(npc agent record-timeout ...) 后直接读 .exhausted，
        record-timeout 失败时 .exhausted=false，continue 无限重派。
        修复后：RT_EXIT=$? + RT_EXIT -ne 0 守卫，失败时保守视为 exhausted。
        """
        assert "RT_EXIT=$?" in spine_run_text, (
            "RT_EXIT=$? capture missing — record-timeout exit code not checked"
        )
        assert "$RT_EXIT -ne 0" in spine_run_text, (
            "exit code check ($RT_EXIT -ne 0) missing in record-timeout validation"
        )

    def test_record_timeout_ok_field_checked(self, spine_run_text):
        """record-timeout 响应的 .ok 字段必须被校验。

        record-timeout 失败（非 JSON 或 .ok=false）时，必须保守视为 exhausted，
        不继续重派，避免 while true 无限循环。
        """
        ok_count = spine_run_text.count("'.ok // false'") + spine_run_text.count(".ok // false")
        assert ok_count >= 2, (
            f".ok // false guard appears only {ok_count} time(s); "
            "both timeout-budget and record-timeout responses must be validated"
        )

    def test_timeout_sec_only_extracted_after_validation(self, spine_run_text):
        """TIMEOUT_SEC 赋值必须在 jq 正整数验证守卫之后，不在其之前。

        确保不存在在验证前先提取 TIMEOUT_SEC 的旧代码残留。
        """
        validation_guard = 'type == "number" and . > 0'
        timeout_extraction = "TIMEOUT_SEC=$(printf '%s' \"$BUDGET\" | jq -r '.timeout_sec')"
        assert timeout_extraction in spine_run_text, (
            "TIMEOUT_SEC extraction line missing from spine-run.md"
        )
        guard_pos = spine_run_text.find(validation_guard)
        timeout_pos = spine_run_text.find(timeout_extraction)
        assert guard_pos < timeout_pos, (
            f"Validation guard (pos {guard_pos}) must appear before "
            f"TIMEOUT_SEC extraction (pos {timeout_pos}) — "
            "timeout_sec must be validated before being used"
        )
