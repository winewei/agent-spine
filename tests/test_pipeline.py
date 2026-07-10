"""pipeline 模块测试：review run / archive run / implement_record / fix_record。

codex / openspec 通过 monkeypatch 替换 subprocess.run；git 命令使用真实 fake_repo。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from npc import pipeline as _pipeline
from npc import paths as _paths
from npc import state as _state
from npc import verify as _verify
from npc import config as _config


# ============================================================
# Helpers
# ============================================================


def _bootstrap_run(env_setup, make_args, capsys, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _stub_codex_writes_review(review_payload: dict):
    """构造一个 fake codex 调用：写好 review.json 并返回 exit 0。"""

    def fake_codex_exec(
        *, repo_root, schema_path, focus_text, review_out, events_out, timeout_sec,
        codex_bin, portable_timeout,
    ):
        review_out.parent.mkdir(parents=True, exist_ok=True)
        review_out.write_text(json.dumps(review_payload), encoding="utf-8")
        events_out.parent.mkdir(parents=True, exist_ok=True)
        events_out.write_text("fake events line\n", encoding="utf-8")
        return 0

    return fake_codex_exec


# ============================================================
# RESULT 行解析
# ============================================================


def test_parse_result_line_basic():
    line = "RESULT: commit=abc123 tasks=5 tests=pass summary=/tmp/x.md notes=ok"
    out = _pipeline._parse_result_line(line)
    assert out["commit"] == "abc123"
    assert out["tasks"] == "5"
    assert out["tests"] == "pass"


def test_parse_result_line_value_with_spaces_until_next_key():
    line = "stuff before\nRESULT: commit=- tests=fail notes=this is a multi word note"
    out = _pipeline._parse_result_line(line)
    assert out["commit"] == "-"
    assert out["notes"] == "this is a multi word note"


def test_parse_result_line_missing():
    assert _pipeline._parse_result_line("nothing here") is None


# ============================================================
# RESULT 行解析 + 校验（R2 round 2：解析器级必需键校验）
# ============================================================


def test_parse_and_validate_result_line_missing_key_surfaces_failure():
    """R2 finding 复现用例：implement RESULT 行缺 `tasks` 时，解析器级校验
    必须直接报出缺失键，而不是把 parsed 字典（缺了 tasks）原样交还给调用方。
    """
    line = "RESULT: commit=abc tests=pass summary=/tmp/s.md"
    parsed, missing = _pipeline._parse_and_validate_result_line(line, "implement")
    assert parsed is not None
    assert "tasks" not in parsed
    assert missing == ["tasks"]


def test_parse_and_validate_result_line_all_keys_present_no_missing():
    line = "RESULT: commit=abc tasks=3 tests=pass summary=/tmp/s.md notes=-"
    parsed, missing = _pipeline._parse_and_validate_result_line(line, "implement")
    assert parsed is not None
    assert missing == []


def test_parse_and_validate_result_line_missing_result_returns_none():
    parsed, missing = _pipeline._parse_and_validate_result_line("nothing here", "implement")
    assert parsed is None
    assert missing == []


def test_parse_and_validate_result_line_fix_missing_multiple_keys():
    line = "RESULT: commit=abc fixed=2 tests=pass summary=/tmp/s.md"
    parsed, missing = _pipeline._parse_and_validate_result_line(line, "fix")
    assert parsed is not None
    assert missing == ["categories_scanned", "regressions_added"]


def test_parse_and_validate_result_line_failure_schema_uses_failure_keys():
    """commit=- 且 tests=fail 时应切到 RESULT_REQUIRED_KEYS['failure']，
    tasks/fixed 等 phase 专属字段不应被误判为缺失。"""
    line = "RESULT: commit=- tests=fail summary=/tmp/s.md notes=boom"
    parsed, missing = _pipeline._parse_and_validate_result_line(line, "implement")
    assert parsed is not None
    assert missing == []


# ============================================================
# review run
# ============================================================


def test_run_review_round_success(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # stub codex
    review_payload = {
        "verdict": "approve",
        "findings": [],
    }
    monkeypatch.setattr(_pipeline, "_codex_exec", _stub_codex_writes_review(review_payload))
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/portable-timeout")
    )

    # repo_root 替换为 fake_repo
    p = env_setup
    p_with_repo = type(p)(
        repo_root=fake_repo,
        proj_key=p.proj_key,
        task_log_dir=p.task_log_dir,
        run_ts=p.run_ts,
        run_dir=p.run_dir,
        state_json=p.state_json,
        state_md=p.state_md,
        index_file=p.index_file,
        schema_path=p.schema_path,
        run_events=p.run_events,
    )

    result = _pipeline.run_review_round(p_with_repo, 1, 0)
    assert result["ok"] is True
    assert result["verdict"] == "approve"
    assert result["blocking"] == 0
    assert result["stale"] is False
    assert Path(result["review_json"]).is_file()

    # 状态：phase exit done + trend 已更新
    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["review-r0"]["status"] == "done"
    assert entry["blocking_trend"] == [0]


def test_run_review_round_with_blocking_renders_findings(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    review_payload = {
        "verdict": "changes-requested",
        "findings": [
            {
                "id": "F1",
                "severity": "high",
                "category": "validation",
                "title": "missing check",
                "file": "a.py",
                "line_range": "10-20",
                "detail": "no validation",
                "recommendation": "add check",
                "in_scope": True,
                "spec_attribution": "impl-deviation",
            }
        ],
    }
    monkeypatch.setattr(_pipeline, "_codex_exec", _stub_codex_writes_review(review_payload))
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/portable-timeout")
    )

    p = env_setup
    p_with_repo = type(p)(
        **{**p.__dict__, "repo_root": fake_repo}  # type: ignore[arg-type]
    )

    result = _pipeline.run_review_round(p_with_repo, 1, 0)
    assert result["ok"] is True
    assert result["blocking"] == 1
    assert result["verdict"] == "changes-requested"
    assert result["findings_path"] is not None
    assert Path(result["findings_path"]).is_file()
    assert "F1" in Path(result["findings_path"]).read_text()


def test_run_review_round_codex_fails_then_retry_fails(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    calls = []

    def fail_codex(**kwargs):
        calls.append(1)
        return 1  # 非 0 exit

    monkeypatch.setattr(_pipeline, "_codex_exec", fail_codex)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/portable-timeout")
    )

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    result = _pipeline.run_review_round(p_with_repo, 1, 0, retries=1)
    assert result["ok"] is False
    assert result["error"] == "codex-exec-failed"
    assert len(calls) == 2  # 重试 1 次共 2 次

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["phases"]["review-r0"]["status"] == "failed"


def _stub_claude_writes_review(review_payload: dict):
    """fake claude -p：把 review_payload 写到 review_out 并返回 0。"""

    def fake_claude_exec(
        *, repo_root, schema_path, focus_text, review_out, events_out, timeout_sec,
        claude_bin, portable_timeout, model=None, extra_args=(),
    ):
        review_out.parent.mkdir(parents=True, exist_ok=True)
        review_out.write_text(json.dumps(review_payload), encoding="utf-8")
        events_out.parent.mkdir(parents=True, exist_ok=True)
        events_out.write_text(
            f"# fake claude\n# model={model}\n# extra_args={extra_args}\n",
            encoding="utf-8",
        )
        return 0

    return fake_claude_exec


def test_run_review_round_uses_claude_when_engine_name_override(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """显式 engine_name='claude' 应路由到 _claude_exec，不调 _codex_exec。

    注：需提供 coder model 与 review.claude.model 不同的配置，确保不触发
    gen_not_orthogonal（coder=claude-sonnet vs review=claude-opus → 不同源）。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # coder 与 review 用不同 model → 不同源，不触发 gen_not_orthogonal
    cfg_path = tmp_path / "npc-diff-model.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "claude"\nmodel = "claude-sonnet-4-5"\n'
        '[review]\nengine = "codex"\n[review.claude]\nmodel = "claude-opus-4-8"\n',
        encoding="utf-8",
    )

    review_payload = {"verdict": "approve", "findings": []}

    codex_calls = []

    def codex_should_not_run(**kwargs):
        codex_calls.append(1)
        return 0

    monkeypatch.setattr(_pipeline, "_codex_exec", codex_should_not_run)
    monkeypatch.setattr(
        _pipeline, "_claude_exec", _stub_claude_writes_review(review_payload)
    )
    monkeypatch.setattr(
        _pipeline, "_find_claude_bin", lambda override=None: "/fake/claude"
    )
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt")
    )

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    result = _pipeline.run_review_round(p_with_repo, 1, 0, engine_name="claude", config_path=cfg_path)
    assert result["ok"] is True
    assert result["engine"] == "claude"
    assert result["verdict"] == "approve"
    assert codex_calls == []  # codex 没被调


