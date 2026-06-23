"""npc init：路径计算 / 目录创建 / schema 与 portable-timeout 自举 / session 识别 / 续跑探测。

模块命名为 init_cmd 而非 init，避免与 __init__.py 冲突。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from . import _io, paths as _paths, schema, session, resume, git_chain as _git_chain, state as _state
from . import settings_auth as _settings_auth


PORTABLE_TIMEOUT_REL = ".local/bin/portable-timeout"

PORTABLE_TIMEOUT_SCRIPT = r"""#!/usr/bin/env bash
# portable-timeout: 跨平台 timeout（GNU timeout / gtimeout / perl alarm 三级回退）
# 用法: portable-timeout SECONDS COMMAND [ARGS...]
# 退出码：透传子命令；超时 124；用法错误 2；命令不存在 127
set -u
if [ "$#" -lt 2 ]; then
  echo "usage: portable-timeout SECONDS COMMAND [ARGS...]" >&2
  exit 2
fi
if command -v timeout >/dev/null 2>&1; then
  exec timeout --kill-after=10 "$@"
elif command -v gtimeout >/dev/null 2>&1; then
  exec gtimeout --kill-after=10 "$@"
else
  exec perl -e '
    use strict; use warnings;
    use POSIX ":sys_wait_h";
    my $secs = shift @ARGV;
    my $pid = fork;
    die "fork: $!" unless defined $pid;
    if ($pid == 0) { exec { $ARGV[0] } @ARGV; exit 127; }
    my $timed_out = 0;
    local $SIG{ALRM} = sub {
      $timed_out = 1;
      kill "TERM", $pid;
      for (1..10) { sleep 1; last if waitpid($pid, WNOHANG) > 0; }
      if (waitpid($pid, WNOHANG) == 0) {
        kill "KILL", $pid;
        waitpid($pid, 0);
      }
      exit 124;
    };
    alarm $secs;
    waitpid $pid, 0;
    my $rc = $?;
    alarm 0;
    exit 124 if $timed_out;
    if ($rc & 127) { exit 128 + ($rc & 127); }
    exit ($rc >> 8);
  ' "$@"
