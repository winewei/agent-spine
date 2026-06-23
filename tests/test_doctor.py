"""doctor.py 测试：纯函数核 gather_checks + handler run 的 emit/退出码。

策略：
- monkeypatch 注入假的 which（控制各 bin present/missing）；
- tmp_path 造 fake home（有/无 mimo.env、有/无 schema）与 fake repo（有/无 principles.md）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from npc import doctor
from npc import paths as _paths


# ============================================================
# 测试辅助
# ============================================================


def _which_factory(present: set[str]):
    """构造假的 which：仅 present 中的名字返回路径，其余 None。"""

    def _which(name: str):
        return f"/usr/bin/{name}" if name in present else None

    return _which


ALL_BINS = {"git", "openspec", "codex", "claude", "jq", "portable-timeout"}


def _make_home(tmp_path: Path, *, mimo: bool = False, schema: bool = False) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if mimo:
        mimo_path = home / ".config" / "npc" / "mimo.env"
        mimo_path.parent.mkdir(parents=True, exist_ok=True)
        mimo_path.write_text("ANTHROPIC_BASE_URL=https://mimo\n", encoding="utf-8")
    if schema:
        schema_path = home / "task_log" / _paths.SCHEMA_FILENAME
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.write_text("{}", encoding="utf-8")
    return home


def _make_repo(tmp_path: Path, *, principles: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if principles:
        p = repo / "docs" / "principles.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# principles\n", encoding="utf-8")
    return repo


def _by_name(checks: list[dict]) -> dict[str, dict]:
    return {c["name"]: c for c in checks}


# ============================================================
# gather_checks：结构与字段
# ============================================================


def test_gather_checks_structure(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    assert isinstance(checks, list)
    for c in checks:
        assert set(c.keys()) == {"name", "status", "detail", "required"}
        assert c["status"] in {"ok", "missing", "warn"}
        assert isinstance(c["required"], bool)
        assert isinstance(c["detail"], str) and c["detail"]


def test_gather_checks_covers_all_items(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    names = {c["name"] for c in checks}
    expected = {
        "git",
        "openspec",
        "codex",
        "claude",
        "jq",
        "portable-timeout",
        "schema",
        "mimo.env",
        "config",
        "principles.md",
    }
    assert expected <= names


def test_only_git_is_required(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    required = {c["name"] for c in checks if c["required"]}
    assert required == {"git"}


# ============================================================
# 全绿场景
# ============================================================


def test_all_green(tmp_path: Path):
    home = _make_home(tmp_path, mimo=True, schema=True)
    repo = _make_repo(tmp_path, principles=True)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    report = doctor.build_report(checks)
    assert report["ok"] is True
    assert report["summary"]["missing_required"] == []
    assert report["summary"]["warn"] == 0
    assert report["summary"]["missing"] == 0
    assert all(c["status"] == "ok" for c in checks)


# ============================================================
# git 缺失：required → missing → report.ok False
# ============================================================


def test_git_missing_is_required_missing(tmp_path: Path):
    home = _make_home(tmp_path, mimo=True, schema=True)
    repo = _make_repo(tmp_path, principles=True)
    present = ALL_BINS - {"git"}
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(present)
    )
    by = _by_name(checks)
    assert by["git"]["status"] == "missing"
    assert by["git"]["required"] is True
    report = doctor.build_report(checks)
    assert report["ok"] is False
    assert report["summary"]["missing_required"] == ["git"]
    assert report["summary"]["missing"] == 1


# ============================================================
# 可选 bin 缺失：warn，不致命
# ============================================================


def test_optional_bins_missing_warn_not_fatal(tmp_path: Path):
    home = _make_home(tmp_path, mimo=True, schema=True)
    repo = _make_repo(tmp_path, principles=True)
    # 只有 git 在
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory({"git"})
    )
    by = _by_name(checks)
    for name in ("openspec", "codex", "claude", "jq"):
        assert by[name]["status"] == "warn"
        assert by[name]["required"] is False
    report = doctor.build_report(checks)
    # required（git）在 → ok 仍为 True
    assert report["ok"] is True
    assert report["summary"]["missing_required"] == []
    assert report["summary"]["missing"] == 0
    assert report["summary"]["warn"] >= 4


# ============================================================
# portable-timeout：PATH / fallback / 缺失
# ============================================================


def test_portable_timeout_via_path(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory({"git", "portable-timeout"})
    )
    pt = _by_name(checks)["portable-timeout"]
    assert pt["status"] == "ok"
    assert "PATH" in pt["detail"]


def test_portable_timeout_via_fallback(tmp_path: Path):
    home = _make_home(tmp_path)
    fallback = home / ".local" / "bin" / "portable-timeout"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    fallback.chmod(0o755)  # 真可执行才应判 ok
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory({"git"})
    )
    pt = _by_name(checks)["portable-timeout"]
    assert pt["status"] == "ok"
    assert str(fallback) in pt["detail"]


def test_portable_timeout_fallback_no_exec_bit_is_warn(tmp_path: Path):
    home = _make_home(tmp_path)
    fallback = home / ".local" / "bin" / "portable-timeout"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    fallback.chmod(0o644)  # 文件在但无执行位 → 实际不可用
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory({"git"})
    )
    pt = _by_name(checks)["portable-timeout"]
    assert pt["status"] == "warn"
    assert pt["required"] is False
    assert "不可执行" in pt["detail"]


def test_portable_timeout_missing(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory({"git"})
    )
    pt = _by_name(checks)["portable-timeout"]
    assert pt["status"] == "warn"
    assert pt["required"] is False


# ============================================================
# schema：有 / 无
# ============================================================


def test_schema_present(tmp_path: Path):
    home = _make_home(tmp_path, schema=True)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    assert _by_name(checks)["schema"]["status"] == "ok"


def test_schema_missing_is_warn(tmp_path: Path):
    home = _make_home(tmp_path, schema=False)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    sc = _by_name(checks)["schema"]
    assert sc["status"] == "warn"
    assert sc["required"] is False


def test_schema_unreadable_is_warn(tmp_path: Path):
    home = _make_home(tmp_path, schema=True)
    schema_path = home / "task_log" / _paths.SCHEMA_FILENAME
    schema_path.chmod(0o000)  # 存在但不可读
    repo = _make_repo(tmp_path)
    try:
        checks = doctor.gather_checks(
            home=home, repo_root=repo, which=_which_factory(ALL_BINS)
        )
        sc = _by_name(checks)["schema"]
        assert sc["status"] == "warn"
        assert "不可读" in sc["detail"]
    finally:
        schema_path.chmod(0o644)  # 还原以便 tmp 清理


def test_schema_invalid_json_is_warn(tmp_path: Path):
    home = _make_home(tmp_path, schema=True)
    schema_path = home / "task_log" / _paths.SCHEMA_FILENAME
    schema_path.write_text("{not valid json", encoding="utf-8")
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    sc = _by_name(checks)["schema"]
    assert sc["status"] == "warn"
    assert "非法" in sc["detail"]


# ============================================================
# mimo.env：有 / 无
# ============================================================


def test_mimo_env_present_marks_cost_routing(tmp_path: Path):
    home = _make_home(tmp_path, mimo=True)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    m = _by_name(checks)["mimo.env"]
    assert m["status"] == "ok"
    assert "成本路由" in m["detail"]


def test_mimo_env_missing_is_warn_not_missing(tmp_path: Path):
    home = _make_home(tmp_path, mimo=False)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    m = _by_name(checks)["mimo.env"]
    assert m["status"] == "warn"
    assert m["status"] != "missing"
    assert m["required"] is False


def test_mimo_env_unreadable_is_warn(tmp_path: Path):
    home = _make_home(tmp_path, mimo=True)
    mimo_path = home / ".config" / "npc" / "mimo.env"
    mimo_path.chmod(0o000)  # 存在但不可读
    repo = _make_repo(tmp_path)
    try:
        checks = doctor.gather_checks(
            home=home, repo_root=repo, which=_which_factory(ALL_BINS)
        )
        m = _by_name(checks)["mimo.env"]
        assert m["status"] == "warn"
        assert "不可读" in m["detail"]
    finally:
        mimo_path.chmod(0o644)


# ============================================================
# principles.md：有 / 无 / repo_root 缺失
# ============================================================


def test_principles_present(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path, principles=True)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    assert _by_name(checks)["principles.md"]["status"] == "ok"


def test_principles_missing_is_warn(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path, principles=False)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    p = _by_name(checks)["principles.md"]
    assert p["status"] == "warn"
    assert p["required"] is False


def test_principles_repo_root_none(tmp_path: Path):
    home = _make_home(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=None, which=_which_factory(ALL_BINS)
    )
    p = _by_name(checks)["principles.md"]
    assert p["status"] == "warn"


# ============================================================
# config：可加载 / 加载失败降级 warn
# ============================================================


def test_config_loadable_ok(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)  # 无配置文件 → 内置默认，仍可加载
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    assert _by_name(checks)["config"]["status"] == "ok"


def test_config_load_failure_degrades_to_warn(tmp_path: Path, monkeypatch):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)

    def _boom(*a, **k):
        from npc import config as _config

        raise _config.ConfigError("坏配置")

    monkeypatch.setattr("npc.doctor._config.load_config", _boom)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    c = _by_name(checks)["config"]
    assert c["status"] == "warn"
    assert c["required"] is False
    assert "坏配置" in c["detail"]


def test_config_oserror_degrades_to_warn(tmp_path: Path, monkeypatch):
    """非 ConfigError 异常（如 OSError）也必须降级 warn，不得裸抛崩溃 run。"""
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)

    def _boom(*a, **k):
        raise OSError("磁盘读崩了")

    monkeypatch.setattr("npc.doctor._config.load_config", _boom)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    c = _by_name(checks)["config"]
    assert c["status"] == "warn"
    assert c["required"] is False
    assert "OSError" in c["detail"]  # 标注异常类型名
    assert "磁盘读崩了" in c["detail"]


def test_config_default_source_wording(tmp_path: Path):
    """无配置文件 → source=<default> 时 detail 应明示用内置默认，不显示误导的 <default>。"""
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)  # 无任何配置文件
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    c = _by_name(checks)["config"]
    assert c["status"] == "ok"
    assert "内置默认" in c["detail"]
    assert "<default>" not in c["detail"]


# ============================================================
# summarize / build_report
# ============================================================


def test_summarize_counts(tmp_path: Path):
    checks = [
        {"name": "git", "status": "missing", "detail": "x", "required": True},
        {"name": "jq", "status": "warn", "detail": "x", "required": False},
        {"name": "claude", "status": "ok", "detail": "x", "required": False},
    ]
    s = doctor.summarize(checks)
    assert s == {
        "ok": 1,
        "warn": 1,
        "missing": 1,
        "missing_required": ["git"],
    }


def test_required_warn_not_counted_as_missing_required():
    """required 项处于 warn（非 missing）时不应误计入 missing_required → 不误触发 exit 3。"""
    checks = [
        {"name": "git", "status": "warn", "detail": "x", "required": True},
        {"name": "jq", "status": "ok", "detail": "x", "required": False},
    ]
    s = doctor.summarize(checks)
    assert s["missing_required"] == []
    report = doctor.build_report(checks)
    assert report["ok"] is True


def test_build_report_ok_when_no_required_missing():
    checks = [
        {"name": "git", "status": "ok", "detail": "x", "required": True},
        {"name": "jq", "status": "warn", "detail": "x", "required": False},
    ]
    report = doctor.build_report(checks)
    assert report["ok"] is True
    assert report["checks"] == checks
    assert report["summary"]["missing_required"] == []


# ============================================================
# run handler：emit JSON + 退出码
# ============================================================


def _args() -> argparse.Namespace:
    return argparse.Namespace()


def test_run_all_green_exit_0(tmp_path: Path, monkeypatch, capsys):
    home = _make_home(tmp_path, mimo=True, schema=True)
    repo = _make_repo(tmp_path, principles=True)
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(doctor._paths, "detect_repo_root", lambda *a, **k: repo)
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(ALL_BINS))

    doctor.run(_args())  # 不应抛 SystemExit

    out = capsys.readouterr().out.strip().splitlines()
    report = json.loads(out[0])
    assert report["ok"] is True
    assert report["summary"]["missing_required"] == []


def test_run_git_missing_exit_3_with_full_checks(tmp_path: Path, monkeypatch, capsys):
    home = _make_home(tmp_path, mimo=True, schema=True)
    repo = _make_repo(tmp_path, principles=True)
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(doctor._paths, "detect_repo_root", lambda *a, **k: repo)
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(ALL_BINS - {"git"}))

    with pytest.raises(SystemExit) as exc:
        doctor.run(_args())
    assert exc.value.code == 3

    lines = capsys.readouterr().out.strip().splitlines()
    # 第一行：完整报告（含全部 checks，调用方可知缺哪个 required）
    report = json.loads(lines[0])
    assert report["ok"] is False
    assert report["summary"]["missing_required"] == ["git"]
    names = {c["name"] for c in report["checks"]}
    assert "git" in names and "config" in names
    # 第二行：emit_error 的错误体，点名 git
    err = json.loads(lines[1])
    assert err["ok"] is False
    assert err["error"] == "dependency_missing"
    assert "git" in err["message"]


def test_run_optional_missing_exit_0(tmp_path: Path, monkeypatch, capsys):
    home = _make_home(tmp_path, mimo=False, schema=False)
    repo = _make_repo(tmp_path, principles=False)
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(doctor._paths, "detect_repo_root", lambda *a, **k: repo)
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"git"}))

    doctor.run(_args())  # required git 在 → 不退出非 0

    report = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert report["ok"] is True
    assert report["summary"]["warn"] >= 1


def test_run_repo_root_undetectable_still_runs(tmp_path: Path, monkeypatch, capsys):
    home = _make_home(tmp_path, mimo=True, schema=True)
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: home))

    def _boom(*a, **k):
        raise doctor._paths.PathsError("not a git repo")

    monkeypatch.setattr(doctor._paths, "detect_repo_root", _boom)
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(ALL_BINS))

    doctor.run(_args())  # repo_root 缺失不致命（git bin 仍在）

    report = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    # principles.md 走 repo_root=None 分支 → warn
    by = {c["name"]: c for c in report["checks"]}
    assert by["principles.md"]["status"] == "warn"
    assert report["ok"] is True
