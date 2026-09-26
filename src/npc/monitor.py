"""Durable progress checks; the host delivers inquiries and owns interventions.

Registration is explicit so analysts, external worktrees and hosts without
discoverable transcripts have the same contract. Activity is not progress.

Agent tasks are asked periodically. Job tasks (remote runs, background
commands) have nobody to ask, so a lack of evidence raises STALL instead.
Terminal observations (DONE_SIGNAL / EXITED_SIGNAL / DEADLINE) never close a
task: only the host's finish/cancel does, after verifying the evidence.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import _io, paths, verify
from .task import TASK_ID_RE, _atomic_write_json

KINDS = ("agent", "job")
OPEN = frozenset({"active", "signaled"})
SIGNALS = frozenset({"DONE_SIGNAL", "EXITED_SIGNAL", "DEADLINE"})
# Consecutive failed liveness checks before EXITED_SIGNAL; one failure may be a
# transient connection error rather than an exit.
ALIVE_FAILURES = 2
DEFAULT_PROBE_TIMEOUT = 30
ERROR_CHARS = 200
MARKER_CHARS = 120
# Rows written before 1.9 lack these keys; they compare equal to the defaults.
SPEC_DEFAULTS = dict(kind="agent", probe=None, done=None, alive=None, deadline_seconds=None)


class ObservationError(Exception):
    """A probe command ran but could not produce a progress marker."""


@contextmanager
def checkpoint(run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ".monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        file = run_dir / "monitor.json"
        doc = json.loads(file.read_text()) if file.exists() else {
            "schema_version": 1, "agents": {}, "serial": 0, "stopped": False,
        }
        yield doc
        # Decisions go to an append-only journal so the snapshot stays small.
        journal = doc.pop("_journal", [])
        if journal:
            with (run_dir / "monitor.history.jsonl").open("a", encoding="utf-8") as f:
                for entry in journal:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _atomic_write_json(file, doc)


def _log(doc: dict, agent_id: str, entry: dict) -> None:
    doc.setdefault("_journal", []).append(dict(entry, agent_id=agent_id))


def _short(value) -> str:
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value)
    text = " ".join(text.split())
    return text[-ERROR_CHARS:]


def _run(command: str, cwd: str | None, timeout: int) -> subprocess.CompletedProcess:
    """Run a check command; the whole process group is reaped so a lingering ssh
    child can neither outlive the timeout nor hold the output open."""
    argv = ["/bin/sh", "-c", command]
    if not hasattr(os, "waitid") or not hasattr(os, "killpg"):
        return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, check=False)
    proc, timed_out = verify.run_in_group(argv, cwd, timeout)
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout)
    return proc


def evidence(agent: dict, repo_root: Path | None, *, probe: bool = True) -> tuple[dict, str | None]:
    """Hash dedicated deliverables; never count transcripts or shared target HEAD.

    These are evidence of work, not proof of useful convergence. Periodic host
    inquiries still run even when these fingerprints change. Returns the
    fingerprints and a short preview of the probe marker.
    """
    result = {}
    for name in agent.get("artifacts", []):
        path = Path(name)
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
            result[name] = digest.hexdigest()
    worktree = agent.get("worktree")
    if worktree and (repo_root is None or Path(worktree).resolve() != repo_root.resolve()):
        for key, args in (("head", ["rev-parse", "HEAD"]),
                          ("diff", ["diff", "--binary", "HEAD"])):
            proc = subprocess.run(["git", "-C", worktree, *args], capture_output=True,
                                  timeout=10, check=False)
            if proc.returncode == 0:
                result[key] = hashlib.sha256(proc.stdout).hexdigest()
    marker = None
    if probe and agent.get("probe"):
        proc = _run(agent["probe"], agent.get("cwd"),
                    agent.get("probe_timeout") or DEFAULT_PROBE_TIMEOUT)
        if proc.returncode != 0:
            raise ObservationError(f"probe exit {proc.returncode}: {_short(proc.stderr)}")
        result["probe"] = hashlib.sha256(proc.stdout.encode()).hexdigest()
        marker = _short(proc.stdout)[:MARKER_CHARS]
    return result, marker


def observe(agent: dict, repo_root: Path | None) -> dict:
    """Collect evidence and terminal checks for one active task (runs commands)."""
    obs = dict(evidence=None, marker=None, done=None, alive=None, error=None)
    try:
        obs["evidence"], obs["marker"] = evidence(agent, repo_root)
    except (OSError, subprocess.TimeoutExpired, ObservationError) as exc:
        obs["error"] = _short(exc)
    timeout = agent.get("probe_timeout") or DEFAULT_PROBE_TIMEOUT
    for key in ("done", "alive"):
        if not agent.get(key) or (key == "alive" and obs["done"]):
            continue
        try:
            obs[key] = _run(agent[key], agent.get("cwd"), timeout).returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Unknown is neither completion nor exit.
            obs["error"] = obs["error"] or f"{key}: {_short(exc)}"
    return obs


def register(doc: dict, *, agent_id: str, role: str, handle: str,
             worktree: str | None, artifacts: list[str], repo_root: Path | None, now: float,
             kind: str = "agent", probe: str | None = None, done: str | None = None,
             alive: str | None = None, deadline_seconds: int | None = None,
             probe_timeout: int = DEFAULT_PROBE_TIMEOUT, cwd: str | None = None,
             revive: bool = False) -> dict:
    if doc["stopped"]:
        if not revive:
            raise ValueError("monitor 已停止；请使用新 run，不能静默复活旧任务")
        doc["stopped"] = False
    if kind not in KINDS:
        raise ValueError(f"kind 必须为 {'/'.join(KINDS)}")
    if deadline_seconds is not None and deadline_seconds <= 0:
        raise ValueError("deadline-seconds 必须为正整数")
    if kind == "job" and not (done or alive or deadline_seconds):
        raise ValueError("job 任务至少需要 --done、--alive 或 --deadline-seconds 之一作为终态检测路径")
    spec = dict(role=role, handle=handle, worktree=worktree, artifacts=artifacts, kind=kind,
                probe=probe, done=done, alive=alive, deadline_seconds=deadline_seconds)
    old = doc["agents"].get(agent_id)
    if old:
        if any(old.get(k, SPEC_DEFAULTS.get(k)) != v for k, v in spec.items()):
            raise ValueError("agent id 已登记为另一任务；接替任务须使用新 id")
        return old  # resume does not reset clocks or discard outstanding actions
    # The probe baseline is taken on the first tick so registration never waits on it.
    row = dict(spec, status="active", registered_at=now, progress_at=now,
               inquiry_at=now, evidence=evidence(spec, repo_root, probe=False)[0], pending=None,
               wait_until=0, cwd=cwd, probe_timeout=probe_timeout,
               deadline_at=now + deadline_seconds if deadline_seconds else None)
    doc["agents"][agent_id] = row
    return row


def _action(doc: dict, agent_id: str, row: dict, kind: str, now: float) -> None:
    doc["serial"] += 1
    row["pending"] = dict(id=str(doc["serial"]), agent_id=agent_id,
                          handle=row["handle"], role=row["role"], kind=kind,
                          created_at=now, sent_at=None)


def _has_progress_evidence(row: dict) -> bool:
    return bool(row.get("artifacts") or row.get("worktree") or row.get("probe"))


def _apply(doc: dict, agent_id: str, row: dict, obs: dict, now: float) -> None:
    # A concurrent tick may already have applied a newer observation.
    started = obs.get("at", now)
    if started < row.get("observed_at", started):
        return
    row["observed_at"] = started
    if obs["evidence"] is not None:
        stored = row["evidence"]
        # Deletion/unavailability alone is not progress; the first probe reading is a baseline.
        if any(stored.get(k) != v for k, v in obs["evidence"].items()
               if not (k == "probe" and k not in stored)):
            row["progress_at"] = now
        stored.update(obs["evidence"])
        if obs["marker"] is not None:
            row["marker"] = obs["marker"]
    if obs["error"]:
        row["observation_error"] = obs["error"]
    else:
        row.pop("observation_error", None)
    signal = None
    if obs["done"]:
        signal = "DONE_SIGNAL"
    elif obs["alive"] is False:
        row["alive_failures"] = row.get("alive_failures", 0) + 1
        if row["alive_failures"] >= ALIVE_FAILURES:
            signal = "EXITED_SIGNAL"
    elif obs["alive"]:
        row["alive_failures"] = 0
    if signal is None and row.get("deadline_at") is not None and now >= row["deadline_at"]:
        signal = "DEADLINE"
    if signal:
        if row["pending"]:
            _log(doc, agent_id, dict(row["pending"], outcome="superseded", decided_at=now))
        # A terminal observation overrides a bounded wait; probing stops until the host decides.
        row.update(status="signaled", signal=signal, wait_until=0)
        _action(doc, agent_id, row, signal, now)


def _resume_kind(row: dict, now: float, stalled_seconds: int) -> str | None:
    """Action raised when a bounded wait expires without a pending action."""
    if row["status"] == "signaled":
        return row["signal"]
    if row.get("kind", "agent") == "agent":
        return "CHECK_IN"
    stalled = _has_progress_evidence(row) and now - row["progress_at"] >= stalled_seconds
    return "STALL" if stalled else None


def tick(doc: dict, *, now: float, repo_root: Path | None, observations: dict | None = None,
         inquiry_seconds: int = 600, progressing_inquiry_seconds: int = 1800,
         stalled_seconds: int = 900, grace_seconds: int = 300) -> dict:
    """Advance every open task. ``observations`` (id → observe() result) lets the
    caller run commands outside the checkpoint lock; tasks without one are not
    observed this round. Without it, observation runs inline."""
    actions = []
    if doc["stopped"]:
        return dict(ok=True, stopped=True, active=0, actions=[])
    active = 0
    for agent_id, row in doc["agents"].items():
        if row["status"] not in OPEN:
            continue
        active += 1
        if row["status"] == "active":
            obs = observe(row, repo_root) if observations is None else observations.get(agent_id)
            if obs is not None:
                _apply(doc, agent_id, row, obs, now)
        pending = row["pending"]
        if now < row["wait_until"]:
            continue
        if row["wait_until"]:
            row["wait_until"] = 0
            kind = None if pending else _resume_kind(row, now, stalled_seconds)
            if kind:
                _action(doc, agent_id, row, kind, now)
                pending = row["pending"]
        if row["status"] == "active" and row.get("kind", "agent") == "agent":
            if pending and pending["kind"] == "CHECK_IN" and pending["sent_at"] is not None:
                if now - pending["sent_at"] >= grace_seconds:
                    # A request still needs a host decision even if artifacts changed.
                    _log(doc, agent_id, dict(pending, outcome="reply-deadline", decided_at=now))
                    _action(doc, agent_id, row, "CONTROL_REQUIRED", now)
            elif not pending:
                # Recent evidence stretches the cadence; it never disables inquiry.
                quiet = now - row["progress_at"] >= inquiry_seconds
                interval = inquiry_seconds if quiet else max(inquiry_seconds,
                                                              progressing_inquiry_seconds)
                if now - row["inquiry_at"] >= interval:
                    _action(doc, agent_id, row, "CHECK_IN", now)
        elif row["status"] == "active" and not pending and _has_progress_evidence(row):
            if (now - row["progress_at"] >= stalled_seconds
                    and now - row["inquiry_at"] >= stalled_seconds):
                _action(doc, agent_id, row, "STALL", now)
        if row["pending"]:
            # Signaled tasks are no longer probed, so their evidence clock is frozen.
            quiet = row["status"] == "active" and now - row["progress_at"] >= stalled_seconds
            actions.append(dict(row["pending"], task_kind=row.get("kind", "agent"),
                                no_progress=quiet,
                                progress_age_seconds=max(0, int(now - row["progress_at"])),
                                observation_error=row.get("observation_error")))
    return dict(ok=True, stopped=False, active=active, actions=actions)


def acknowledge(doc: dict, *, action_id: str, decision: str, note: str,
                now: float, wait_seconds: int = 0, deadline_seconds: int | None = None) -> None:
    for agent_id, row in doc["agents"].items():
        pending = row["pending"]
        if not pending or pending["id"] != action_id:
            continue
        kind = pending["kind"]
        if decision == "sent":
            if kind != "CHECK_IN":
                raise ValueError(f"{kind} 必须记录工程判断，不能仅标为 sent")
            if pending["sent_at"] is None:
                pending["sent_at"] = now
                row["inquiry_at"] = now
            return
        if not note.strip():
            raise ValueError("判断必须包含证据、原因或下一步")
        if kind == "CHECK_IN" and pending["sent_at"] is None:
            raise ValueError("先实际询问 agent，再 ack sent；不能将未发送询问当已处理")
        if kind in SIGNALS and decision not in {"wait", "intervene"}:
            raise ValueError("终态信号须核验后 finish/cancel 关闭，或以 wait/intervene 记录处理")
        if decision == "wait" and not 1 <= wait_seconds <= 900:
            raise ValueError("wait 必须指定 1–900 秒的有界期限")
        if deadline_seconds is not None and deadline_seconds <= 0:
            raise ValueError("deadline-seconds 必须为正整数")
        _log(doc, agent_id, dict(pending, outcome=decision, note=note, decided_at=now))
        row["pending"] = None
        row["inquiry_at"] = now
        row["wait_until"] = now + wait_seconds if decision == "wait" else 0
        if deadline_seconds is not None:
            row["deadline_at"] = now + deadline_seconds
        elif kind == "DEADLINE" and decision == "intervene":
            row["deadline_at"] = None  # the passed deadline is handled; a new one must be explicit
        if row["status"] == "signaled" and decision == "intervene":
            # Observations collected before this decision must not re-raise the signal.
            row.update(status="active", signal=None, alive_failures=0,
                       observed_at=max(now, row.get("observed_at", now)))
        # Self-reported status/ack never resets the objective evidence clock.
        return
    raise ValueError("找不到待处理 action id（可能已经处理）")


def close(doc: dict, *, agent_id: str, status: str, note: str, now: float,
          result: str | None = None) -> bool:
    """Close a task after the host verified exit/receipt. Returns False if already closed."""
    row = doc["agents"].get(agent_id)
    if row is None:
        raise ValueError("未知 agent id")
    if not note.strip():
        raise ValueError(f"{'finish' if status == 'finished' else 'cancel'} 需要已核实退出/收单的证据")
    if row["status"] not in OPEN:
        return False
    _log(doc, agent_id, dict(outcome=status, result=result, note=note, at=now,
                             pending=row["pending"]))
    row.update(status=status, pending=None, wait_until=0)
    if result:
        row["result"] = result
    return True


def listing(doc: dict, *, now: float, open_only: bool) -> dict:
    tasks = []
    for agent_id, row in doc["agents"].items():
        if open_only and row["status"] not in OPEN:
            continue
        pending = row.get("pending")
        tasks.append(dict(
            id=agent_id, kind=row.get("kind", "agent"), role=row["role"], handle=row["handle"],
            status=row["status"], result=row.get("result"),
            pending=dict(id=pending["id"], kind=pending["kind"], sent_at=pending["sent_at"])
            if pending else None,
            progress_age_seconds=max(0, int(now - row["progress_at"])),
            deadline_at=row.get("deadline_at"), marker=row.get("marker"),
            observation_error=row.get("observation_error")))
    return dict(ok=True, stopped=doc["stopped"],
                open=sum(1 for r in doc["agents"].values() if r["status"] in OPEN), tasks=tasks)


# Actions whose next step is a message to the task's handle.
ASK_KINDS = frozenset({"CHECK_IN", "CONTROL_REQUIRED"})


def render_line(result: dict, fresh: frozenset | set = frozenset()) -> str:
    """One plain-text line for the host's context; ``list --open`` holds the detail.

    Every pending action stays on the line (the host needs its id to ack), but
    only the fields a decision needs: id, kind, task, and the handle when the
    next step is to ask. ``*`` marks actions that are new since the last line.
    """
    if result.get("stopped"):
        return "monitor stopped"
    actions = result.get("actions") or []
    head = f"monitor: {result.get('active', 0)} open, {len(actions)} pending"
    if not actions:
        return head
    items = []
    for a in actions:
        text = f"{'*' if a['id'] in fresh else ''}#{a['id']} {a['kind']} {a['agent_id']}"
        if a["kind"] in ASK_KINDS:
            text += " asked" if a.get("sent_at") is not None else f" -> {a['handle']}"
        if a.get("no_progress"):
            text += f" idle {a.get('progress_age_seconds', 0) // 60}m"
        if a.get("observation_error"):
            text += " probe-error"
        items.append(text)
    return f"{head} | {'; '.join(items)} | detail: npc monitor list --open"


def _output(result: dict, fmt: str, fresh: frozenset | set = frozenset()) -> None:
    if fmt == "line":
        sys.stdout.write(render_line(result, fresh) + "\n")
        sys.stdout.flush()
    else:
        _io.emit(result)


class Emitter:
    """Decide which follow snapshots reach the host.

    Only new actions (or an action turning no_progress) wake the host; its own
    acks, registrations and closures never do. Unresolved actions are repeated
    every ``remind_seconds``; an idle monitor stays silent. ``fresh`` holds the
    ids that caused the latest wake-up.
    """

    def __init__(self, remind_seconds: int):
        self.remind_seconds = remind_seconds
        self.seen: set = set()
        self.fresh: frozenset = frozenset()
        self.last_emit: float | None = None

    def __call__(self, result: dict, now: float) -> bool:
        items = {(a["id"], a["kind"], a["no_progress"]) for a in result["actions"]}
        fresh = items - self.seen
        self.seen = items
        self.fresh = frozenset(item[0] for item in fresh)
        due = (bool(items) and self.last_emit is not None
               and now - self.last_emit >= self.remind_seconds)
        if result["stopped"] or fresh or due:
            self.last_emit = now
            return True
        return False


def _scope(args: argparse.Namespace) -> tuple[Path, Path | None, str]:
    """Run scope by default; ``--owner`` selects a host-level registry outside any run."""
    owner = getattr(args, "owner", None)
    if owner:
        if not TASK_ID_RE.fullmatch(owner):
            raise ValueError("owner 必须匹配 [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
        try:
            repo_root = paths.detect_repo_root()
        except paths.PathsError:
            repo_root = None
        return Path.home() / "task_log" / "_monitor" / owner, repo_root, "owner"
    p = paths.load_paths(args)
    return p.run_dir, p.repo_root, "run"


def _tick(directory: Path, repo_root: Path | None, args: argparse.Namespace) -> dict:
    # Probes may be slow (ssh); run them outside the lock so ack/register never wait on them.
    with checkpoint(directory) as doc:
        rows = {} if doc["stopped"] else {
            k: dict(v) for k, v in doc["agents"].items() if v["status"] == "active"}
    started = time.time()
    observations = {k: dict(observe(v, repo_root), at=started) for k, v in rows.items()}
    with checkpoint(directory) as doc:
        return tick(doc, now=time.time(), repo_root=repo_root, observations=observations,
                    inquiry_seconds=args.inquiry_seconds,
                    progressing_inquiry_seconds=args.progressing_inquiry_seconds,
                    stalled_seconds=args.stalled_seconds, grace_seconds=args.grace_seconds)


def run(args: argparse.Namespace) -> None:
    try:
        directory, repo_root, scope = _scope(args)
        command = args.monitor_cmd
        if command == "follow":
            # Single long-lived emitter. Tick/ack may safely run concurrently.
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / ".monitor-follow.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("本作用域已有 monitor follow；重新连接原监控") from None
                emit = Emitter(args.remind_seconds)
                while True:
                    result = _tick(directory, repo_root, args)
                    if emit(result, time.monotonic()):
                        _output(result, args.format, emit.fresh)
                    if result["stopped"]:
                        return
                    time.sleep(args.interval)
        elif command == "tick":
            _output(_tick(directory, repo_root, args), args.format)
        else:
            _io.emit(dict(_execute(directory, repo_root, scope, args, command), scope=scope))
    except (paths.PathsError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _io.emit_error("monitor_failed", str(exc), exit_code=3)
    except KeyboardInterrupt:
        return


def _execute(directory: Path, repo_root: Path | None, scope: str,
             args: argparse.Namespace, command: str) -> dict:
    now = time.time()
    with checkpoint(directory) as doc:
        if command == "register":
            register(doc, agent_id=args.id, role=args.role, handle=args.handle,
                     worktree=str(Path(args.worktree).resolve()) if args.worktree else None,
                     artifacts=[str(Path(a).resolve()) for a in args.artifact],
                     repo_root=repo_root, now=now, kind=args.kind, probe=args.probe,
                     done=args.done, alive=args.alive, deadline_seconds=args.deadline_seconds,
                     probe_timeout=args.probe_timeout, cwd=str(Path.cwd()),
                     revive=scope == "owner")
        elif command == "ack":
            acknowledge(doc, action_id=args.action_id, decision=args.decision,
                        note=args.note, now=now, wait_seconds=args.wait_seconds,
                        deadline_seconds=args.deadline_seconds)
        elif command in ("finish", "cancel"):
            closed = close(doc, agent_id=args.id, now=now, note=args.note,
                           status="finished" if command == "finish" else "cancelled",
                           result=getattr(args, "result", None))
            if not closed:
                return dict(ok=True, command=command, already_closed=True,
                            checkpoint=str(directory / "monitor.json"))
        elif command == "list":
            return listing(doc, now=now, open_only=args.open)
        elif command == "stop":
            if any(r["status"] in OPEN for r in doc["agents"].values()):
                raise ValueError("仍有未关闭任务；先核实并 finish/cancel，再停止 monitor")
            doc["stopped"] = True
        return dict(ok=True, command=command, checkpoint=str(directory / "monitor.json"))


def add_parser(sub) -> None:
    parser = sub.add_parser("monitor", help="全角色进展监控、终态信号、周期询问与工程干预回执")
    commands = parser.add_subparsers(dest="monitor_cmd", required=True)
    helps = dict(
        tick="执行一次检查并输出待处理动作（初始化/恢复/补算）",
        follow="单实例后台检查，只在出现新动作或提醒到期时输出一行 JSON",
        register="登记 agent 或 job 任务及其证据/终态检测命令",
        ack="记录对待处理动作的发送或工程判断",
        finish="核实后关闭任务（结果 done/failed）",
        cancel="核实后取消任务",
        list="列出任务状态（--open 只看未关闭）",
        stop="全部任务关闭后停止监控，follow 随之退出",
    )
    for name, text in helps.items():
        p = commands.add_parser(name, help=text)
        p.set_defaults(handler=run, _cmd_path=f"monitor {name}")
        p.add_argument("--owner", default=None,
                       help="host 作用域名称（~/task_log/_monitor/<owner>），不依赖 run")
        if name in {"tick", "follow"}:
            p.add_argument("--inquiry-seconds", type=_positive, default=600)
            p.add_argument("--progressing-inquiry-seconds", type=_positive, default=1800,
                           help="上一询问周期内证据有变化的 agent 的询问间隔")
            p.add_argument("--stalled-seconds", type=_positive, default=900)
            p.add_argument("--grace-seconds", type=_positive, default=300)
            p.add_argument("--format", choices=["line", "json"],
                           default="json",
                           help="json：完整结构（默认，stdout 契约）；line：一行纯文本，供宿主 context 使用")
        if name == "follow":
            p.add_argument("--interval", type=_positive, default=60)
            p.add_argument("--remind-seconds", type=_positive, default=1800,
                           help="未处理动作的重复提醒间隔")
        if name in {"register", "finish", "cancel"}:
            p.add_argument("--id", required=True)
        if name == "register":
            p.add_argument("--role", required=True)
            p.add_argument("--handle", required=True)
            p.add_argument("--kind", choices=KINDS, default="agent")
            p.add_argument("--worktree")
            p.add_argument("--artifact", action="append", default=[])
            p.add_argument("--probe", help="输出进度标记的命令；输出变化计为进展")
            p.add_argument("--done", help="退出码 0 表示完成（DONE_SIGNAL）")
            p.add_argument("--alive", help="退出码 0 表示仍在运行；连续失败为 EXITED_SIGNAL")
            p.add_argument("--deadline-seconds", type=_positive, default=None)
            p.add_argument("--probe-timeout", type=_positive, default=DEFAULT_PROBE_TIMEOUT)
        if name == "ack":
            p.add_argument("--action-id", required=True)
            p.add_argument("--decision", choices=["sent", "progress", "wait", "intervene"], required=True)
            p.add_argument("--wait-seconds", type=int, default=0)
            p.add_argument("--deadline-seconds", type=_positive, default=None,
                           help="以当前时间为起点重设截止时间")
        if name == "finish":
            p.add_argument("--result", choices=["done", "failed"], default="done")
        if name in {"ack", "finish", "cancel"}:
            p.add_argument("--note", default="")
        if name == "list":
            p.add_argument("--open", action="store_true")


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number
