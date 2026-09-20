"""Durable progress checks; the host delivers inquiries and owns interventions.

Registration is explicit so analysts, external worktrees and hosts without
discoverable transcripts have the same contract. Activity is not progress.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import time

from . import _io, paths
from .task import _atomic_write_json


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
        _atomic_write_json(file, doc)


def evidence(agent: dict, repo_root: Path) -> dict:
    """Hash dedicated deliverables; never count transcripts or shared target HEAD.

    These are evidence of work, not proof of useful convergence. Periodic host
    inquiries still run even when these fingerprints change.
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
    if worktree and Path(worktree).resolve() != repo_root.resolve():
        for key, args in (("head", ["rev-parse", "HEAD"]),
                          ("diff", ["diff", "--binary", "HEAD"])):
            proc = subprocess.run(["git", "-C", worktree, *args], capture_output=True,
                                  timeout=10, check=False)
            if proc.returncode == 0:
                result[key] = hashlib.sha256(proc.stdout).hexdigest()
    return result


def register(doc: dict, *, agent_id: str, role: str, handle: str,
             worktree: str | None, artifacts: list[str], repo_root: Path, now: float) -> dict:
    if doc["stopped"]:
        raise ValueError("monitor 已停止；请使用新 run，不能静默复活旧任务")
    spec = dict(role=role, handle=handle, worktree=worktree, artifacts=artifacts)
    old = doc["agents"].get(agent_id)
    if old:
        if any(old[k] != v for k, v in spec.items()):
            raise ValueError("agent id 已登记为另一任务；接替任务须使用新 id")
        return old  # resume does not reset clocks or discard outstanding actions
    row = dict(spec, status="active", registered_at=now, progress_at=now,
               inquiry_at=now, evidence=evidence(spec, repo_root), pending=None,
               wait_until=0, history=[])
    doc["agents"][agent_id] = row
    return row


def _action(doc: dict, agent_id: str, row: dict, kind: str, now: float) -> None:
    doc["serial"] += 1
    row["pending"] = dict(id=str(doc["serial"]), agent_id=agent_id,
                          handle=row["handle"], role=row["role"], kind=kind,
                          created_at=now, sent_at=None)


def tick(doc: dict, *, now: float, repo_root: Path, inquiry_seconds: int = 600,
         stalled_seconds: int = 900, grace_seconds: int = 300) -> dict:
    actions = []
    if doc["stopped"]:
        return dict(ok=True, stopped=True, active=0, actions=[])
    active = 0
    for agent_id, row in doc["agents"].items():
        if row["status"] != "active":
            continue
        active += 1
        try:
            current = evidence(row, repo_root)
            # Deletion/unavailability alone is not progress.
            if any(row["evidence"].get(k) != v for k, v in current.items()):
                row["progress_at"] = now
            row["evidence"].update(current)
            row.pop("observation_error", None)
        except (OSError, subprocess.TimeoutExpired) as exc:
            row["observation_error"] = str(exc)
        pending = row["pending"]
        if now < row["wait_until"]:
            continue
        if row["wait_until"]:
            row["wait_until"] = 0
            if not pending:
                _action(doc, agent_id, row, "CHECK_IN", now)
                pending = row["pending"]
        if pending and pending["kind"] == "CHECK_IN" and pending["sent_at"] is not None:
            if now - pending["sent_at"] >= grace_seconds:
                # A request still needs a host decision even if artifacts changed.
                row["history"].append(dict(pending, outcome="reply-deadline"))
                _action(doc, agent_id, row, "CONTROL_REQUIRED", now)
        elif not pending and now - row["inquiry_at"] >= inquiry_seconds:
            _action(doc, agent_id, row, "CHECK_IN", now)
        if row["pending"]:
            actions.append(dict(row["pending"],
                                no_progress=now - row["progress_at"] >= stalled_seconds,
                                progress_age_seconds=max(0, int(now - row["progress_at"])),
                                observation_error=row.get("observation_error")))
    return dict(ok=True, stopped=False, active=active, actions=actions)