fi
"""


def ensure_portable_timeout(home: Path | None = None) -> tuple[Path, bool]:
    """写 portable-timeout wrapper 到 ~/.local/bin/portable-timeout（若不存在）。

    返回 (path, created)。
    """
    h = home or Path.home()
    target = h / PORTABLE_TIMEOUT_REL
    if target.exists() and os.access(target, os.X_OK):
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(PORTABLE_TIMEOUT_SCRIPT, encoding="utf-8")
    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target, True


def _emit_shell_exports(payload: dict) -> None:
    """[Deprecated since v0.2] 以 shell export 行格式输出关键字段到 stdout。

    v0.2 起子命令默认通过 ``run.json`` + ``active.json`` 自包含 resolve，
    无需 ``eval "$(npc init --shell-exports)"``。保留此分支仅为向后兼容。
    """
    # 单引号包裹值（值不含单引号；路径里不可能）
    key_map = [
        ("NPC_REPO_ROOT", payload["repo_root"]),
        ("NPC_PROJ_KEY", payload["proj_key"]),
        ("NPC_TASK_LOG_DIR", payload["task_log_dir"]),
        ("NPC_RUN_TS", payload["run_ts"]),
        ("NPC_RUN_DIR", payload["run_dir"]),
        ("NPC_STATE_JSON", payload["state_json"]),
        ("NPC_STATE_MD", payload["state_md"]),
        ("NPC_INDEX_FILE", payload["index_file"]),
        ("NPC_SCHEMA_PATH", payload["schema_path"]),
        ("NPC_RUN_EVENTS", payload["run_events"]),
        ("NPC_SESSION_ID", payload["session_id"]),
        ("NPC_TRANSCRIPT_PATH", payload["transcript_path"]),
        ("NPC_SESSION_SOURCE", payload["session_source"]),
        ("NPC_NEEDS_RESUME", "true" if payload["needs_resume"] else "false"),
        ("NPC_RESUME_STATE_JSON", payload.get("resume_state_json") or ""),
        ("NPC_MODE", payload["mode"]),
        ("NPC_FRESH", "true" if payload["fresh"] else "false"),
    ]
    out_lines = [f"export {k}='{v}'" for k, v in key_map]
    sys.stdout.write("\n".join(out_lines) + "\n")


def run(args: argparse.Namespace) -> None:
    """init 主入口。"""
    home = Path.home()

    # 1. 探测 git repo
    try:
        repo_root = _paths.detect_repo_root()
    except _paths.PathsError as e:
        _io.emit_error("not_git_repo", str(e), exit_code=3)
        return

    # 2. 续跑探测（在生成 run_ts 之前）
    proj_key = _paths.proj_key_for(repo_root)
    task_log_dir = home / "task_log" / proj_key
    resume_state_json: Path | None = None
    needs_resume = False
    if not args.fresh:
        if task_log_dir.is_dir():
            resume_state_json = resume.find_latest_in_progress(task_log_dir)
            needs_resume = resume_state_json is not None

    # 3. 决定本次使用的 run_ts / paths
    if needs_resume and resume_state_json is not None:
        # 复用旧 run 的 ts
        try:
            old = json.loads(resume_state_json.read_text(encoding="utf-8"))
            old_run_ts = old.get("run_ts") or resume_state_json.name.replace("-plan-state.json", "")
        except (OSError, json.JSONDecodeError):
            old_run_ts = resume_state_json.name.replace("-plan-state.json", "")
        p = _paths.compute_paths(repo_root, run_ts=old_run_ts, home=home)
    else:
        p = _paths.compute_paths(repo_root, home=home)

    # 4. 确保目录
    _paths.ensure_dirs(p)

    # 4b. 落 run.json + active.json（v0.2 起作为子命令默认 resolve 入口）
    run_json_path = _paths.write_run_json(p)
    _paths.set_active(p.task_log_dir, p.run_ts)

    # 5. 自举 schema
    schema_created = schema.ensure_schema(p.schema_path)

    # 6. 自举 portable-timeout
    pt_path, pt_created = ensure_portable_timeout(home)

    # 7. session 识别
    sid, tx, src = session.detect_session(p.proj_key, home=home)

    # 8. sanity check：cc projects 目录
    if not (home / ".claude" / "projects" / p.proj_key).is_dir():
        _io.warn(
            f"cc projects 目录不存在：{home / '.claude' / 'projects' / p.proj_key}，"
            "session 串联可能受限"
        )

    if schema_created:
        _io.info(f"已写入 review schema：{p.schema_path}")
    if pt_created:
        _io.info(f"已写入 portable-timeout wrapper：{pt_path}")

    mode = "auto" if args.auto else "interactive"

    # 8b. auto 授权：仅 --auto 时给项目 .claude/settings.json 授足够权限（不阻塞 init）
    auto_auth: dict | None = None
    if args.auto:
        try:
            auto_auth = _settings_auth.grant_auto_permissions(p.repo_root)
            if auto_auth.get("ok"):
                _io.info(f"已为 auto 模式授权：{auto_auth['path']}")
            else:
                _io.warn(f"auto 授权跳过（{auto_auth.get('skipped')}）：{auto_auth.get('path')}")
        except OSError as e:
            _io.warn(f"auto 授权失败（不阻塞 init）：{e}")
            auto_auth = {"ok": False, "error": str(e)}

    # 9. state_drift 扫描（仅 needs_resume 时执行）
    state_drift: dict | None = None
    if needs_resume and resume_state_json is not None:
        try:
            old_state = _state.read_state(resume_state_json)
            state_drift = _git_chain.scan_state_drift(p.repo_root, old_state)
        except (OSError, json.JSONDecodeError) as e:
            _io.warn(f"state_drift 扫描跳过：旧 state 不可读 ({e})")
            state_drift = None
        except RuntimeError as e:
            # git 缺失等：不阻塞 init
            _io.warn(f"state_drift 扫描失败：{e}")
            state_drift = None

    payload = {
        "repo_root": str(p.repo_root),
        "proj_key": p.proj_key,
        "task_log_dir": str(p.task_log_dir),
        "run_ts": p.run_ts,
        "run_dir": str(p.run_dir),
        "state_json": str(p.state_json),
        "state_md": str(p.state_md),
        "index_file": str(p.index_file),
        "schema_path": str(p.schema_path),
        "run_events": str(p.run_events),
        "run_json": str(run_json_path),
        "active_json": str(_paths.active_json_path_for(p.task_log_dir)),
        "session_id": sid,
        "transcript_path": tx,
        "session_source": src,
        "needs_resume": needs_resume,
        "resume_state_json": str(resume_state_json) if resume_state_json else None,
        "state_drift": state_drift,
        "mode": mode,
        "fresh": bool(args.fresh),
        "auto_auth": auto_auth,
    }

    if args.shell_exports:
        _io.warn(
            "--shell-exports 已 deprecated（v0.2）；"
            "子命令现已自包含，无需再 eval 导出环境变量。"
        )
        _emit_shell_exports(payload)
    else:
        _io.emit(payload)