def test_run_review_round_claude_from_config_toml(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """配置文件 [review].engine = claude → 默认走 claude 引擎。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    cfg_path = tmp_path / "npc-cfg.toml"
    cfg_path.write_text(
        '[review]\nengine = "claude"\n[review.claude]\nmodel = "claude-opus-4-7"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        _pipeline,
        "_claude_exec",
        _stub_claude_writes_review({"verdict": "approve", "findings": []}),
    )
    monkeypatch.setattr(
        _pipeline, "_find_claude_bin", lambda override=None: "/fake/claude"
    )
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt")
    )

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    result = _pipeline.run_review_round(p_with_repo, 1, 0, config_path=cfg_path)
    assert result["ok"] is True
    assert result["engine"] == "claude"


def test_run_review_round_claude_fails_then_retry_fails(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # coder 与 review 用不同 model → 不同源，不触发 gen_not_orthogonal
    cfg_path = tmp_path / "npc-diff-model.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "claude"\nmodel = "claude-sonnet-4-5"\n'
        '[review]\nengine = "codex"\n[review.claude]\nmodel = "claude-opus-4-8"\n',
        encoding="utf-8",
    )

    calls = []

    def fail_claude(**kwargs):
        calls.append(1)
        return 65  # JSON 提取失败

    monkeypatch.setattr(_pipeline, "_claude_exec", fail_claude)
    monkeypatch.setattr(
        _pipeline, "_find_claude_bin", lambda override=None: "/fake/claude"
    )
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt")
    )

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    result = _pipeline.run_review_round(
        p_with_repo, 1, 0, retries=1, engine_name="claude", config_path=cfg_path
    )
    assert result["ok"] is False
    assert result["error"] == "claude-exec-failed"
    assert result["engine"] == "claude"
    assert len(calls) == 2


def test_run_review_round_rejects_unknown_engine(
    env_setup, make_args, capsys, fake_repo: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    with pytest.raises(ValueError, match="未知 review engine"):
        _pipeline.run_review_round(p_with_repo, 1, 0, engine_name="gemini")


# ============================================================
# round-0 双 pass 对抗式评审（change review-r0-adversarial-pass）
# ============================================================


def _stub_dispatch_by_path(pass1_payload: dict, pass2_payload: dict | None = None,
                           pass2_fail: bool = False):
    """按 review_out 文件名分派 pass1 / pass2 的 fake codex 调用。返回 (fake, calls)。"""
    calls = {"count": 0, "names": []}

    def fake(*, repo_root, schema_path, focus_text, review_out, events_out,
             timeout_sec, codex_bin, portable_timeout):
        calls["count"] += 1
        calls["names"].append(review_out.name)
        events_out.parent.mkdir(parents=True, exist_ok=True)
        events_out.write_text("ev\n", encoding="utf-8")
        if "pass2.adversarial" in review_out.name:
            if pass2_fail:
                return 1
            review_out.write_text(json.dumps(pass2_payload), encoding="utf-8")
            return 0
        review_out.write_text(json.dumps(pass1_payload), encoding="utf-8")
        return 0

    return fake, calls


def _capture_emit_review_round(monkeypatch):
    captured: list[dict] = []
    real = _pipeline._telemetry.emit_review_round

    def wrapper(**kwargs):
        captured.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(_pipeline._telemetry, "emit_review_round", wrapper)
    return captured


def _adv_finding(sev="high", cat="concurrency", file="b.py", line="5"):
    return {
        "id": "F1", "severity": sev, "category": cat, "title": "t",
        "file": file, "line_range": line, "detail": "d", "recommendation": "r",
        "in_scope": True, "spec_attribution": "spec-silent",
    }


def _set_implement_commit(state_json: Path, commit: str = "deadbeef") -> None:
    s = json.loads(state_json.read_text())
    entry = s["progress"][0]
    entry.setdefault("phases", {})["implement"] = {"commit": commit}
    state_json.write_text(json.dumps(s), encoding="utf-8")


def test_run_review_round0_double_pass_success(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """情形 1：双 pass 成功 → 合并、adversarial_pass_ran=True、count=pass2 blocking 数。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    fake, calls = _stub_dispatch_by_path(
        {"verdict": "approve", "findings": []},
        {"verdict": "changes-requested", "findings": [_adv_finding()]},
    )
    monkeypatch.setattr(_pipeline, "_codex_exec", fake)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(_pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt"))
    captured = _capture_emit_review_round(monkeypatch)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result = _pipeline.run_review_round(p_with_repo, 1, 0)

    assert result["ok"] is True
    assert result["blocking"] == 1
    assert result["verdict"] == "changes-requested"
    assert calls["count"] == 2
    base = Path(result["review_json"]).parent
    assert (base / "round-0.review.pass1.json").is_file()
    assert (base / "round-0.review.pass2.adversarial.json").is_file()
    assert (base / "round-0.adversarial.focus.md").is_file()
    merged = json.loads((base / "round-0.review.json").read_text())
    assert len(merged["findings"]) == 1
    # telemetry
    assert captured[-1]["adversarial_pass_ran"] is True
    assert captured[-1]["adversarial_blocking_count"] == 1


def test_run_review_round0_pass2_fail_degrades(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """情形 2：pass2 重试耗尽 → 降级 pass1-only、ok=True、adversarial_pass_ran=False/count=None。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    fake, calls = _stub_dispatch_by_path(
        {"verdict": "changes-requested", "findings": [_adv_finding(file="a.py", cat="validation")]},
        pass2_fail=True,
    )
    monkeypatch.setattr(_pipeline, "_codex_exec", fake)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(_pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt"))
    captured = _capture_emit_review_round(monkeypatch)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result = _pipeline.run_review_round(p_with_repo, 1, 0, retries=1)

    assert result["ok"] is True
    assert result["blocking"] == 1  # 等价 pass1-only
    base = Path(result["review_json"]).parent
    # pass2 失败未落盘
    assert not (base / "round-0.review.pass2.adversarial.json").exists()
    merged = json.loads((base / "round-0.review.json").read_text())
    assert len(merged["findings"]) == 1
    assert captured[-1]["adversarial_pass_ran"] is False
    assert captured[-1]["adversarial_blocking_count"] is None


def test_run_review_round0_pass1_fail_no_pass2(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """情形 3：pass1 失败 → 整轮失败、pass2 不执行、adversarial_pass_ran=False/count=None。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    names: list[str] = []

    def fail_pass1(**kwargs):
        names.append(kwargs["review_out"].name)
        return 1

    monkeypatch.setattr(_pipeline, "_codex_exec", fail_pass1)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(_pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt"))
    captured = _capture_emit_review_round(monkeypatch)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result = _pipeline.run_review_round(p_with_repo, 1, 0, retries=1)

    assert result["ok"] is False
    assert all("pass2.adversarial" not in n for n in names)  # pass2 从未执行
    assert captured[-1]["adversarial_pass_ran"] is False
    assert captured[-1]["adversarial_blocking_count"] is None


def test_run_review_round0_disabled_single_call(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """情形 4：adversarial_round0=false → round-0 只调一次引擎、无 adversarial 产物。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    cfg_path = tmp_path / "no-adv.toml"
    cfg_path.write_text('[review]\nadversarial_round0 = false\n', encoding="utf-8")
    fake, calls = _stub_dispatch_by_path({"verdict": "approve", "findings": []})
    monkeypatch.setattr(_pipeline, "_codex_exec", fake)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(_pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt"))
    captured = _capture_emit_review_round(monkeypatch)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result = _pipeline.run_review_round(p_with_repo, 1, 0, config_path=cfg_path)

    assert result["ok"] is True
    assert calls["count"] == 1
    base = Path(result["review_json"]).parent
    assert not (base / "round-0.review.pass2.adversarial.json").exists()
    assert not (base / "round-0.adversarial.focus.md").exists()
    assert not (base / "round-0.review.pass1.json").exists()  # 单通道直写最终路径
    assert captured[-1]["adversarial_pass_ran"] is False
    assert captured[-1]["adversarial_blocking_count"] is None


def test_run_review_round1_single_call_no_adversarial(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """情形 5：round>=1 → 只调一次引擎、无 adversarial 产物、字段 False/None。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    _set_implement_commit(env_setup.state_json)
    fake, calls = _stub_dispatch_by_path({"verdict": "approve", "findings": []})
    monkeypatch.setattr(_pipeline, "_codex_exec", fake)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(_pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt"))
    captured = _capture_emit_review_round(monkeypatch)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result = _pipeline.run_review_round(p_with_repo, 1, 1)

    assert result["ok"] is True
    assert calls["count"] == 1
    assert all("adversarial" not in n for n in calls["names"])
    base = Path(result["review_json"]).parent
    assert not list(base.glob("*.adversarial.*"))
    assert captured[-1]["adversarial_pass_ran"] is False
    assert captured[-1]["adversarial_blocking_count"] is None


# ------------------------------------------------------------
# pass2 产出合法 JSON 但非法 REVIEW_SCHEMA → MUST 降级（不当作成功）
# 覆盖 finding F1（category=validation）：合法 JSON 语法 != 合法 review 结构。
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_pass2, why",
    [
        ({"verdict": "approve"}, "缺 findings 必填字段"),
        ({"verdict": "approve", "findings": "nope"}, "findings 非数组"),
        (
            {
                "verdict": "changes-requested",
                "findings": [{"id": "F1", "severity": "high", "title": "t"}],
            },
            "finding 缺必填字段（category/file/... 缺失）",
        ),
    ],
)
def test_run_review_round0_pass2_invalid_schema_degrades(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, bad_pass2, why
):
    """pass2 返回合法 JSON 但非法 schema → 重试耗尽后降级 pass1-only。

    回归 F1：旧代码只 json.loads 不校验 schema，非法 schema 会走成功路径
    （被 merge 成空 pass2 并 adversarial_pass_ran=True，或让 merge/parse 抛 ValueError
    绕过降级）。修复后应等价情形 2：ok=True、pass1-only、adversarial_pass_ran=False/count=None。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    fake, calls = _stub_dispatch_by_path(
        {"verdict": "changes-requested",
         "findings": [_adv_finding(file="a.py", cat="validation")]},
        pass2_payload=bad_pass2,
    )
    monkeypatch.setattr(_pipeline, "_codex_exec", fake)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(_pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt"))
    captured = _capture_emit_review_round(monkeypatch)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result = _pipeline.run_review_round(p_with_repo, 1, 0, retries=1)

    assert result["ok"] is True, why
    assert result["blocking"] == 1  # 等价 pass1-only（pass2 被降级为空替身）
    base = Path(result["review_json"]).parent
    merged = json.loads((base / "round-0.review.json").read_text())
    assert len(merged["findings"]) == 1
    # pass2 校验失败耗尽 retries → 每次都重试（retries=1 → 2 次 pass2 调用）
    assert sum(1 for n in calls["names"] if "pass2.adversarial" in n) == 2
    assert captured[-1]["adversarial_pass_ran"] is False
    assert captured[-1]["adversarial_blocking_count"] is None


def test_run_review_round0_pass1_invalid_schema_no_pass2(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """pass1 返回合法 JSON 但非法 schema → 整轮失败、pass2 MUST NOT 执行。

    回归 F1 的第二面：旧代码 pass1 非法 schema 时仍会执行 pass2、直到 parse_review
    才失败，违反"pass1 失败则 pass2 不执行"。修复后 pass1 schema 校验失败即耗尽 retries
    走既有 ok=False 路径，pass2 从未被调用。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    fake, calls = _stub_dispatch_by_path(
        {"verdict": "approve"},  # 合法 JSON，缺 findings → 非法 schema
    )
    monkeypatch.setattr(_pipeline, "_codex_exec", fake)
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(_pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt"))
    captured = _capture_emit_review_round(monkeypatch)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result = _pipeline.run_review_round(p_with_repo, 1, 0, retries=1)

    assert result["ok"] is False
    assert result["error"] == "codex-exec-failed"
    assert all("pass2.adversarial" not in n for n in calls["names"])  # pass2 从未执行
    assert captured[-1]["adversarial_pass_ran"] is False
    assert captured[-1]["adversarial_blocking_count"] is None


# ============================================================
# implement record
# ============================================================


def test_record_implement_success(env_setup, make_args, capsys, fake_repo: Path):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # 创建一个真实 commit 用作 commit 校验
    (fake_repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: x"], cwd=fake_repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    summary = p.run_dir / "001-add-foo" / "implement.summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# impl summary\n")

    result_line = f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)
    assert result["ok"] is True
    assert result["commit"] == commit

    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["implement"]["status"] == "done"
    assert entry["status"] == "reviewing"
    assert entry["implement_commit"] == commit


def test_record_implement_failed_tests(env_setup, make_args, capsys, fake_repo: Path):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result_line = "RESULT: commit=- tasks=0 tests=fail summary=- notes=bug"
    result = _pipeline.record_implement(p_with_repo, 1, result_line, require_summary=False)
    assert result["ok"] is False
    assert result["error"] == "implementer-failed"
    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "failed"


def test_record_implement_summary_missing(env_setup, make_args, capsys, fake_repo: Path):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    # 真实 commit
    (fake_repo / "g.txt").write_text("y")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "y"], cwd=fake_repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    result_line = f"RESULT: commit={commit} tasks=1 tests=pass summary=/nonexistent.md notes=-"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)
    assert result["ok"] is False
    assert result["error"] == "summary-missing"


# ============================================================
# fix record
# ============================================================


def test_record_fix_success(env_setup, make_args, capsys, fake_repo: Path):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    (fake_repo / "h.txt").write_text("z")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix"], cwd=fake_repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "round-1.fix.summary.md"
    summary.write_text("# fix summary\n")

    result_line = (
        f"RESULT: commit={commit} fixed=2 tests=pass summary={summary} "
        f"categories_scanned=validation regressions_added=- notes=-"
    )
    result = _pipeline.record_fix(p_with_repo, 1, 1, result_line)
    assert result["ok"] is True
    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["phases"]["fix-r1"]["status"] == "done"
    assert entry["status"] == "in-fix-loop"


# ============================================================
# archive run（subprocess mock）
# ============================================================


def test_run_archive_success(env_setup, make_args, capsys, fake_repo: Path, monkeypatch):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # 安排一个真实 implement_commit 写入 state
    (fake_repo / "i.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "impl"], cwd=fake_repo, check=True)
    impl_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    def mutate(s):
        e = s["progress"][0]
        e["implement_commit"] = impl_commit
        e["phases"] = {"implement": {"status": "done", "commit": impl_commit, "started_ms": 0, "started_at": "x"}}

    _state.update_state(p.state_json, p.state_md, mutate)

    # 制造 openspec/ 目录的改动让 git commit 有内容
    (fake_repo / "openspec").mkdir(exist_ok=True)
    (fake_repo / "openspec" / "x.md").write_text("dummy")

    # mock openspec validate / archive subprocess
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["/fake/openspec", "validate"] or (
            isinstance(cmd, list) and len(cmd) >= 2 and cmd[0].endswith("openspec") and cmd[1] == "validate"
        ):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0].endswith("openspec") and cmd[1] == "archive":
            # 真实 openspec archive 会把 changes/<id>/ 搬到 changes/archive/<date>-<id>/。
            # fake 补上等价副作用，否则会被新增的归档副作用核验判为 abort。
            arch = fake_repo / "openspec" / "changes" / "archive" / "2026-07-10-add-foo"
            arch.mkdir(parents=True, exist_ok=True)
            (arch / "proposal.md").write_text("archived")
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)
    assert result["ok"] is True
    assert result["change_id"] == "add-foo"
    assert result["archive_commit"]

    s = json.loads(p.state_json.read_text())
    entry = s["progress"][0]
    assert entry["status"] == "archived"
    assert entry["phases"]["archive"]["status"] == "done"


def test_run_archive_precheck_fails(env_setup, make_args, capsys, fake_repo: Path):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # 写一个不存在的 implement_commit，制造 precheck failure
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    def mutate(s):
        e = s["progress"][0]
        e["implement_commit"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    _state.update_state(p.state_json, p.state_md, mutate)

    result = _pipeline.run_archive(p_with_repo, 1)
    assert result["ok"] is False
    assert result["error"] == "commit-chain-broken"
    assert "deadbeef" in result["missing"][0]


# ============================================================
# archive-structured-errors: git add / _git_head 失败仍输出结构化 JSON
# ============================================================


def _make_archive_ready(env_setup, make_args, capsys, fake_repo: Path):
    """Bootstrap state so run_archive can reach the git steps."""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # 写入一个真实的 implement_commit
    (fake_repo / "i.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "impl"], cwd=fake_repo, check=True)
    impl_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    def mutate(s):
        e = s["progress"][0]
        e["implement_commit"] = impl_commit
        e["phases"] = {
            "implement": {"status": "done", "commit": impl_commit, "started_ms": 0, "started_at": "x"}
        }

    _state.update_state(p.state_json, p.state_md, mutate)
    return p_with_repo


def _make_archive_effect(fake_repo: Path, change_id: str = "add-foo") -> Path:
    """模拟 `openspec archive` 的真实副作用：把 changes/<id>/ 搬到
    changes/archive/<date>-<id>/。让走到 git 步骤的用例能通过新增的归档副作用核验。
    返回创建的归档目录路径。
    """
    change_dir = fake_repo / "openspec" / "changes" / change_id
    if change_dir.exists():
        shutil.rmtree(change_dir)
    arch = fake_repo / "openspec" / "changes" / "archive" / f"2026-07-10-{change_id}"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "proposal.md").write_text("archived")
    return arch


def _fake_run_openspec_ok(real_run):
    """返回一个 subprocess.run 替换，openspec 命令总返回 0，其余走真实调用。"""

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        return real_run(cmd, *args, **kwargs)

    return fake_run


def test_run_archive_git_add_failed_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: git add 因 index.lock 失败仍返回单行 JSON（ok=false, error=git-add-failed）。"""
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)
    _make_archive_effect(fake_repo)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "add"]:
            exc = subprocess.CalledProcessError(128, cmd, stderr="fatal: Unable to create index.lock")
            exc.stderr = "fatal: Unable to create index.lock"
            raise exc
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    # 核心断言：返回值是结构化 dict，ok=false，error=git-add-failed
    assert result["ok"] is False
    assert result["error"] == "git-add-failed"
    assert result["seq"] == 1

    # 验证可被 json.dumps 序列化（等价于 stdout 是合法 JSON）
    serialized = json.dumps(result)
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "git-add-failed"


def test_run_archive_git_add_failed_no_traceback_in_output(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: stdout 不含 traceback 字样（无裸 traceback 泄漏到 stdout）。"""
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)
    _make_archive_effect(fake_repo)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "add"]:
            exc = subprocess.CalledProcessError(128, cmd, stderr="fatal: index.lock exists")
            exc.stderr = "fatal: index.lock exists"
            raise exc
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    # 结果序列化为 JSON 后不含 "Traceback"
    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    assert result["ok"] is False


def test_run_archive_git_head_failed_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: _git_head 失败 → stdout 单行 JSON ok=false, error=git-head-failed，无裸 traceback。"""
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)
    _make_archive_effect(fake_repo)

    # openspec/ 目录需存在，让 git add 和 git commit 能成功
    (fake_repo / "openspec").mkdir(exist_ok=True)
    (fake_repo / "openspec" / "x.md").write_text("dummy")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        if isinstance(cmd, list) and cmd[:2] == ["git", "rev-parse"]:
            exc = subprocess.CalledProcessError(128, cmd, stderr="fatal: not a git repository")
            exc.stderr = "fatal: not a git repository"
            raise exc
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    assert result["ok"] is False
    assert result["error"] == "git-head-failed"

    # 验证 JSON 可序列化且不含 Traceback
    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "git-head-failed"


def test_run_archive_check_chain_git_missing_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: check_chain 在 git 二进制缺失时抛 RuntimeError，run_archive 应将其转换为
    结构化 JSON（ok=false, error=git-missing），而非让裸异常逃逸到 CLI 层被标为 dependency_missing exit 4。
    这是 F1 修复的回归测试：验证 precheck 阶段的 git-missing 路径。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)

    # 模拟 git_chain.check_chain 因 git 缺失抛 RuntimeError
    import npc.git_chain as _git_chain_mod

    def fake_check_chain(repo_root, entry):
        raise RuntimeError("未找到 git 命令")

    monkeypatch.setattr(_git_chain_mod, "check_chain", fake_check_chain)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    # 核心断言：异常被转化为结构化错误，而非裸 traceback
    assert result["ok"] is False
    assert result["error"] == "git-missing"
    assert result["seq"] == 1

    # 验证可被 json.dumps 序列化
    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "git-missing"


def test_run_archive_git_commit_fnf_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: git commit 的 subprocess.run 因 git 二进制缺失抛 FileNotFoundError，
    run_archive 应将其转换为结构化 JSON（ok=false, error=git-missing），而非被 CLI 层
    捕获为 dependency_missing exit 4。
    这是 F1 修复的回归测试：验证 git commit 阶段的 git-missing 路径。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)
    _make_archive_effect(fake_repo)

    # 创建 openspec/ 内容让 git add 能在真实 git 上成功
    (fake_repo / "openspec").mkdir(exist_ok=True)
    (fake_repo / "openspec" / "x.md").write_text("dummy")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        if isinstance(cmd, list) and cmd[:2] == ["git", "commit"]:
            raise FileNotFoundError(2, "No such file or directory: 'git'")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    # 核心断言：FileNotFoundError 被转化为结构化错误
    assert result["ok"] is False
    assert result["error"] == "git-missing"
    assert result["seq"] == 1

    # 验证可被 json.dumps 序列化且不含 Traceback
    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "git-missing"


# ============================================================
# archive-structured-errors round-2: openspec 启动失败路径回归
# ============================================================


def test_run_archive_openspec_missing_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: _find_openspec_bin 找不到 openspec（FileNotFoundError），
    run_archive 必须返回结构化 JSON（ok=false, error=openspec-missing, exit 1），
    不得逃逸到 cli_archive_run 的通用分支以 dependency_missing / exit 4 退出。
    这是 F1 round-2 修复的核心回归：覆盖 openspec 缺失路径。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)

    # 模拟 _find_openspec_bin 抛 FileNotFoundError（openspec 不在 PATH）
    def fake_find_openspec_bin(override=None):
        raise FileNotFoundError("未在 PATH 中找到 openspec 命令")

    monkeypatch.setattr(_pipeline, "_find_openspec_bin", fake_find_openspec_bin)

    result = _pipeline.run_archive(p_with_repo, 1)

    # 核心断言：异常被内部捕获并转化为结构化错误，不逃逸
    assert result["ok"] is False
    assert result["error"] == "openspec-missing"
    assert result["seq"] == 1

    # 验证可被 json.dumps 序列化（等价于 CLI 可输出单行 JSON）
    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "openspec-missing"


def test_run_archive_openspec_validate_subprocess_failed_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: openspec validate 的 subprocess.run 因路径无效抛 FileNotFoundError
    （--openspec-bin 指向不可执行路径），run_archive 必须将其转化为结构化 JSON
    （ok=false, error=openspec-subprocess-failed），而非逃逸为 dependency_missing exit 4。
    这是 F1 round-2 修复的回归测试：覆盖 openspec validate 启动失败路径。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0] and cmd[1] == "validate":
            raise FileNotFoundError(2, "No such file or directory: '/bad/openspec'")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    # _find_openspec_bin 返回一个路径，但该路径在 subprocess.run 时不可执行
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/bad/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    assert result["ok"] is False
    assert result["error"] == "openspec-subprocess-failed"
    assert result["seq"] == 1

    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "openspec-subprocess-failed"


def test_run_archive_openspec_archive_subprocess_failed_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario: openspec archive 的 subprocess.run 因路径无效抛 OSError（例如 EACCES），
    run_archive 必须将其转化为结构化 JSON（ok=false, error=openspec-subprocess-failed），
    而非逃逸为 dependency_missing exit 4。
    这是 F1 round-2 修复的回归测试：覆盖 openspec archive 启动失败路径。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0] and cmd[1] == "validate":
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0] and cmd[1] == "archive":
            raise OSError(13, "Permission denied: '/bad/openspec'")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/bad/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    assert result["ok"] is False
    assert result["error"] == "openspec-subprocess-failed"
    assert result["seq"] == 1

    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "openspec-subprocess-failed"


# ============================================================
# archive-verify-effect: 归档副作用核验（openspec archive 静默 abort 仍 exit 0）
# ============================================================


def test_archive_effect_happened_true_when_moved(tmp_path: Path):
    """2.1 change 目录已消失 + archive/ 下存在 -<id> 后缀目录 → 返回 True。"""
    (tmp_path / "openspec" / "changes" / "archive" / "2026-07-10-add-foo").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is True


def test_archive_effect_happened_false_when_change_dir_still_present(tmp_path: Path):
    """2.2 change 目录仍存在（未搬迁）→ False，即使 archive/ 恰好有同名后缀目录（历史遗留）。"""
    (tmp_path / "openspec" / "changes" / "add-foo").mkdir(parents=True)
    (tmp_path / "openspec" / "changes" / "archive" / "2026-07-10-add-foo").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is False


def test_archive_effect_happened_false_when_no_matching_archive_dir(tmp_path: Path):
    """2.3 change 目录已消失，但 archive/ 下无任何 -<id> 后缀目录 → False。"""
    (tmp_path / "openspec" / "changes" / "archive" / "2026-07-10-other-change").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is False


def test_archive_effect_happened_false_when_archive_dir_absent(tmp_path: Path):
    """2.4 archive/ 目录本身不存在（全新仓库从未归档）→ False，不抛异常。"""
    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is False


def test_archive_effect_happened_matches_any_valid_date(tmp_path: Path):
    """2.5 回归：前缀仅限数字/连字符 + `-<change_id>` 整体——任意合法日期前缀均匹配。"""
    (tmp_path / "openspec" / "changes" / "archive" / "2025-12-31-add-foo").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is True


def test_archive_effect_happened_matches_nonstandard_date_prefix(tmp_path: Path):
    """2.5 回归（非固定长度）：非零填充日期前缀 `2026-1-1-<change_id>` 仍正确匹配。

    tasks.md 2.5 明确要求匹配不依赖固定 `YYYY-MM-DD-` 10 字符长度——前缀只要
    仅含数字/连字符即放行。`2026-1-1-add-foo` 的前缀 `2026-1-1` 全是数字/连字符，
    故必须匹配为 True。这与 suffix-碰撞防护同时成立：碰撞前缀（如 `...-add`）含
    字母而被排除。
    """
    (tmp_path / "openspec" / "changes" / "archive" / "2026-1-1-add-foo").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is True


def test_archive_effect_happened_no_false_positive_on_substring(tmp_path: Path):
    """回归边界：archive/ 下目录名仅包含 change_id 作为子串但非 -<id> 后缀 → 不误判为 True。"""
    (tmp_path / "openspec" / "changes" / "archive" / "2026-07-10-add-foo-extra").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is False


def test_archive_effect_happened_no_false_positive_on_suffix_collision(tmp_path: Path):
    """F1 回归：change_id 为另一归档 change_id 的连字符后缀时不误判。

    change_id="foo"，archive/ 下仅有 `2026-07-10-add-foo`（另一个 change
    `add-foo` 的归档，恰以 `-foo` 结尾）。裸 endswith("-foo") 会误命中；
    锚定日期前缀 + change_id 整体相等必须返回 False。
    """
    (tmp_path / "openspec" / "changes" / "archive" / "2026-07-10-add-foo").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "foo") is False


def test_archive_effect_happened_false_on_missing_date_prefix(tmp_path: Path):
    """回归边界：archive/ 下目录名为裸 change_id（完全无数字/连字符日期前缀）→ False。

    `add-foo` 前无任何 `[0-9][0-9-]*-` 前缀，不符合 OpenSpec archive 命名契约
    （archive 目录名总是 `<date>-<change_id>`），不被视作有效归档副作用。
    注意：非零填充日期前缀 `2026-1-1-add-foo` 反而**应**匹配，见
    test_archive_effect_happened_matches_nonstandard_date_prefix。
    """
    (tmp_path / "openspec" / "changes" / "archive" / "add-foo").mkdir(parents=True)
    assert _pipeline._archive_effect_happened(tmp_path, "add-foo") is False


def test_run_archive_silent_abort_returns_structured_json(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """3.1 openspec archive exit 0 但归档副作用未发生（静默 abort）→
    ok=false, error=openspec-archive-aborted, stdout_tail 含 abort 原文；
    且 MUST NOT 执行后续 git add / git commit。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)
    # 关键：不调用 _make_archive_effect —— 归档副作用不发生。

    real_run = subprocess.run
    git_write_calls: list = []

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "Aborted. No files were changed." if cmd[1] == "archive" else ""
            r.stderr = ""
            return r
        # 记录任何 git add / git commit 调用——effect 核验失败后不应触发
        if isinstance(cmd, list) and cmd[:2] in (["git", "add"], ["git", "commit"]):
            git_write_calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    assert result["ok"] is False
    assert result["error"] == "openspec-archive-aborted"
    assert result["seq"] == 1
    assert "Aborted. No files were changed." in result["stdout_tail"]
    # MUST NOT 执行后续 git add / git commit
    assert git_write_calls == []

    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    parsed = json.loads(serialized)
    assert parsed["ok"] is False
    assert parsed["error"] == "openspec-archive-aborted"


def test_run_archive_silent_abort_records_failed_state(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """3.2 静默 abort → _do_phase_exit extra.reason == openspec-archive-aborted，
    progress 状态置为 failed。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "Aborted. No files were changed." if cmd[1] == "archive" else ""
            r.stderr = ""
            return r
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)
    assert result["error"] == "openspec-archive-aborted"

    s = json.loads(p_with_repo.state_json.read_text())
    entry = s["progress"][0]
    assert entry["status"] == "failed"
    assert entry["phases"]["archive"]["status"] == "failed"
    assert entry["phases"]["archive"].get("reason") == "openspec-archive-aborted"


def test_run_archive_nonzero_returncode_stays_archive_failed_no_effect_check(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """3.3 回归：openspec archive returncode != 0 → 沿用 openspec-archive-failed，
    两分支互斥，不触发新核验（即使此时归档副作用碰巧未发生也报 archive-failed，
    且字段仍为 stderr_tail 而非 stdout_tail）。
    """
    p_with_repo = _make_archive_ready(env_setup, make_args, capsys, fake_repo)
    # 不创建归档副作用；但因 returncode != 0，effect 核验不应被触发。

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0] and cmd[1] == "validate":
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        if isinstance(cmd, list) and len(cmd) >= 2 and "openspec" in cmd[0] and cmd[1] == "archive":
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "boom: archive command errored"
            return r
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)

    assert result["ok"] is False
    assert result["error"] == "openspec-archive-failed"
    assert result["stderr_tail"] == "boom: archive command errored"
    assert "stdout_tail" not in result


# ============================================================
# 接线测试：run_review_round routing guard（wire-verify-routing）
# ============================================================


def _make_p_with_repo(env_setup, fake_repo: Path):
    """把 env_setup Paths 的 repo_root 替换为 fake_repo。"""
    p = env_setup
    return type(p)(**{**p.__dict__, "repo_root": fake_repo})


def test_run_review_round_rejects_mimo_in_review_model(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """Scenario: 配置 review claude_model 含 mimo → run_review_round 拒绝执行并返回 routing-violation。

    接线测试：断言不变量 4（review 永不路由 MiMo）在 run_review_round 入口被强制。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # 构造含 mimo 的 review 配置（claude_model 含 "mimo"）
    cfg_path = tmp_path / "npc-mimo.toml"
    cfg_path.write_text(
        '[review]\nengine = "claude"\n[review.claude]\nmodel = "mimo-v2.5-pro"\n',
        encoding="utf-8",
    )

    p_with_repo = _make_p_with_repo(env_setup, fake_repo)

    with pytest.raises(SystemExit) as ei:
        _pipeline.run_review_round(p_with_repo, 1, 0, config_path=cfg_path)
    assert ei.value.code == 1

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "routing-violation"
    rules = {v["rule"] for v in out["violations"]}
    assert "mimo_exec_only" in rules


def test_run_review_round_rejects_review_same_source_as_coder(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """Scenario: review 与 coder 同源（相同 claude bin+model）→ 拒绝执行 routing-violation。

    接线测试：断言不变量 1（生成⊥验证）在 run_review_round 入口被强制。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # coder=claude(bin=claude, model=claude-opus-4-8) 与 review=claude(同 bin+model) → 同源
    cfg_path = tmp_path / "npc-same.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "claude"\nbin = "claude"\nmodel = "claude-opus-4-8"\n'
        '[review]\nengine = "claude"\n[review.claude]\nbin = "claude"\nmodel = "claude-opus-4-8"\n',
        encoding="utf-8",
    )

    p_with_repo = _make_p_with_repo(env_setup, fake_repo)

    with pytest.raises(SystemExit) as ei:
        _pipeline.run_review_round(p_with_repo, 1, 0, config_path=cfg_path)
    assert ei.value.code == 1

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "routing-violation"
    rules = {v["rule"] for v in out["violations"]}
    assert "gen_not_orthogonal" in rules


def test_run_review_round_allows_legal_routing(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """Scenario: 合法路由（coder=claude，review=codex）→ check_routing 无 violation，review 正常进入原逻辑。

    接线测试：断言合法配置不被路由守卫误拦。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # 合法：coder=claude, review=codex（不同源、不含 mimo）
    cfg_path = tmp_path / "npc-legal.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "claude"\n[review]\nengine = "codex"\n',
        encoding="utf-8",
    )

    review_payload = {"verdict": "approve", "findings": []}
    monkeypatch.setattr(_pipeline, "_codex_exec", _stub_codex_writes_review(review_payload))
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt")
    )

    p_with_repo = _make_p_with_repo(env_setup, fake_repo)

    result = _pipeline.run_review_round(p_with_repo, 1, 0, config_path=cfg_path)
    assert result["ok"] is True
    assert result["verdict"] == "approve"
    assert result.get("error") != "routing-violation"


def test_run_review_round_rejects_engine_name_cli_override_same_source(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """Scenario: 配置 review.engine=codex（通过校验），但 CLI --engine claude 覆盖后
    coder 与 review 同源（gen_not_orthogonal）→ 守卫必须用实际执行 engine 校验，拒绝执行。

    回归测试 F1：确保 engine_name CLI 覆盖场景下路由守卫检验的是实际执行的 engine，
    而非原始 cfg 中的 review.engine（codex），否则违规路由会绕过守卫。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # 配置：coder=claude(bin=claude, model=claude-opus-4-8)，review.engine=codex
    # 若不修复：check_routing 看到 review.engine=codex，与 coder=claude 不同源 → 通过
    # 修复后：CLI engine_name="claude" 覆盖，check_routing 看到 review.engine=claude，
    #          与 coder 完全同源（同 bin+model）→ gen_not_orthogonal → 拒绝
    cfg_path = tmp_path / "npc-codex-base.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "claude"\nbin = "claude"\nmodel = "claude-opus-4-8"\n'
        '[review]\nengine = "codex"\n[review.claude]\nbin = "claude"\nmodel = "claude-opus-4-8"\n',
        encoding="utf-8",
    )

    p_with_repo = _make_p_with_repo(env_setup, fake_repo)

    with pytest.raises(SystemExit) as ei:
        _pipeline.run_review_round(p_with_repo, 1, 0, config_path=cfg_path, engine_name="claude")
    assert ei.value.code == 1

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "routing-violation"
    rules = {v["rule"] for v in out["violations"]}
    assert "gen_not_orthogonal" in rules


def test_run_review_round_allows_engine_name_cli_override_different_source(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path, tmp_path: Path
):
    """Scenario: 配置 review.engine=codex，CLI --engine claude 覆盖，但 coder 与 review
    使用不同 model → 不同源，路由守卫应放行。

    回归测试 F1 正向场景：确保合法的 CLI engine 覆盖不会被误拒。
    """
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

    # coder=claude(model=claude-sonnet)，review.claude.model=claude-opus → 不同源，合法
    cfg_path = tmp_path / "npc-cli-claude-diff.toml"
    cfg_path.write_text(
        '[coder]\nbackend = "claude"\nbin = "claude"\nmodel = "claude-sonnet-4-5"\n'
        '[review]\nengine = "codex"\n[review.claude]\nbin = "claude"\nmodel = "claude-opus-4-8"\n',
        encoding="utf-8",
    )

    review_payload = {"verdict": "approve", "findings": []}
    monkeypatch.setattr(_pipeline, "_claude_exec", _stub_claude_writes_review(review_payload))
    monkeypatch.setattr(_pipeline, "_find_claude_bin", lambda override=None: "/fake/claude")
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/pt")
    )

    p_with_repo = _make_p_with_repo(env_setup, fake_repo)

    result = _pipeline.run_review_round(p_with_repo, 1, 0, config_path=cfg_path, engine_name="claude")
    assert result["ok"] is True
    assert result.get("error") != "routing-violation"


# ============================================================
# wire-verify-tests：record 阶段对 coder 自报 tests=pass 做真实复跑
# ============================================================

def _make_commit_and_summary(fake_repo: Path, p) -> tuple[str, Path]:
    """辅助：在 fake_repo 创建一个 commit + summary 文件，返回 (commit_hash, summary_path)。"""
    (fake_repo / "wire_test.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "wire: test"], cwd=fake_repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()
    summary = p.run_dir / "001-add-foo" / "implement.summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# summary\n")
    return commit, summary


def test_record_implement_rerun_fail_overrides_self_report(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario 3.1：复跑失败覆盖 coder 自报 → ok=False, tests_verified=False。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit, summary = _make_commit_and_summary(fake_repo, p)

    # 注入 run_tests_result：复跑失败
    def fake_rerun(repo_root, cfg, runner=None):
        return {"no_command": False, "passed": False, "cmd": "pytest", "tail": "1 failed"}

    monkeypatch.setattr(_verify, "run_tests_result", fake_rerun)
    # 配置 rerun_tests=true（显式开启，不依赖 NPC_MODE）
    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(
            verify=_config.VerifyConfig(rerun_tests=True)
        ),
    )

    result_line = f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)

    assert result["ok"] is False
    assert result["error"] == "rerun-tests-failed"
    assert result["tests"] == "fail"  # F1 regression: rerun failure must override self-report
    assert result["tests_verified"] is False
    assert "failed" in result.get("rerun_tail", "")

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "failed"
    # phase record must also carry tests="fail" so consumers see the override
    phase_record = s["progress"][0]["phases"]["implement"]
    assert phase_record.get("tests") == "fail"


def test_record_implement_rerun_pass_sets_verified_true(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario 3.2：复跑通过 → ok=True, tests_verified=True。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit, summary = _make_commit_and_summary(fake_repo, p)

    def fake_rerun(repo_root, cfg, runner=None):
        return {"no_command": False, "passed": True, "cmd": "pytest", "tail": "1 passed"}

    monkeypatch.setattr(_verify, "run_tests_result", fake_rerun)
    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(
            verify=_config.VerifyConfig(rerun_tests=True)
        ),
    )

    result_line = f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)

    assert result["ok"] is True
    assert result["tests_verified"] is True

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "reviewing"


def test_record_implement_rerun_disabled_skips_verify(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario 3.3：rerun_tests=false → 不复跑，行为与现状一致，tests_verified=None。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit, summary = _make_commit_and_summary(fake_repo, p)

    call_count = {"n": 0}

    def fake_rerun(repo_root, cfg, runner=None):
        call_count["n"] += 1
        return {"no_command": False, "passed": False, "cmd": "pytest", "tail": "fail"}

    monkeypatch.setattr(_verify, "run_tests_result", fake_rerun)
    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(
            verify=_config.VerifyConfig(rerun_tests=False)
        ),
    )

    result_line = f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)

    # 没有复跑：call_count 仍为 0
    assert call_count["n"] == 0
    # record 成功（采信自报）
    assert result["ok"] is True
    assert result.get("tests_verified") is None


def test_record_implement_rerun_no_command_degrades_gracefully(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """Scenario 3.4：探测不到测试命令 → tests_verified=None，record 不失败。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit, summary = _make_commit_and_summary(fake_repo, p)

    def fake_rerun(repo_root, cfg, runner=None):
        return {"no_command": True}

    monkeypatch.setattr(_verify, "run_tests_result", fake_rerun)
    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(
            verify=_config.VerifyConfig(rerun_tests=True)
        ),
    )

    result_line = f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)

    assert result["ok"] is True
    assert result["tests_verified"] is None

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "reviewing"