def acknowledge(doc: dict, *, action_id: str, decision: str, note: str,
                now: float, wait_seconds: int = 0) -> None:
    for row in doc["agents"].values():
        pending = row["pending"]
        if not pending or pending["id"] != action_id:
            continue
        if decision == "sent":
            if pending["kind"] != "CHECK_IN":
                raise ValueError("CONTROL_REQUIRED 必须记录工程判断，不能仅标为 sent")
            if pending["sent_at"] is None:
                pending["sent_at"] = now
                row["inquiry_at"] = now
            return
        if not note.strip():
            raise ValueError("判断必须包含证据、原因或下一步")
        if pending["kind"] == "CHECK_IN" and pending["sent_at"] is None:
            raise ValueError("先实际询问 agent，再 ack sent；不能将未发送询问当已处理")
        if decision == "wait" and not 1 <= wait_seconds <= 900:
            raise ValueError("wait 必须指定 1–900 秒的有界期限")
        row["history"].append(dict(pending, outcome=decision, note=note, decided_at=now))
        row["pending"] = None
        row["inquiry_at"] = now
        row["wait_until"] = now + wait_seconds if decision == "wait" else 0
        # Self-reported status/ack never resets the objective evidence clock.
        return
    raise ValueError("找不到待处理 action id（可能已经处理）")


def run(args: argparse.Namespace) -> None:
    try:
        p = paths.load_paths(args)
        command = args.monitor_cmd
        if command == "follow":
            # Single long-lived emitter. Tick/ack may safely run concurrently.
            p.run_dir.mkdir(parents=True, exist_ok=True)
            with (p.run_dir / ".monitor-follow.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("本 run 已有 monitor follow；重新连接原监控") from None
                previous = None
                last_emit = 0.0
                while True:
                    result = _execute(p, args, "tick")
                    signature = (result["stopped"], result["active"],
                                 [(a["id"], a["kind"], a["sent_at"], a["no_progress"],
                                   a["observation_error"]) for a in result["actions"]])
                    if signature != previous or time.monotonic() - last_emit >= args.inquiry_seconds:
                        _io.emit(result)
                        previous, last_emit = signature, time.monotonic()
                    if result["stopped"]:
                        return
                    time.sleep(args.interval)
        else:
            _io.emit(_execute(p, args, command))
    except (paths.PathsError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _io.emit_error("monitor_failed", str(exc), exit_code=3)
    except KeyboardInterrupt:
        return


def _execute(p: paths.Paths, args: argparse.Namespace, command: str) -> dict:
    now = time.time()
    with checkpoint(p.run_dir) as doc:
        if command == "register":
            register(doc, agent_id=args.id, role=args.role, handle=args.handle,
                     worktree=str(Path(args.worktree).resolve()) if args.worktree else None,
                     artifacts=[str(Path(a).resolve()) for a in args.artifact],
                     repo_root=p.repo_root, now=now)
        elif command == "ack":
            acknowledge(doc, action_id=args.action_id, decision=args.decision,
                        note=args.note, now=now, wait_seconds=args.wait_seconds)
        elif command == "finish":
            if args.id not in doc["agents"]:
                raise ValueError("未知 agent id")
            if not args.note.strip():
                raise ValueError("finish 需要已核实退出/收单的证据")
            row = doc["agents"][args.id]
            row["history"].append(dict(outcome="finished", note=args.note, at=now,
                                       pending=row["pending"]))
            row.update(status="finished", pending=None)
        elif command == "stop":
            if any(r["status"] == "active" for r in doc["agents"].values()):
                raise ValueError("仍有 active agent；先核实退出并 finish，再停止 monitor")
            doc["stopped"] = True
        if command == "tick":
            return tick(doc, now=now, repo_root=p.repo_root,
                        inquiry_seconds=args.inquiry_seconds,
                        stalled_seconds=args.stalled_seconds, grace_seconds=args.grace_seconds)
        return dict(ok=True, command=command, checkpoint=str(p.run_dir / "monitor.json"))


def add_parser(sub) -> None:
    parser = sub.add_parser("monitor", help="全角色进展监控、周期询问与工程干预回执")
    commands = parser.add_subparsers(dest="monitor_cmd", required=True)
    for name in ("tick", "follow", "register", "ack", "finish", "stop"):
        p = commands.add_parser(name)
        p.set_defaults(handler=run, _cmd_path=f"monitor {name}")
        if name in {"tick", "follow"}:
            p.add_argument("--inquiry-seconds", type=_positive, default=600)
            p.add_argument("--stalled-seconds", type=_positive, default=900)
            p.add_argument("--grace-seconds", type=_positive, default=300)
        if name == "follow":
            p.add_argument("--interval", type=_positive, default=60)
        if name in {"register", "finish"}:
            p.add_argument("--id", required=True)
        if name == "register":
            p.add_argument("--role", required=True)
            p.add_argument("--handle", required=True)
            p.add_argument("--worktree")
            p.add_argument("--artifact", action="append", default=[])
        if name == "ack":
            p.add_argument("--action-id", required=True)
            p.add_argument("--decision", choices=["sent", "progress", "wait", "intervene"], required=True)
            p.add_argument("--wait-seconds", type=int, default=0)
        if name in {"ack", "finish"}:
            p.add_argument("--note", default="")


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number
