"""doctor.py 测试：纯函数核 gather_checks + handler run 的 emit/退出码。

策略：
- monkeypatch 注入假的 which（控制各 bin present/missing）；
- tmp_path 造 fake home（有/无 mimo.env、有/无 schema）与 fake repo（有/无 principles.md）。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
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
        "providers",
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


def test_all_green(tmp_path: Path, monkeypatch):
    home = _make_home(tmp_path, mimo=True, schema=True)
    repo = _make_repo(tmp_path, principles=True)
    # install-source 取决于本机真实安装来源（可能装自未合并分支）→ 打桩，保持本例
    # "构造出的环境全绿"的语义；该检查项自身另有专门用例覆盖。
    monkeypatch.setattr(
        doctor,
        "_check_install_source",
        lambda **_kw: {
            "name": "install-source",
            "status": "ok",
            "detail": "stub",
            "required": False,
        },
    )
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


def test_run_git_missing_exit_4_single_line(tmp_path: Path, monkeypatch, capsys):
    home = _make_home(tmp_path, mimo=True, schema=True)
    repo = _make_repo(tmp_path, principles=True)
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(doctor._paths, "detect_repo_root", lambda *a, **k: repo)
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(ALL_BINS - {"git"}))

    with pytest.raises(SystemExit) as exc:
        doctor.run(_args())
    # required（git）缺失 → exit 4（外部依赖缺失），非 3（环境错）
    assert exc.value.code == 4

    lines = capsys.readouterr().out.strip().splitlines()
    # 严守单行 JSON 契约：只有一行，错误信息内嵌在唯一的 report 里
    assert len(lines) == 1
    report = json.loads(lines[0])
    assert report["ok"] is False
    assert report["summary"]["missing_required"] == ["git"]
    names = {c["name"] for c in report["checks"]}
    assert "git" in names and "config" in names
    # 错误体内嵌：error/message 点名 git，调用方一行 jq 即可读
    assert report["error"] == "dependency_missing"
    assert "git" in report["message"]


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


# ============================================================
# providers 检查（v1.6：路由在用 provider 就绪性）
# ============================================================


def test_providers_check_default_claude_ok(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    by = {c["name"]: c for c in checks}
    assert by["providers"]["status"] == "ok"
    assert "claude" in by["providers"]["detail"]


def test_providers_check_env_file_missing_warn(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    cfg_dir = repo / ".npc"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        f'[providers.kimi]\nenv_file = "{tmp_path / "nope.env"}"\n'
        '[coder]\nbackend = "kimi"\n',
        encoding="utf-8",
    )
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    by = {c["name"]: c for c in checks}
    assert by["providers"]["status"] == "warn"
    assert "env_file 缺失" in by["providers"]["detail"]


def test_providers_check_env_file_present_ok(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    env_file = tmp_path / "kimi.env"
    env_file.write_text("export ANTHROPIC_BASE_URL=https://kimi.example\n")
    cfg_dir = repo / ".npc"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        f'[providers.kimi]\nenv_file = "{env_file}"\nmodel = "kimi-k3"\n'
        '[coder]\nbackend = "kimi"\n',
        encoding="utf-8",
    )
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    by = {c["name"]: c for c in checks}
    assert by["providers"]["status"] == "ok"
    assert "kimi" in by["providers"]["detail"]


def test_providers_check_runner_bin_missing_warn(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    cfg_dir = repo / ".npc"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        '[coder]\nbackend = "codex"\n', encoding="utf-8"
    )
    # PATH 里没有 codex
    bins = tuple(b for b in ALL_BINS if b != "codex")
    checks = doctor.gather_checks(home=home, repo_root=repo, which=_which_factory(bins))
    by = {c["name"]: c for c in checks}
    assert by["providers"]["status"] == "warn"
    assert "codex" in by["providers"]["detail"]


# ============================================================
# v1.7 宿主检查项
# ============================================================


def test_host_check_present_and_ok(tmp_path: Path):
    home = _make_home(tmp_path)
    repo = _make_repo(tmp_path)
    checks = doctor.gather_checks(
        home=home, repo_root=repo, which=_which_factory(ALL_BINS)
    )
    host = next(c for c in checks if c["name"] == "host")
    assert host["status"] == "ok"
    assert host["required"] is False
    assert "宿主" in host["detail"]


# ============================================================
# install-source 检查项（安装来源与 main 的一致性）
# ============================================================


class _FakeDist:
    """最小 importlib.metadata.Distribution 替身：只需 version + read_text。"""

    def __init__(self, version: str, direct_url: str | None):
        self.version = version
        self._direct_url = direct_url

    def read_text(self, filename: str):
        if filename == "direct_url.json":
            return self._direct_url
        return None


def _fake_dist(monkeypatch, version: str, direct_url: str | None) -> None:
    monkeypatch.setattr(
        doctor.importlib.metadata,
        "distribution",
        lambda name: _FakeDist(version, direct_url),
    )


def _local_src(tmp_path: Path, version: str) -> Path:
    """造一个"源码 checkout"目录（只需 src/npc/__init__.py 带 __version__）。"""
    src = tmp_path / "checkout"
    init = src / "src" / "npc" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return src


def _git_runner(*, branch: str, sha: str = "abc1234", ancestor: bool):
    """假 git：只回答 rev-parse / merge-base，绝不真的调 git。"""

    def _run(argv, **kwargs):
        assert argv[0] == "git"
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("timeout") == 5
        sub = argv[1:]
        if sub[:2] == ["rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{branch}\n", stderr="")
        if sub[:2] == ["rev-parse", "--short"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{sha}\n", stderr="")
        if sub[0] == "merge-base":
            return subprocess.CompletedProcess(argv, 0 if ancestor else 1, stdout="", stderr="")
        raise AssertionError(f"未预期的 git 调用：{argv}")

    return _run


def test_install_source_local_even_on_main_warns(tmp_path: Path, monkeypatch):
    """本地目录安装即便在 main 且版本一致也必须 warn——开发中的代码不得影响本机 CLI。"""
    src = _local_src(tmp_path, "1.7.1")
    _fake_dist(monkeypatch, "1.7.1", json.dumps({"url": f"file://{src}"}))

    c = doctor._check_install_source(
        which=_which_factory({"git"}), run=_git_runner(branch="main", ancestor=True)
    )
    assert c["name"] == "install-source"
    assert c["status"] == "warn"
    assert c["required"] is False
    assert "本地目录安装" in c["detail"]
    assert str(src) in c["detail"] and "main abc1234" in c["detail"]
    assert "未合入 main" not in c["detail"]
    assert "git+https://github.com/winewei/agent-spine@v<版本>" in c["detail"]
    assert "uv run npc" in c["detail"]


def test_install_source_local_unmerged_branch_warn(tmp_path: Path, monkeypatch):
    src = _local_src(tmp_path, "1.7.1")
    _fake_dist(monkeypatch, "1.7.1", json.dumps({"url": f"file://{src}"}))

    c = doctor._check_install_source(
        which=_which_factory({"git"}),
        run=_git_runner(branch="release/v1.7.1", sha="3ba17c6", ancestor=False),
    )
    assert c["status"] == "warn"
    assert "release/v1.7.1 3ba17c6" in c["detail"]
    assert "未合入 main" in c["detail"]


def test_install_source_version_mismatch_warn(tmp_path: Path, monkeypatch):
    src = _local_src(tmp_path, "1.8.0")
    _fake_dist(monkeypatch, "1.7.1", json.dumps({"url": f"file://{src}"}))

    c = doctor._check_install_source(
        which=_which_factory({"git"}), run=_git_runner(branch="main", ancestor=True)
    )
    assert c["status"] == "warn"
    assert "已安装 1.7.1" in c["detail"] and "源码版本 1.8.0 与已安装不一致" in c["detail"]


def test_install_source_no_direct_url_warn(monkeypatch):
    _fake_dist(monkeypatch, "1.7.1", None)

    def _boom(*a, **k):
        raise AssertionError("不应触发 subprocess")

    c = doctor._check_install_source(which=_which_factory({"git"}), run=_boom)
    assert c["status"] == "warn"
    assert "无法读取安装来源" in c["detail"]


def test_install_source_remote_vcs_ok(monkeypatch):
    _fake_dist(
        monkeypatch,
        "1.7.1",
        json.dumps(
            {
                "url": "https://github.com/winewei/agent-spine",
                "vcs_info": {"vcs": "git", "commit_id": "0123456789abcdef", "requested_revision": "v1.7.1"},
            }
        ),
    )

    def _boom(*a, **k):
        raise AssertionError("不应触发 subprocess")

    c = doctor._check_install_source(which=_which_factory({"git"}), run=_boom)
    assert c["status"] == "ok"
    assert "0123456" in c["detail"]


def test_install_source_distribution_absent_warn(monkeypatch):
    def _absent(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(doctor.importlib.metadata, "distribution", _absent)
    c = doctor._check_install_source(which=_which_factory({"git"}))
    assert c["status"] == "warn"
    assert "无法读取安装来源" in c["detail"]


def test_install_source_registered_in_gather_checks(tmp_path: Path, monkeypatch):
    src = _local_src(tmp_path, "1.7.1")
    _fake_dist(monkeypatch, "1.7.1", json.dumps({"url": f"file://{src}"}))
    monkeypatch.setattr(doctor.subprocess, "run", _git_runner(branch="main", ancestor=True))

    checks = doctor.gather_checks(
        home=_make_home(tmp_path), repo_root=_make_repo(tmp_path), which=_which_factory(ALL_BINS)
    )
    c = next(c for c in checks if c["name"] == "install-source")
    assert c["status"] == "warn"
    assert c["required"] is False