def test_record_fix_rerun_fail_overrides_self_report(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """record_fix 同样支持复跑失败覆盖自报。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})

    (fake_repo / "fix_wire.txt").write_text("fix")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix: wire"], cwd=fake_repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "round-1.fix.summary.md"
    summary.write_text("# fix summary\n")

    def fake_rerun(repo_root, cfg, runner=None):
        return {"no_command": False, "passed": False, "cmd": "pytest", "tail": "2 failed"}

    monkeypatch.setattr(_verify, "run_tests_result", fake_rerun)
    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(
            verify=_config.VerifyConfig(rerun_tests=True)
        ),
    )

    result_line = (
        f"RESULT: commit={commit} fixed=1 tests=pass summary={summary} "
        f"categories_scanned=validation regressions_added=- notes=-"
    )
    result = _pipeline.record_fix(p_with_repo, 1, 1, result_line)

    assert result["ok"] is False
    assert result["error"] == "rerun-tests-failed"
    assert result["tests"] == "fail"  # F2 regression: rerun failure must override self-report in fix path
    assert result["tests_verified"] is False

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "needs-user-decision"
    # phase record must also carry tests="fail" so consumers see the override
    phase_record = s["progress"][0]["phases"]["fix-r1"]
    assert phase_record.get("tests") == "fail"


# ============================================================
# F1 回归：p.mode="auto" 时（NPC_MODE 未设置）应触发复跑
# ============================================================


def test_should_rerun_tests_mode_auto_in_paths_no_env(monkeypatch):
    """_should_rerun_tests：p.mode=auto 且 NPC_MODE 未设置 → 触发复跑。

    回归：auto 档默认编排路径不导出 NPC_MODE 环境变量（--shell-exports deprecated），
    须从 p.mode（run.json 持久化）读取 mode 决定缺省值。
    """
    # 确保 NPC_MODE 不在环境中
    monkeypatch.delenv("NPC_MODE", raising=False)

    cfg = _config.Config(verify=_config.VerifyConfig(rerun_tests=None))  # 缺省

    # 构造 mode=auto 的 Paths（仅 mode 字段有意义，其余字段置 dummy）
    import dataclasses
    dummy_path = Path("/tmp/dummy")
    p_auto = _paths.Paths(
        repo_root=dummy_path,
        proj_key="dummy",
        task_log_dir=dummy_path,
        run_ts="2026-01-01-0000-00000",
        run_dir=dummy_path,
        state_json=dummy_path / "state.json",
        state_md=dummy_path / "state.md",
        index_file=dummy_path / "index.jsonl",
        schema_path=dummy_path / "schema.json",
        run_events=dummy_path / "events.jsonl",
        mode="auto",
    )
    p_interactive = dataclasses.replace(p_auto, mode="interactive")

    # auto 档：NPC_MODE 未设置，p.mode=auto → 应复跑
    assert _pipeline._should_rerun_tests(cfg, p_auto) is True

    # interactive 档：NPC_MODE 未设置，p.mode=interactive → 不复跑
    assert _pipeline._should_rerun_tests(cfg, p_interactive) is False

    # p=None 时（旧调用路径）→ 不复跑
    assert _pipeline._should_rerun_tests(cfg, None) is False


def test_should_rerun_tests_env_overrides_paths_mode(monkeypatch):
    """NPC_MODE 环境变量优先于 p.mode（兼容旧 --shell-exports 路径）。"""
    # NPC_MODE=auto 但 p.mode=interactive → 应复跑（env 优先）
    monkeypatch.setenv("NPC_MODE", "auto")
    cfg = _config.Config(verify=_config.VerifyConfig(rerun_tests=None))

    import dataclasses
    dummy_path = Path("/tmp/dummy")
    p_interactive = _paths.Paths(
        repo_root=dummy_path,
        proj_key="dummy",
        task_log_dir=dummy_path,
        run_ts="2026-01-01-0000-00000",
        run_dir=dummy_path,
        state_json=dummy_path / "state.json",
        state_md=dummy_path / "state.md",
        index_file=dummy_path / "index.jsonl",
        schema_path=dummy_path / "schema.json",
        run_events=dummy_path / "events.jsonl",
        mode="interactive",
    )
    assert _pipeline._should_rerun_tests(cfg, p_interactive) is True

    # NPC_MODE=interactive 但 p.mode=auto → 不复跑（env 优先）
    monkeypatch.setenv("NPC_MODE", "interactive")
    p_auto = dataclasses.replace(p_interactive, mode="auto")
    assert _pipeline._should_rerun_tests(cfg, p_auto) is False


def test_record_implement_auto_mode_rerun_via_paths(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """F1 回归：record_implement 在 NPC_MODE 未设置、p.mode=auto 时触发真实复跑。

    模拟 npc init --auto 的默认编排路径（不设 NPC_MODE）。
    """
    monkeypatch.delenv("NPC_MODE", raising=False)

    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    p = env_setup
    # 注入 p.mode=auto（模拟 run.json 里写了 mode=auto）
    import dataclasses
    p_auto = dataclasses.replace(p, mode="auto")
    p_with_repo = dataclasses.replace(p_auto, repo_root=fake_repo)
    commit, summary = _make_commit_and_summary(fake_repo, p)

    rerun_called = {"n": 0}

    def fake_rerun(repo_root, cfg, runner=None):
        rerun_called["n"] += 1
        return {"no_command": False, "passed": True, "cmd": "pytest", "tail": "1 passed"}

    monkeypatch.setattr(_verify, "run_tests_result", fake_rerun)
    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(
            verify=_config.VerifyConfig(rerun_tests=None)  # 显式缺省 → 由 p.mode 决定
        ),
    )

    result_line = f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)

    # p.mode=auto 应触发复跑
    assert rerun_called["n"] == 1, "auto 档缺省时应触发真实复跑"
    assert result["ok"] is True
    assert result["tests_verified"] is True


# ============================================================
# change fix-prompt-exhaustive-sweep：复现检测「重算前后差集」触发 telemetry
# ============================================================


def _seed_phases(p, seq, phases):
    def mutate(state):
        entry = state["progress"][seq - 1]
        entry["phases"] = dict(phases)
    _state.update_state(p.state_json, p.state_md, mutate)


def _capture_recurrence(monkeypatch):
    calls = []
    monkeypatch.setattr(
        _pipeline._telemetry,
        "emit_category_recurrence",
        lambda **kw: calls.append(kw),
    )
    return calls


def test_review_phase_exit_emits_recurrence_on_new_recur(
    env_setup, make_args, capsys, monkeypatch
):
    p = env_setup
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    # 已有 review-r0（触发轮）+ fix-r1 自报 error-handling
    _seed_phases(
        p, 1,
        {
            "review-r0": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
            "fix-r1": {"status": "done", "categories_scanned": "error-handling"},
        },
    )
    calls = _capture_recurrence(monkeypatch)
    # review-r1 再次判 error-handling blocking → 复现（M=1 ≥ N=1）
    _pipeline._do_review_phase_exit_and_trend(
        p, 1, "review-r1", {"blocking": 1, "categories": ["error-handling"], "verdict": "x"}
    )
    assert len(calls) == 1
    assert calls[0]["category"] == "error-handling"
    assert calls[0]["claimed_at_round"] == 1
    assert calls[0]["recurred_at_round"] == 1


def test_review_phase_exit_no_emit_when_no_recur(
    env_setup, make_args, capsys, monkeypatch
):
    p = env_setup
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    _seed_phases(
        p, 1,
        {
            "review-r0": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
            "fix-r1": {"status": "done", "categories_scanned": "error-handling"},
        },
    )
    calls = _capture_recurrence(monkeypatch)
    # review-r1 未再现 error-handling → 差集为空，不发事件
    _pipeline._do_review_phase_exit_and_trend(
        p, 1, "review-r1", {"blocking": 0, "categories": [], "verdict": "x"}
    )
    assert calls == []


def test_review_phase_exit_no_duplicate_recurrence_emit(
    env_setup, make_args, capsys, monkeypatch
):
    p = env_setup
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    # review-r1 已经确立了复现（fix-r1 claim → review-r1 再现）
    _seed_phases(
        p, 1,
        {
            "review-r0": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
            "fix-r1": {"status": "done", "categories_scanned": "error-handling"},
            "review-r1": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
        },
    )
    calls = _capture_recurrence(monkeypatch)
    # 写 review-r2 再现 → fix-r1 的复现已在 review-r1 记过（(error-handling,1) 去重），
    # review-r2 只新增不了 fix-r1 的条目；无 fix-r2 claim → 无新增
    _pipeline._do_review_phase_exit_and_trend(
        p, 1, "review-r2", {"blocking": 1, "categories": ["error-handling"], "verdict": "x"}
    )
    assert calls == []


def test_review_phase_exit_no_new_state_field_persisted(
    env_setup, make_args, capsys, monkeypatch
):
    p = env_setup
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    _seed_phases(
        p, 1,
        {
            "review-r0": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
            "fix-r1": {"status": "done", "categories_scanned": "error-handling"},
        },
    )
    _capture_recurrence(monkeypatch)
    _pipeline._do_review_phase_exit_and_trend(
        p, 1, "review-r1", {"blocking": 1, "categories": ["error-handling"], "verdict": "x"}
    )
    state = _state.read_state(p.state_json)
    entry = state["progress"][0]
    # 不落盘任何复现证据字段
    assert "category_recurrence_evidence" not in entry
    assert "recurred_categories" not in entry
    assert "category_streaks" not in entry
    for ph in entry["phases"].values():
        assert "recurred_categories" not in ph
        assert "category_recurrence_evidence" not in ph


# ============================================================
# commit-not-on-run-branch（fix-coder-cwd-desync：ancestor 门）
# ============================================================


def _make_linked_worktree_with_stray_commit(fake_repo: Path, tmp_path: Path) -> tuple[Path, str]:
    """建 linked worktree，然后在主 checkout 上再提交一笔（模拟 cwd 漂移的 coder）。

    返回 (worktree_path, 主分支上的漂移 commit hash)。该 commit 在共享对象库中
    可见（cat-file 门穿透），但不在 worktree HEAD 祖先链上。
    """
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "spine/test", str(wt), "HEAD"],
        cwd=fake_repo, check=True, capture_output=True,
    )
    (fake_repo / "stray.txt").write_text("drifted")
    subprocess.run(["git", "add", "stray.txt"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: drifted"], cwd=fake_repo, check=True)
    stray = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()
    return wt, stray


def test_record_implement_rejects_commit_not_on_run_branch(
    env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    wt, stray = _make_linked_worktree_with_stray_commit(fake_repo, tmp_path)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": wt})
    summary = p.run_dir / "001-add-foo" / "implement.summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# impl summary\n")

    result_line = f"RESULT: commit={stray} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)
    assert result["ok"] is False
    assert result["error"] == "commit-not-on-run-branch"
    assert result["commit"] == stray
    assert "hint" in result

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "failed"


def test_record_implement_accepts_commit_on_worktree_head(
    env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    wt = tmp_path / "wt2"
    subprocess.run(
        ["git", "worktree", "add", "-b", "spine/test2", str(wt), "HEAD"],
        cwd=fake_repo, check=True, capture_output=True,
    )
    # coder 正常在 worktree 内提交
    (wt / "ok.txt").write_text("good")
    subprocess.run(["git", "add", "ok.txt"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: ok"], cwd=wt, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True
    ).stdout.strip()

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": wt})
    summary = p.run_dir / "001-add-foo" / "implement.summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# impl summary\n")

    result_line = f"RESULT: commit={commit} tasks=3 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)
    assert result["ok"] is True
    assert result["commit"] == commit


def test_record_fix_rejects_commit_not_on_run_branch(
    env_setup, make_args, capsys, fake_repo: Path, tmp_path: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    wt, stray = _make_linked_worktree_with_stray_commit(fake_repo, tmp_path)

    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": wt})
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "round-1.fix.summary.md"
    summary.write_text("# fix summary\n")

    result_line = (
        f"RESULT: commit={stray} fixed=1 tests=pass summary={summary} "
        f"categories_scanned=validation regressions_added=- notes=-"
    )
    result = _pipeline.record_fix(p_with_repo, 1, 1, result_line)
    assert result["ok"] is False
    assert result["error"] == "commit-not-on-run-branch"
