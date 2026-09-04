"""pipeline 模块测试：review run / archive run / implement_record / fix_record。

codex / openspec 通过 monkeypatch 替换 subprocess.run；git 命令使用真实 fake_repo。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from npc import pipeline as _pipeline
from npc import state as _state


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
    out = _pipeline._parse_result_line(line, ["commit", "tasks"])
    assert out["commit"] == "abc123"
    assert out["tasks"] == "5"
    assert out["tests"] == "pass"


def test_parse_result_line_value_with_spaces_until_next_key():
    line = "stuff before\nRESULT: commit=- tests=fail notes=this is a multi word note"
    out = _pipeline._parse_result_line(line, ["commit"])
    assert out["commit"] == "-"
    assert out["notes"] == "this is a multi word note"


def test_parse_result_line_missing():
    assert _pipeline._parse_result_line("nothing here", []) is None


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


def test_run_review_round_focus_uses_own_commits(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """review run 路径下 focus 也按本 change 自身提交出 git show（含当轮 fix-rN）。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    s = json.loads(env_setup.state_json.read_text())
    s["progress"][0]["implement_commit"] = "aaa1111"
    s["progress"][0]["phases"] = {
        "implement": {"commit": "aaa1111"},
        "fix-r1": {"commit": "bbb2222"},
    }
    env_setup.state_json.write_text(json.dumps(s))

    monkeypatch.setattr(
        _pipeline, "_codex_exec", _stub_codex_writes_review({"verdict": "approve", "findings": []})
    )
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/portable-timeout")
    )

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

    result = _pipeline.run_review_round(p_with_repo, 1, 1)
    assert result["ok"] is True
    text = (Path(result["review_json"]).parent / "round-1.focus.md").read_text()
    assert "git --no-pager show aaa1111" in text
    assert "git --no-pager show bbb2222" in text
    assert "aaa1111~1..HEAD" not in text


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
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """显式 engine_name='claude' 应路由到 _claude_exec，不调 _codex_exec。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

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

    result = _pipeline.run_review_round(p_with_repo, 1, 0, engine_name="claude")
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
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")

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
        p_with_repo, 1, 0, retries=1, engine_name="claude"
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


def test_run_archive_detects_silent_openspec_abort(env_setup, make_args, capsys, fake_repo: Path, monkeypatch):
    """openspec archive 因 delta 标题与基线冲突打印 Aborted 却 exit 0：change 目录仍在即判归档失败。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
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
    # change 目录在 archive 后仍存在 → 模拟 openspec 静默中止
    (fake_repo / "openspec" / "changes" / "add-foo").mkdir(parents=True)
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0].endswith("openspec"):
            r = MagicMock()
            r.returncode = 0
            r.stdout = 'foundation ADDED failed for header "### Requirement: NFR-X" - already exists\nAborted. No files were changed.\n' if cmd[1] == "archive" else ""
            r.stderr = ""
            return r
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _pipeline.run_archive(p_with_repo, 1)
    assert result["ok"] is False
    assert result["error"] == "openspec-archive-failed"
    assert "already exists" in result["stderr_tail"]
    entry = json.loads(p.state_json.read_text())["progress"][0]
    assert entry["status"] == "failed" and entry["reason"] == "openspec-archive"


