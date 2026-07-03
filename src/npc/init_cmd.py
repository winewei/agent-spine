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
from . import git_ops as _git_ops


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


def _get_current_branch(repo_root: Path, runner=subprocess.run) -> str:
    """获取当前分支名（detached HEAD 时返回空字符串）。"""
    import os as _os
    env = dict(_os.environ)
    env["LC_ALL"] = "C"
    proc = runner(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
    )
    val = (proc.stdout or "").strip()
    return "" if val == "HEAD" else val


def _mark_initializing_skeleton_orphan(init_file: Path) -> None:
    """将 initializing 骨架文件的 status 更新为 'orphan'。

    worktree 缺失/残破时调用，使 clean 命令可以发现并回收该记录。
    写入失败时静默忽略（保守：不因孤儿标记失败而中断 init）。
    """
    try:
        data = json.loads(init_file.read_text(encoding="utf-8"))
        data["status"] = "orphan"
        init_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def _scan_spine_worktrees_for_resume(
    canonical_repo_root: Path,
    home: Path,
    runner=subprocess.run,
) -> tuple[bool, Path | None, bool]:
    """扫描 spine/* worktree，查找有 in-progress 或 initializing state 的悬空 worktree。

    返回 (needs_resume, worktree_root_or_None, is_initializing)。
    - needs_resume=True + is_initializing=False：in-progress 续跑，worktree_root 已有 plan state。
    - needs_resume=True + is_initializing=True：initializing 崩溃恢复，worktree 完好但
      init-run 未执行，调用方应复用该 worktree_root 重新走 init 流程（跳过 worktree 创建）。
    多个命中取 state mtime 最新（in-progress 优先于 initializing）。

    副作用：发现 worktree 缺失/残破的 initializing 记录时，将骨架文件的 status 更新为
    'orphan'（记录在案），以便后续 clean 命令可以发现并回收，同时 init 继续新建 worktree。
    """
    try:
        worktrees = _git_ops.list_worktrees(canonical_repo_root, runner=runner)
    except _git_ops.WorktreeError:
        return False, None, False

    in_progress_candidates: list[tuple[float, Path]] = []
    initializing_candidates: list[tuple[float, Path]] = []

    # 构建 git worktree 路径集合，用于反向扫描
    known_wt_dirs: set[Path] = set()

    for wt in worktrees:
        branch = wt.get("branch", "")
        # branch 形如 refs/heads/spine/<run_ts>
        if not branch.startswith("refs/heads/spine/"):
            continue
        wt_path = Path(wt["path"])
        known_wt_dirs.add(wt_path)
        if not wt_path.is_dir():
            # Task 2.2: worktree 在 git 列表中但目录已缺失/残破 →
            # 检查 task_log 里是否有对应 initializing 骨架，标记孤儿
            try:
                wt_proj_key = _paths.proj_key_for(wt_path)
            except _paths.PathsError:
                continue
            wt_task_log_dir = home / "task_log" / wt_proj_key
            init_file = resume.find_latest_initializing(wt_task_log_dir)
            if init_file is not None:
                _mark_initializing_skeleton_orphan(init_file)
            continue
        # 按 worktree 路径推 task_log_dir
        try:
            wt_proj_key = _paths.proj_key_for(wt_path)
        except _paths.PathsError:
            continue
        wt_task_log_dir = home / "task_log" / wt_proj_key

        # 先检查 in-progress（优先级高于 initializing）
        state_file = resume.find_latest_in_progress(wt_task_log_dir)
        if state_file is not None:
            try:
                mtime = state_file.stat().st_mtime
            except OSError:
                mtime = 0.0
            in_progress_candidates.append((mtime, wt_path))
            continue

        # 再检查 initializing（崩溃窗口中间态）
        init_file = resume.find_latest_initializing(wt_task_log_dir)
        if init_file is not None:
            try:
                mtime = init_file.stat().st_mtime
            except OSError:
                mtime = 0.0
            initializing_candidates.append((mtime, wt_path))

    # Task 2.2 反向扫描：遍历 home/task_log/* 找 initializing 骨架，
    # 若骨架的 worktree_root 不在 git 列表中（骨架在 worktree add 之前/之后写入
    # 但 worktree add 从未完成），标记为孤儿。
    task_log_root = home / "task_log"
    if task_log_root.is_dir():
        for tl_dir in task_log_root.iterdir():
            if not tl_dir.is_dir():
                continue
            init_file = resume.find_latest_initializing(tl_dir)
            if init_file is None:
                continue
            try:
                skel = json.loads(init_file.read_text(encoding="utf-8"))
                wt_root_str = skel.get("worktree_root")
            except (OSError, json.JSONDecodeError):
                continue
            if not wt_root_str:
                continue
            wt_root = Path(wt_root_str)
            # 如果 worktree 不在 git 列表里且目录不存在 → 标记孤儿
            if wt_root not in known_wt_dirs and not wt_root.is_dir():
                _mark_initializing_skeleton_orphan(init_file)

    # in-progress 优先
    if in_progress_candidates:
        in_progress_candidates.sort(reverse=True)
        return True, in_progress_candidates[0][1], False

    if initializing_candidates:
        initializing_candidates.sort(reverse=True)
        return True, initializing_candidates[0][1], True

    return False, None, False


