"""agent 模块测试：prompt render + spawn-prompt（v1.0.0）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import agent as _agent
from npc import experience as _experience
from npc import state as _state
from npc import telemetry as _telemetry
from npc import templates as _templates


# ============================================================
# Helpers
# ============================================================


def _bootstrap(env_setup, make_args, capsys, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _read_emit(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _write_review(base: Path, round_n: int, findings: list[dict], verdict: str = "changes-requested") -> Path:
    rv = base / f"round-{round_n}.review.json"
    rv.write_text(
        json.dumps({"verdict": verdict, "findings": findings}, ensure_ascii=False),
        encoding="utf-8",
    )
    return rv


# ============================================================
# resolve helpers
# ============================================================


def test_resolve_seq_by_change_id(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo", "add-bar", "add-baz")
    state = _state.read_state(env_setup.state_json)
    assert _agent._resolve_seq(state, "add-bar", None) == 2
    assert _agent._resolve_seq(state, "add-baz", 3) == 3


def test_resolve_seq_unknown_change_id(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    state = _state.read_state(env_setup.state_json)
    with pytest.raises(ValueError, match="不在 STATE_JSON.progress"):
        _agent._resolve_seq(state, "add-nope", None)


def test_resolve_seq_mismatched_explicit(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo", "add-bar")
    state = _state.read_state(env_setup.state_json)
    with pytest.raises(ValueError, match="不一致"):
        _agent._resolve_seq(state, "add-foo", 2)


# ============================================================
# prompt render — implement
# ============================================================


def test_prompt_render_implement_writes_file(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    out = _read_emit(capsys)
    assert out["ok"] is True
    assert out["phase"] == "implement"
    assert out["seq"] == 1
    assert out["template_version"]
    assert out["bytes"] > 0

    p = Path(out["output"])
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "add-foo" in text
    assert "RESULT:" in text
    assert p.name == "implement.prompt.md"


def test_prompt_render_implement_rejects_round(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="implement",
                change_id="add-foo",
                seq=None,
                round_n=2,
                output=None,
                review_json=None,
                implement_commit=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "round_not_allowed"


def test_prompt_render_explicit_output_path(env_setup, make_args, capsys, tmp_path):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    out_path = tmp_path / "custom.md"
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=str(out_path),
            review_json=None,
            implement_commit=None,
        )
    )
    payload = _read_emit(capsys)
    assert Path(payload["output"]) == out_path
    assert out_path.exists()


# ============================================================
# prompt render — fix
# ============================================================


def test_prompt_render_fix_requires_round(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=None,
                output=None,
                review_json=None,
                implement_commit=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "missing_round"


def test_prompt_render_fix_requires_implement_commit(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    # 不在 state 里写 implement_commit、也不传 --implement-commit
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=1,
                output=None,
                review_json=None,
                implement_commit=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "missing_implement_commit"


def test_prompt_render_fix_missing_review(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=1,
                output=None,
                review_json=None,
                implement_commit="abc1234",
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "review_not_found"


def test_prompt_render_fix_full_flow(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    # 找到 base
    state = _state.read_state(env_setup.state_json)
    base = Path(state["progress"][0]["base"])

    # 写 round-0.review.json（fix-r1 需要）
    findings = [
        {
            "id": "F1",
            "severity": "high",
            "category": "validation",
            "title": "Missing input check",
            "file": "src/foo.py",
            "line_range": "42-58",
            "detail": "no validation on x",
            "recommendation": "add type check",
            "in_scope": True,
        },
        {
            "id": "F2",
            "severity": "medium",
            "category": "style",
            "title": "trailing whitespace",
            "file": "src/bar.py",
            "line_range": "1-1",
            "detail": "ws",
            "recommendation": "remove",
            "in_scope": True,
        },
    ]
    _write_review(base, 0, findings)

    # 把 categories_seen / blocking_trend / implement_commit 注入 state
    def _mut(s: dict) -> None:
        s["progress"][0]["implement_commit"] = "deadbeef"
        s["progress"][0]["categories_seen"] = ["validation", "style"]
        s["progress"][0]["blocking_trend"] = [3]

    _state.update_state(env_setup.state_json, env_setup.state_md, _mut)

    _agent.prompt_render(
        make_args(
            phase="fix",
            change_id="add-foo",
            seq=None,
            round_n=1,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    out = _read_emit(capsys)
    assert out["ok"] is True
    assert out["phase"] == "fix"
    assert out["round"] == 1
    assert out["blocking_count"] == 1  # 仅 F1（critical/high + in_scope）
    assert out["implement_commit"] == "deadbeef"

    text = Path(out["output"]).read_text(encoding="utf-8")
    # 精确断言：blocking findings 段（## Review Findings 与 ## 修复历史 之间）
    # 应该只含 in_scope blocking 的 F1，不含 advisory 的 F2
    findings_start = text.index("## Review Findings")
    findings_end = text.index("## 修复历史")
    findings_section = text[findings_start:findings_end]
    assert "F1" in findings_section
    assert "F2" not in findings_section  # advisory 不进入 Fixer prompt 的 findings 段
    assert "Missing input check" in findings_section
    # 其它运行时变量
    assert "deadbeef" in text
    assert "FIX_ROUND=1" in text
    assert "validation, style" in text
    assert "fix(add-foo): review round 1" in text


def test_prompt_render_fix_explicit_implement_commit_overrides_state(
    env_setup, make_args, capsys
):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    state = _state.read_state(env_setup.state_json)
    base = Path(state["progress"][0]["base"])
    _write_review(base, 0, [])

    _agent.prompt_render(
        make_args(
            phase="fix",
            change_id="add-foo",
            seq=None,
            round_n=1,
            output=None,
            review_json=None,
            implement_commit="override-hash",
        )
    )
    out = _read_emit(capsys)
    assert out["implement_commit"] == "override-hash"


# ============================================================
# spawn-prompt
# ============================================================


def test_spawn_prompt_basic_implement(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")

    # 先 render
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    rendered = _read_emit(capsys)

    # 然后 spawn-prompt
    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=None,
            extension=None,
            extension_inline=None,
        )
    )
    out = _read_emit(capsys)

    assert out["ok"] is True
    assert out["prompt_file"] == rendered["output"]
    assert "prompt" in out
    assert rendered["output"] in out["prompt"]
    assert out["has_extension"] is False
    # 引导语应该极短（不携带模板本体）。
    # 实测 ~600 bytes（含 prompt 文件绝对路径，路径越长引导语越长）；
    # 给一个宽松上限，确保不会回退到内联完整模板的程度。
    assert out["bytes"] < 1000


def test_spawn_prompt_missing_prompt_file(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.spawn_prompt(
            make_args(
                phase="implement",
                change_id="add-foo",
                seq=None,
                round_n=None,
                prompt_file=None,
                extension=None,
                extension_inline=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "prompt_file_not_found"


def test_spawn_prompt_with_inline_extension(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    capsys.readouterr()

    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=None,
            extension=None,
            extension_inline="- 跑 make race-test",
        )
    )
    out = _read_emit(capsys)
    assert out["has_extension"] is True
    assert "make race-test" in out["prompt"]


def test_spawn_prompt_extension_file(env_setup, make_args, capsys, tmp_path):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    capsys.readouterr()

    ext_file = tmp_path / "ext.md"
    ext_file.write_text("- 额外指令 A\n- 额外指令 B")

    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=None,
            extension=str(ext_file),
            extension_inline=None,
        )
    )
    out = _read_emit(capsys)
    assert "额外指令 A" in out["prompt"]
    assert "额外指令 B" in out["prompt"]


def test_spawn_prompt_extension_conflict(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    capsys.readouterr()

    with pytest.raises(SystemExit):
        _agent.spawn_prompt(
            make_args(
                phase="implement",
                change_id="add-foo",
                seq=None,
                round_n=None,
                prompt_file=None,
                extension="/some/path",
                extension_inline="inline text",
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "conflicting_args"


def test_spawn_prompt_fix_requires_round(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.spawn_prompt(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=None,
                prompt_file=None,
                extension=None,
                extension_inline=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "missing_round"


def test_spawn_prompt_custom_prompt_file(env_setup, make_args, capsys, tmp_path):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    custom = tmp_path / "my-prompt.md"
    custom.write_text("hi")

    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=str(custom),
            extension=None,
            extension_inline=None,
        )
    )
    out = _read_emit(capsys)
    assert Path(out["prompt_file"]) == custom.resolve()


# ============================================================
# 经验层召回（蓝本 §3.4）
# ============================================================


def _enable_experience(repo: Path) -> None:
    (repo / ".npc").mkdir(exist_ok=True)
    (repo / ".npc" / "config.toml").write_text(
        "[experience]\nenabled = true\n", encoding="utf-8"
    )


def _entry(uri: str, score: float, text: str = "先写真实回归再改实现") -> dict:
    return {"uri": uri, "score": score, "text": text, "detail": ""}


def _stub_recall(monkeypatch, result: _experience.RecallResult) -> dict:
    """把 from_config / recall 替换为可观测桩；返回捕获到的调用参数。"""
    captured: dict = {}
    monkeypatch.setattr(_experience, "from_config", lambda *a, **k: object())

    def fake_recall(client, cfg, *, phase, query, exclude_uris=()):
        captured["phase"] = phase
        captured["query"] = query
        captured["exclude_uris"] = list(exclude_uris)
        return result

    monkeypatch.setattr(_experience, "recall", fake_recall)
    return captured


def _spy_telemetry(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_experience_recall", lambda **kw: calls.append(kw)
    )
    return calls


def _render_implement(make_args, change_id: str = "add-foo") -> None:
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id=change_id,
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )


def test_experience_disabled_leaves_prompt_byte_identical(
    env_setup, make_args, capsys
):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _render_implement(make_args)
    out = _read_emit(capsys)

    assert out["experience_injected"] == 0
    assert "experience_error" not in out
    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    assert Path(out["output"]).read_text(encoding="utf-8") == _templates.render_implementer(
        change_id="add-foo", base=str(base), repo_root=str(env_setup.repo_root)
    )
    assert not (base / "implement.experience.json").exists()


def test_experience_enabled_injects_block_and_writes_record(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    result = _experience.RecallResult(
        entries=(
            _entry("viking://~/memories/experiences/e1.md", 0.91),
            _entry("viking://~/memories/experiences/e2.md", 0.72),
        ),
        tokens=40,
        query="q",
    )
    _stub_recall(monkeypatch, result)
    calls = _spy_telemetry(monkeypatch)

    _render_implement(make_args)
    out = _read_emit(capsys)

    assert out["ok"] is True
    assert out["experience_injected"] == 2
    assert out["experience_tokens"] > 0
    text = Path(out["output"]).read_text(encoding="utf-8")
    assert text.count(_experience.INJECTION_TAG) == 2
    assert _experience.BLOCK_HEADING in text

    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    record = base / "implement.experience.json"
    assert record.is_file()
    assert Path(out["experience_record"]) == record
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert [u["uri"] for u in payload["uris"]] == [
        "viking://~/memories/experiences/e1.md",
        "viking://~/memories/experiences/e2.md",
    ]
    assert (base / "implement.experience.md").is_file()

    assert len(calls) == 1
    assert calls[0]["entries"] == 2
    assert calls[0]["ok"] is True
    assert calls[0]["phase"] == "implement"
    assert calls[0]["policy_snapshot_id"]


def test_experience_first_recall_pins_policy_snapshot_id(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo", "add-bar")
    _stub_recall(
        monkeypatch,
        _experience.RecallResult(
            entries=(_entry("viking://~/memories/experiences/e1.md", 0.9),), query="q"
        ),
    )
    _spy_telemetry(monkeypatch)

    _render_implement(make_args)
    capsys.readouterr()
    pinned = _state.read_state(env_setup.state_json)["policy_snapshot_id"]
    assert isinstance(pinned, str) and len(pinned) == 64

    # 第二个 change 召回到不同条目也不改锚点：同一 run 内比较必须锚定同一经验库版本
    _stub_recall(
        monkeypatch,
        _experience.RecallResult(
            entries=(_entry("viking://~/memories/experiences/e9.md", 0.5),), query="q2"
        ),
    )
    _render_implement(make_args, "add-bar")
    capsys.readouterr()
    assert _state.read_state(env_setup.state_json)["policy_snapshot_id"] == pinned


def test_experience_recall_error_keeps_prompt_clean_and_exit_zero(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _stub_recall(monkeypatch, _experience.RecallResult(error="timeout", query="q"))
    calls = _spy_telemetry(monkeypatch)

    _render_implement(make_args)
    out = _read_emit(capsys)

    assert out["ok"] is True
    assert out["experience_injected"] == 0
    assert out["experience_error"] == "timeout"
    text = Path(out["output"]).read_text(encoding="utf-8")
    assert _experience.INJECTION_TAG not in text
    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    # 失败也落回执：query 与 error 是复盘召回质量的唯一证据
    assert (base / "implement.experience.json").is_file()
    assert calls[0]["ok"] is False and calls[0]["error"] == "timeout"
    assert _state.read_state(env_setup.state_json)["policy_snapshot_id"] is None


def test_experience_no_credentials_reports_error_without_recall(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    monkeypatch.setattr(_experience, "from_config", lambda *a, **k: None)

    _render_implement(make_args)
    out = _read_emit(capsys)

    assert out["experience_injected"] == 0
    assert out["experience_error"] == "no-credentials"


def test_experience_internal_exception_degrades_to_empty_block(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_experience, "from_config", boom)

    _render_implement(make_args)
    out = _read_emit(capsys)

    assert out["ok"] is True
    assert out["experience_injected"] == 0
    assert out["experience_error"] == "internal"
    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    assert Path(out["output"]).read_text(encoding="utf-8") == _templates.render_implementer(
        change_id="add-foo", base=str(base), repo_root=str(env_setup.repo_root)
    )


def test_experience_implement_query_uses_proposal_title_and_stack(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    prop = env_setup.repo_root / "openspec" / "changes" / "add-foo"
    prop.mkdir(parents=True)
    (prop / "proposal.md").write_text(
        "\n# 并发写入去重\n\n## Why\n...\n", encoding="utf-8"
    )
    (env_setup.repo_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    captured = _stub_recall(monkeypatch, _experience.RecallResult(query="q"))
    _spy_telemetry(monkeypatch)

    _render_implement(make_args)
    capsys.readouterr()

    assert "add-foo" in captured["query"]
    assert "并发写入去重" in captured["query"]
    assert "python" in captured["query"]


def test_experience_fix_query_uses_previous_round_blocking_findings(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    _write_review(
        base,
        0,
        [
            {
                "id": "F1",
                "severity": "high",
                "category": "race-condition",
                "title": "并发写入丢失",
                "file": "src/a.py",
                "line_range": "1-2",
                "detail": "d",
                "recommendation": "r",
                "in_scope": True,
            },
            {
                "id": "F2",
                "severity": "low",
                "category": "style",
                "title": "空行",
                "file": "src/b.py",
                "line_range": "1-1",
                "detail": "d",
                "recommendation": "r",
                "in_scope": True,
            },
        ],
    )
    captured = _stub_recall(
        monkeypatch,
        _experience.RecallResult(
            entries=(_entry("viking://~/memories/experiences/e1.md", 0.8),), query="q"
        ),
    )
    _spy_telemetry(monkeypatch)

    _agent.prompt_render(
        make_args(
            phase="fix",
            change_id="add-foo",
            seq=None,
            round_n=1,
            output=None,
            review_json=None,
            implement_commit="deadbeef",
        )
    )
    out = _read_emit(capsys)

    assert captured["phase"] == "fix"
    assert "race-condition: 并发写入丢失" in captured["query"]
    assert "空行" not in captured["query"]  # advisory 不进 query
    assert out["experience_injected"] == 1
    assert (base / "fix-r1.experience.json").is_file()
    text = Path(out["output"]).read_text(encoding="utf-8")
    assert text.index("## 修复历史") < text.index(_experience.INJECTION_TAG)
    assert text.index(_experience.INJECTION_TAG) < text.index("## 修复规则")


def test_experience_excludes_already_injected_uris(
    env_setup, make_args, capsys, monkeypatch
):
    _enable_experience(env_setup.repo_root)
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    _experience.write_injection_record(
        base,
        "fix",
        9,
        _experience.RecallResult(
            entries=(_entry("viking://~/memories/experiences/old.md", 0.6),), query="q"
        ),
        "abc1234",
    )
    captured = _stub_recall(monkeypatch, _experience.RecallResult(query="q"))
    _spy_telemetry(monkeypatch)

    _render_implement(make_args)
    capsys.readouterr()

    assert captured["exclude_uris"] == ["viking://~/memories/experiences/old.md"]