def _archive_ready(env_setup, make_args, capsys, fake_repo: Path):
    """把 seq=1 推到"可归档"状态（真实 implement commit + state 装订），返回 paths。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    (fake_repo / "i.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "impl"], cwd=fake_repo, check=True)
    impl_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()
    p = env_setup

    def mutate(s):
        e = s["progress"][0]
        e["implement_commit"] = impl_commit
        e["phases"] = {
            "implement": {"status": "done", "commit": impl_commit, "started_ms": 0, "started_at": "x"}
        }

    _state.update_state(p.state_json, p.state_md, mutate)
    # git add openspec/ 需要该 pathspec 真实存在
    (fake_repo / "openspec").mkdir(exist_ok=True)
    (fake_repo / "openspec" / "x.md").write_text("dummy")
    return type(p)(**{**p.__dict__, "repo_root": fake_repo})


def _fake_openspec_ok(monkeypatch):
    """openspec validate/archive 一律成功；其余命令按传入 handler 处理。"""
    real_run = subprocess.run

    def make(commit_handler):
        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0].endswith("openspec"):
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
                return r
            if isinstance(cmd, list) and cmd[:2] == ["git", "commit"]:
                return commit_handler()
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(_pipeline, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    return make


def test_run_archive_git_commit_nothing_to_commit_detail(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """git commit 把 "nothing to commit" 写在 stdout：必须回传该输出并标 detail。"""
    p_with_repo = _archive_ready(env_setup, make_args, capsys, fake_repo)

    def commit_handler():
        r = MagicMock()
        r.returncode = 1
        r.stdout = "On branch main\nnothing to commit, working tree clean\n"
        r.stderr = ""
        return r

    _fake_openspec_ok(monkeypatch)(commit_handler)

    result = _pipeline.run_archive(p_with_repo, 1)
    assert result["ok"] is False
    assert result["error"] == "git-commit-failed"
    assert result["detail"] == "nothing-to-commit"
    assert "nothing to commit" in result["stderr_tail"]
    entry = json.loads(env_setup.state_json.read_text())["progress"][0]
    assert entry["status"] == "failed" and entry["reason"] == "git-commit"
    exit_extra = entry["phases"]["archive"]
    assert exit_extra["detail"] == "nothing-to-commit"
    assert "nothing to commit" in exit_extra["stderr"]


def test_run_archive_git_commit_stderr_failure_has_no_detail(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """普通 stderr 失败仍原样回传，且不带 nothing-to-commit 标记。"""
    p_with_repo = _archive_ready(env_setup, make_args, capsys, fake_repo)

    def commit_handler():
        r = MagicMock()
        r.returncode = 128
        r.stdout = ""
        r.stderr = "fatal: cannot lock ref 'HEAD'\n"
        return r

    _fake_openspec_ok(monkeypatch)(commit_handler)

    result = _pipeline.run_archive(p_with_repo, 1)
    assert result["ok"] is False
    assert result["error"] == "git-commit-failed"
    assert "detail" not in result
    assert "cannot lock ref" in result["stderr_tail"]


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
# 打地鼠派生指标（v1.7.1）
# ============================================================


def _finding(fid: str, category: str, severity: str = "high", in_scope: bool = True) -> dict:
    return {
        "id": fid,
        "severity": severity,
        "category": category,
        "title": "所有写入路径都不得跳过参数校验",
        "file": "a.py",
        "line_range": "1-2",
        "detail": "d",
        "recommendation": "r",
        "in_scope": in_scope,
    }


def test_count_high_findings():
    assert _pipeline._count_high_findings({"findings": []}) == 0
    assert _pipeline._count_high_findings(
        {
            "findings": [
                _finding("F1", "security"),
                _finding("F2", "security", severity="critical"),
                _finding("F3", "security", severity="medium"),
                _finding("F4", "security", in_scope=False),
            ]
        }
    ) == 2
    # 结构异常 → None（不影响主流程）
    assert _pipeline._count_high_findings({"findings": "oops"}) is None
    assert _pipeline._count_high_findings({}) is None


def test_repeat_category_ratio_missing_or_first_round(tmp_path: Path):
    assert _pipeline._repeat_category_ratio(tmp_path, 0, ["security"]) is None
    # round>=1 但上一轮 review.json 不存在
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["security"]) is None
    # 上一轮文件损坏
    (tmp_path / "round-0.review.json").write_text("{not json", encoding="utf-8")
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["security"]) is None


def test_repeat_category_ratio_computes_overlap(tmp_path: Path):
    (tmp_path / "round-0.review.json").write_text(
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [_finding("F1", "security"), _finding("F2", "validation")],
            }
        ),
        encoding="utf-8",
    )
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["security", "locking"]) == 0.5
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["security"]) == 1.0
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["locking"]) == 0.0


def test_repeat_category_ratio_ignores_non_blocking_categories(tmp_path: Path):
    """上一轮 advisory（low）/ out-of-scope 的 category 不算基线；本轮同理经 _blocking_categories 过滤。"""
    (tmp_path / "round-0.review.json").write_text(
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [
                    _finding("F1", "security"),
                    _finding("F2", "style", severity="low"),
                    _finding("F3", "validation", in_scope=False),
                ],
            }
        ),
        encoding="utf-8",
    )
    # 本轮 blocking 只有 locking；style/validation 在上一轮均非 blocking → 不构成重复
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["locking"]) == 0.0
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["style", "validation"]) == 0.0
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["security", "style"]) == 0.5
    # 上一轮全是非 blocking → 无可比基线
    (tmp_path / "round-0.review.json").write_text(
        json.dumps({"verdict": "passed-with-advisory", "findings": [_finding("F2", "style", severity="low")]}),
        encoding="utf-8",
    )
    assert _pipeline._repeat_category_ratio(tmp_path, 1, ["style"]) is None


def test_blocking_categories_filters_and_dedups():
    metrics = _pipeline._review.parse_review(
        {
            "verdict": "changes-requested",
            "findings": [
                _finding("F1", "security"),
                _finding("F2", "security"),
                _finding("F3", "style", severity="low"),
                _finding("F4", "validation", in_scope=False),
            ],
        }
    )
    assert metrics["categories"] == ["security", "style", "validation"]
    assert _pipeline._blocking_categories(metrics) == ["security"]


def test_run_review_round_emits_repeat_category_ratio(
    env_setup, make_args, capsys, monkeypatch, fake_repo: Path
):
    """存在上一轮 review.json 时，emit_review_round 收到正确的打地鼠指标。"""
    _bootstrap_run(env_setup, make_args, capsys, "add-foo")
    s = json.loads(env_setup.state_json.read_text())
    s["progress"][0]["implement_commit"] = "aaa1111"
    s["progress"][0]["phases"] = {"implement": {"commit": "aaa1111"}}
    env_setup.state_json.write_text(json.dumps(s))
    base = Path(s["progress"][0]["base"])
    base.mkdir(parents=True, exist_ok=True)
    # 上一轮：security + validation
    (base / "round-0.review.json").write_text(
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [_finding("F1", "security"), _finding("F2", "validation")],
            }
        ),
        encoding="utf-8",
    )
    # 本轮：security（重复）+ locking（新）→ ratio 0.5，high_count 2
    review_payload = {
        "verdict": "changes-requested",
        "findings": [_finding("F1", "security"), _finding("F2", "locking")],
    }
    monkeypatch.setattr(_pipeline, "_codex_exec", _stub_codex_writes_review(review_payload))
    monkeypatch.setattr(_pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")
    monkeypatch.setattr(
        _pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/portable-timeout")
    )

    captured: list[dict] = []

    def fake_emit(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(_pipeline._telemetry, "emit_review_round", fake_emit)

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

    result = _pipeline.run_review_round(p_with_repo, 1, 1)
    assert result["ok"] is True
    assert len(captured) == 1
    assert captured[0]["high_count"] == 2
    assert captured[0]["repeat_category_ratio"] == 0.5