def run(args: argparse.Namespace, runner=subprocess.run) -> None:
    """init 主入口。"""
    home = Path.home()
    no_worktree: bool = getattr(args, "no_worktree", False)

    # 1. 探测 git repo（canonical repo root，init 从主 checkout 运行）
    try:
        canonical_repo_root = _paths.detect_repo_root()
    except _paths.PathsError as e:
        _io.emit_error("not_git_repo", str(e), exit_code=3)
        return

    canonical_proj_key = _paths.proj_key_for(canonical_repo_root)

    # 2. worktree 模式：建新 worktree 前先续跑扫描
    worktree_root: Path | None = None
    spine_branch: str | None = None
    base_branch: str | None = None
    run_ts_for_branch: str | None = None  # worktree 模式下与 spine_branch 一致
    repo_root: Path = canonical_repo_root  # 默认值，step 4 可能覆盖

    if not no_worktree and not args.fresh:
        needs_resume_wt, resume_wt_path, is_initializing = _scan_spine_worktrees_for_resume(
            canonical_repo_root, home, runner=runner
        )
        if needs_resume_wt and resume_wt_path is not None:
            if not is_initializing:
                # 命中悬空 in-progress spine worktree → 续跑，不新建
                _io.emit({
                    "needs_resume": True,
                    "worktree_root": str(resume_wt_path),
                    "canonical_proj_key": canonical_proj_key,
                })
                return
            else:
                # 命中 initializing 崩溃中间态 → 复用该 worktree，跳过创建步骤
                worktree_root = resume_wt_path
                repo_root = worktree_root
                # 从骨架文件读回 spine_branch / run_ts_for_branch
                _recovered = False
                try:
                    wt_proj_key_tmp = _paths.proj_key_for(resume_wt_path)
                    wt_task_log_tmp = home / "task_log" / wt_proj_key_tmp
                    init_file = resume.find_latest_initializing(wt_task_log_tmp)
                    if init_file is not None:
                        skel = json.loads(init_file.read_text(encoding="utf-8"))
                        run_ts_for_branch = skel.get("run_ts") or ""
                        spine_branch = skel.get("spine_branch") or (
                            f"spine/{run_ts_for_branch}" if run_ts_for_branch else None
                        )
                        base_branch = skel.get("base_branch")
                        _recovered = True
                except (OSError, json.JSONDecodeError, _paths.PathsError):
                    pass
                if not _recovered:
                    # 骨架不可读时回退：从 git worktree list 推断
                    try:
                        for wt_entry in _git_ops.list_worktrees(canonical_repo_root, runner=runner):
                            if Path(wt_entry["path"]) == resume_wt_path:
                                branch_ref = wt_entry.get("branch", "")
                                if branch_ref.startswith("refs/heads/spine/"):
                                    spine_branch = branch_ref[len("refs/heads/"):]
                                    run_ts_for_branch = spine_branch.split("spine/", 1)[1]
                                break
                    except _git_ops.WorktreeError:
                        pass

    # 3. 续跑探测：仅 --no-worktree 时检查 canonical task_log。
    #    worktree 模式下悬空扫描（步骤 2）若未命中，直接建新 worktree + needs_resume=false；
    #    不能用 canonical task_log 的旧 run_ts 来键入新 worktree，否则 state 路径错位。
    task_log_dir_for_resume = home / "task_log" / canonical_proj_key
    resume_state_json: Path | None = None
    needs_resume = False
    if not args.fresh and no_worktree:
        if task_log_dir_for_resume.is_dir():
            resume_state_json = resume.find_latest_in_progress(task_log_dir_for_resume)
            needs_resume = resume_state_json is not None

    # 4. worktree 模式：创建 worktree + 分支（跳过条件：已从 initializing 恢复）
    if not no_worktree and worktree_root is None:
        run_ts_for_branch = _paths.make_run_ts()
        spine_branch = f"spine/{run_ts_for_branch}"
        base_branch = _get_current_branch(canonical_repo_root, runner=runner)
        worktree_dir = home / ".spine" / "worktrees" / canonical_proj_key / run_ts_for_branch

        # 意向落盘（骨架）：在建 worktree 前写 plan-state skeleton（status=initializing），
        # 使「worktree 已建但 init-run 未执行」的崩溃窗口有可被续跑扫描发现的记录。
        _wt_proj_key = _paths.proj_key_for(worktree_dir)
        _wt_task_log = home / "task_log" / _wt_proj_key
        _wt_task_log.mkdir(parents=True, exist_ok=True)
        _skeleton_path = _wt_task_log / f"{run_ts_for_branch}-plan-state.json"
        _skeleton: dict = {
            "schema_version": 2,
            "run_ts": run_ts_for_branch,
            "status": "initializing",
            "worktree_root": str(worktree_dir),
            "spine_branch": spine_branch,
            "base_branch": base_branch,
            "plan_order": [],
            "progress": [],
        }
        _skeleton_path.write_text(
            json.dumps(_skeleton, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        try:
            _git_ops.add_worktree(
                canonical_repo_root,
                path=worktree_dir,
                branch=spine_branch,
                base_ref="HEAD",
                runner=runner,
            )
        except _git_ops.WorktreeError as e:
            _io.emit_error("worktree_create_failed", str(e), exit_code=3)
            return
        worktree_root = worktree_dir
        repo_root = worktree_root
    elif not no_worktree and worktree_root is not None:
        # initializing 恢复：repo_root 和 worktree_root 已在 step 2 设置
        pass
    else:
        repo_root = canonical_repo_root

    # 5. 决定本次使用的 run_ts / paths
    if needs_resume and resume_state_json is not None:
        # 复用旧 run 的 ts
        try:
            old = json.loads(resume_state_json.read_text(encoding="utf-8"))
            old_run_ts = old.get("run_ts") or resume_state_json.name.replace("-plan-state.json", "")
        except (OSError, json.JSONDecodeError):
            old_run_ts = resume_state_json.name.replace("-plan-state.json", "")
        p = _paths.compute_paths(repo_root, run_ts=old_run_ts, home=home)
    else:
        if not no_worktree:
            # worktree 模式用已生成的 run_ts（与 spine_branch 一致）；
            # run_ts_for_branch 在 step 2（initializing 恢复）或 step 4（新建）时赋值。
            # 若两者均未设置（不应发生），generate_ts 作为保底。
            p = _paths.compute_paths(repo_root, run_ts=run_ts_for_branch or None, home=home)
        else:
            p = _paths.compute_paths(repo_root, home=home)

    # 如有 worktree 回指字段，构造带回指的 Paths
    from dataclasses import replace as _dc_replace
    if not no_worktree and worktree_root is not None:
        p = _dc_replace(
            p,
            canonical_repo_root=canonical_repo_root,
            canonical_proj_key=canonical_proj_key,
            base_branch=base_branch,
            spine_branch=spine_branch,
        )

    # 5b. 把 mode 落入 Paths（run.json 持久化后 record 阶段可读，无需 NPC_MODE env）
    _init_mode = "auto" if args.auto else "interactive"
    p = _dc_replace(p, mode=_init_mode)

    # 6. 确保目录
    _paths.ensure_dirs(p)

    # 6b. 落 run.json + active.json（v0.2 起作为子命令默认 resolve 入口）
    run_json_path = _paths.write_run_json(p)
    _paths.set_active(p.task_log_dir, p.run_ts)

    # 7. 自举 schema
    schema_created = schema.ensure_schema(p.schema_path)

    # 8. 自举 portable-timeout
    pt_path, pt_created = ensure_portable_timeout(home)

    # 9. session 识别
    sid, tx, src = session.detect_session(p.proj_key, home=home)

    # 10. sanity check：cc projects 目录
    if not (home / ".claude" / "projects" / p.proj_key).is_dir():
        _io.warn(
            f"cc projects 目录不存在：{home / '.claude' / 'projects' / p.proj_key}，"
            "session 串联可能受限"
        )

    if schema_created:
        _io.info(f"已写入 review schema：{p.schema_path}")
    if pt_created:
        _io.info(f"已写入 portable-timeout wrapper：{pt_path}")

    mode = _init_mode  # 已在步骤 5b 计算并落入 run.json

    # 10b. auto 授权：仅 --auto 时把项目授权写到主 checkout（live session 真正读取
    #      settings 的位置），而非 worktree（其 settings.json 不被 cwd 会话加载）。
    #      两处落盘：
    #      - settings.json：defaultMode=acceptEdits + harness Bash 白名单（可共享）。
    #      - settings.local.json：worktree 根 / task_log 等 cwd 外受信目录（机器专属
    #        绝对路径，gitignore，绝不污染可提交的 settings.json）。
    #      不阻塞 init。
    auto_auth: dict | None = None
    auto_local: dict | None = None
    if args.auto:
        try:
            auto_auth = _settings_auth.grant_auto_permissions(canonical_repo_root)
            if auto_auth.get("ok"):
                _io.info(f"已为 auto 模式授权：{auto_auth['path']}")
            else:
                _io.warn(f"auto 授权跳过（{auto_auth.get('skipped')}）：{auto_auth.get('path')}")
        except OSError as e:
            _io.warn(f"auto 授权失败（不阻塞 init）：{e}")
            auto_auth = {"ok": False, "error": str(e)}
        try:
            auto_local = _settings_auth.grant_auto_local_dirs(canonical_repo_root, home=home)
            if auto_local.get("ok"):
                _io.info(f"已授信 cwd 外目录（本地）：{auto_local['path']}")
            else:
                _io.warn(
                    f"cwd 外目录授信跳过（{auto_local.get('skipped')}）：{auto_local.get('path')}"
                )
        except OSError as e:
            _io.warn(f"cwd 外目录授信失败（不阻塞 init）：{e}")
            auto_local = {"ok": False, "error": str(e)}

    # 11. state_drift 扫描（仅 needs_resume 时执行）
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
        "auto_local_dirs": auto_local,
        # worktree 回指字段（--no-worktree 时为 null）
        "worktree_root": str(worktree_root) if worktree_root else None,
        "spine_branch": spine_branch,
        "canonical_proj_key": canonical_proj_key,
        "canonical_repo_root": str(canonical_repo_root) if not no_worktree else None,
    }

    if args.shell_exports:
        _io.warn(
            "--shell-exports 已 deprecated（v0.2）；"
            "子命令现已自包含，无需再 eval 导出环境变量。"
        )
        _emit_shell_exports(payload)
    else:
        _io.emit(payload)
