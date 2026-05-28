"""路径计算与运行时元数据装载。

v0.2 起，所有派生路径由 `npc init` 同时落盘到两份持久化文件：

- ``<run_dir>/run.json``：本次 run 的 deterministic 元数据（repo_root / proj_key /
  各派生路径）。一次写入、不可变。
- ``<task_log_dir>/active.json``：指向当前 active run_ts 的指针，便于后续子命令
  在不依赖 shell env 的情况下找到本次 run。

子命令的 resolve 顺序（高→低）：

1. 显式参数（``--run-ts`` / ``--state-json`` / ``--task-log-dir``）
2. cwd → repo_root → task_log_dir → ``active.json`` → ``run.json``
3. NPC_* 环境变量（v0.1 兼容路径，会发 deprecation warning）

任何一级失败都向下回退；全部失败时抛 ``PathsError``。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SCHEMA_FILENAME = ".new-plan-review-schema.json"
SESSION_CACHE_REL = ".session-cache"
RUN_JSON_FILENAME = "run.json"
ACTIVE_JSON_FILENAME = "active.json"
RUN_JSON_SCHEMA_VERSION = 1
ACTIVE_JSON_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Paths:
    """本次 run 所需的所有路径与 key。"""

    repo_root: Path
    proj_key: str
    task_log_dir: Path
    run_ts: str
    run_dir: Path
    state_json: Path
    state_md: Path
    index_file: Path
    schema_path: Path
    run_events: Path

    def to_env(self) -> dict[str, str]:
        """投影为环境变量字典（仅供 ``--shell-exports`` 兼容路径使用）。"""
        return {
            "NPC_REPO_ROOT": str(self.repo_root),
            "NPC_PROJ_KEY": self.proj_key,
            "NPC_TASK_LOG_DIR": str(self.task_log_dir),
            "NPC_RUN_TS": self.run_ts,
            "NPC_RUN_DIR": str(self.run_dir),
            "NPC_STATE_JSON": str(self.state_json),
            "NPC_STATE_MD": str(self.state_md),
            "NPC_INDEX_FILE": str(self.index_file),
            "NPC_SCHEMA_PATH": str(self.schema_path),
            "NPC_RUN_EVENTS": str(self.run_events),
        }

    def to_run_json_dict(self) -> dict:
        """序列化为 ``run.json`` 的 dict 形态。"""
        return {
            "schema_version": RUN_JSON_SCHEMA_VERSION,
            "repo_root": str(self.repo_root),
            "proj_key": self.proj_key,
            "task_log_dir": str(self.task_log_dir),
            "run_ts": self.run_ts,
            "run_dir": str(self.run_dir),
            "state_json": str(self.state_json),
            "state_md": str(self.state_md),
            "index_file": str(self.index_file),
            "schema_path": str(self.schema_path),
            "run_events": str(self.run_events),
        }


class PathsError(Exception):
    """路径计算或环境装载失败。"""


# ============================================================
# 基础推导
# ============================================================


def detect_repo_root(start: Path | None = None) -> Path:
    """运行 git rev-parse --show-toplevel 探测工程根。

    失败抛 PathsError；调用方负责把错误转成 emit_error。
    """
    cwd = start or Path.cwd()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:  # 没装 git
        raise PathsError(f"未找到 git 命令：{e}") from e
    except subprocess.CalledProcessError as e:
        raise PathsError(f"当前目录不是 git 仓库（cwd={cwd}）：{e.stderr.strip()}") from e
    return Path(out.stdout.strip())


def proj_key_for(repo_root: Path) -> str:
    """把绝对路径转为 cc projects 同款 mangle key（/ → -）。"""
    s = str(repo_root)
    if not s.startswith("/"):
        # Windows / 相对路径不在本工具支持范围内
        raise PathsError(f"工程根必须是绝对路径：{s}")
    return s.replace("/", "-")


def make_run_ts(now: datetime | None = None) -> str:
    """生成 'YYYY-MM-DD-HHMM' 格式的 run timestamp（本地时区）。"""
    dt = now or datetime.now()
    return dt.strftime("%Y-%m-%d-%H%M")


def task_log_dir_for(repo_root: Path, home: Path | None = None) -> Path:
    """根据 repo_root 派生 ``<home>/task_log/<proj_key>``。"""
    h = home or Path.home()
    return h / "task_log" / proj_key_for(repo_root)


def compute_paths(
    repo_root: Path,
    run_ts: str | None = None,
    home: Path | None = None,
) -> Paths:
    """根据 repo_root + run_ts 派生本次 run 的全部路径。

    home 参数仅供测试覆盖；生产代码使用 Path.home()。
    """
    if not repo_root.is_absolute():
        raise PathsError(f"repo_root 必须是绝对路径：{repo_root}")
    h = home or Path.home()
    proj_key = proj_key_for(repo_root)
    task_log_dir = h / "task_log" / proj_key
    ts = run_ts or make_run_ts()
    run_dir = task_log_dir / ts
    state_json = task_log_dir / f"{ts}-plan-state.json"
    state_md = task_log_dir / f"{ts}-plan-state.md"
    index_file = task_log_dir / "index.jsonl"
    schema_path = h / "task_log" / SCHEMA_FILENAME
    run_events = run_dir / "run.events.jsonl"
    return Paths(
        repo_root=repo_root,
        proj_key=proj_key,
        task_log_dir=task_log_dir,
        run_ts=ts,
        run_dir=run_dir,
        state_json=state_json,
        state_md=state_md,
        index_file=index_file,
        schema_path=schema_path,
        run_events=run_events,
    )


def ensure_dirs(paths: Paths) -> None:
    """确保所有必要目录存在（task_log_dir / run_dir / schema 目录 / session cache）。"""
    paths.task_log_dir.mkdir(parents=True, exist_ok=True)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.schema_path.parent.mkdir(parents=True, exist_ok=True)
    # session cache 根（hook 写入位置）
    (paths.schema_path.parent / SESSION_CACHE_REL / "sessions").mkdir(
        parents=True, exist_ok=True
    )
    (paths.schema_path.parent / SESSION_CACHE_REL / "by-cwd").mkdir(
        parents=True, exist_ok=True
    )


def session_cache_dirs(home: Path | None = None) -> tuple[Path, Path, Path]:
    """返回 (root, sessions_dir, by_cwd_dir)。"""
    h = home or Path.home()
    root = h / "task_log" / SESSION_CACHE_REL
    return root, root / "sessions", root / "by-cwd"


def base_for(paths: Paths, seq: int, change_id: str) -> Path:
    """计算单 change 的产物子目录（schema_version=2 布局）。"""
    return paths.run_dir / f"{seq:03d}-{change_id}"


# ============================================================
# run.json / active.json 持久化
# ============================================================


def run_json_path_for(task_log_dir: Path, run_ts: str) -> Path:
    return task_log_dir / run_ts / RUN_JSON_FILENAME


def active_json_path_for(task_log_dir: Path) -> Path:
    return task_log_dir / ACTIVE_JSON_FILENAME


def write_run_json(paths: Paths) -> Path:
    """把 Paths 写入 ``<run_dir>/run.json``，返回写入路径。"""
    target = run_json_path_for(paths.task_log_dir, paths.run_ts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(paths.to_run_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def read_run_json(run_json_path: Path) -> Paths:
    """从 ``run.json`` 重建 Paths。"""
    try:
        data = json.loads(run_json_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise PathsError(f"读取 run.json 失败：{run_json_path}：{e}") from e
    except json.JSONDecodeError as e:
        raise PathsError(f"run.json 不是合法 JSON：{run_json_path}：{e}") from e
    required = {
        "repo_root",
        "proj_key",
        "task_log_dir",
        "run_ts",
        "run_dir",
        "state_json",
        "state_md",
        "index_file",
        "schema_path",
        "run_events",
    }
    missing = required - data.keys()
    if missing:
        raise PathsError(f"run.json 缺少字段 {sorted(missing)}：{run_json_path}")
    return Paths(
        repo_root=Path(data["repo_root"]),
        proj_key=data["proj_key"],
        task_log_dir=Path(data["task_log_dir"]),
        run_ts=data["run_ts"],
        run_dir=Path(data["run_dir"]),
        state_json=Path(data["state_json"]),
        state_md=Path(data["state_md"]),
        index_file=Path(data["index_file"]),
        schema_path=Path(data["schema_path"]),
        run_events=Path(data["run_events"]),
    )


def set_active(task_log_dir: Path, run_ts: str) -> Path:
    """把 ``<task_log_dir>/active.json`` 指针更新为给定 run_ts。"""
    target = active_json_path_for(task_log_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ACTIVE_JSON_SCHEMA_VERSION,
        "current_run_ts": run_ts,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    # 原子写：先写临时文件再 rename
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def read_active(task_log_dir: Path) -> str | None:
    """读取 ``active.json`` 的 current_run_ts；缺失或解析失败返回 None。"""
    p = active_json_path_for(task_log_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ts = data.get("current_run_ts")
    if not isinstance(ts, str) or not ts:
        return None
    return ts


# ============================================================
# Loader：子命令统一入口
# ============================================================


def _arg(args: argparse.Namespace | None, name: str) -> str | None:
    if args is None:
        return None
    val = getattr(args, name, None)
    if val is None or val == "":
        return None
    return str(val)


def _load_from_env() -> Paths | None:
    """v0.1 兼容路径：从 NPC_* 环境变量重建 Paths。"""
    required = [
        "NPC_REPO_ROOT",
        "NPC_PROJ_KEY",
        "NPC_TASK_LOG_DIR",
        "NPC_RUN_TS",
        "NPC_RUN_DIR",
        "NPC_STATE_JSON",
        "NPC_STATE_MD",
        "NPC_INDEX_FILE",
        "NPC_SCHEMA_PATH",
        "NPC_RUN_EVENTS",
    ]
    if any(not os.environ.get(k) for k in required):
        return None
    return Paths(
        repo_root=Path(os.environ["NPC_REPO_ROOT"]),
        proj_key=os.environ["NPC_PROJ_KEY"],
        task_log_dir=Path(os.environ["NPC_TASK_LOG_DIR"]),
        run_ts=os.environ["NPC_RUN_TS"],
        run_dir=Path(os.environ["NPC_RUN_DIR"]),
        state_json=Path(os.environ["NPC_STATE_JSON"]),
        state_md=Path(os.environ["NPC_STATE_MD"]),
        index_file=Path(os.environ["NPC_INDEX_FILE"]),
        schema_path=Path(os.environ["NPC_SCHEMA_PATH"]),
        run_events=Path(os.environ["NPC_RUN_EVENTS"]),
    )


def load_paths(args: argparse.Namespace | None = None) -> Paths:
    """子命令统一入口：按优先级 resolve 当前 run 的 Paths。

    resolve 顺序：

    1. 若 ``args.task_log_dir`` 与 ``args.run_ts`` 都给定：直接定位 run.json。
    2. 若 ``args.run_ts`` 给定：cwd → repo_root → task_log_dir，定位 run.json。
    3. cwd → repo_root → task_log_dir → active.json → run.json。
    4. NPC_* 环境变量（v0.1 兼容）。
    5. 全部失败 → ``PathsError``。

    ``args.state_json`` 仅覆盖 ``state_json`` 字段，不参与 run 定位（保留语义）。
    """
    explicit_task_log = _arg(args, "task_log_dir")
    explicit_run_ts = _arg(args, "run_ts")
    state_json_override = _arg(args, "state_json")

    paths: Paths | None = None
    source: str = ""

    # 1/2: 显式 run_ts
    if explicit_run_ts:
        if explicit_task_log:
            task_log_dir = Path(explicit_task_log)
        else:
            task_log_dir = _task_log_dir_from_cwd()
        rj = run_json_path_for(task_log_dir, explicit_run_ts)
        if rj.is_file():
            paths = read_run_json(rj)
            source = "run_json_explicit"
        else:
            raise PathsError(
                f"未找到指定 run 的 run.json：{rj}（请检查 --run-ts/--task-log-dir）"
            )

    # 3: cwd + active.json
    if paths is None:
        try:
            task_log_dir = _task_log_dir_from_cwd(explicit_task_log)
        except PathsError:
            task_log_dir = None
        if task_log_dir is not None:
            active_ts = read_active(task_log_dir)
            if active_ts:
                rj = run_json_path_for(task_log_dir, active_ts)
                if rj.is_file():
                    paths = read_run_json(rj)
                    source = "run_json_active"

    # 4: env fallback
    if paths is None:
        env_paths = _load_from_env()
        if env_paths is not None:
            paths = env_paths
            source = "env"

    if paths is None:
        raise PathsError(
            "未能定位当前 run：请先运行 `npc init`，或显式传 --run-ts/--task-log-dir。"
        )

    # 应用 --state-json 覆盖
    if state_json_override:
        paths = Paths(
            repo_root=paths.repo_root,
            proj_key=paths.proj_key,
            task_log_dir=paths.task_log_dir,
            run_ts=paths.run_ts,
            run_dir=paths.run_dir,
            state_json=Path(state_json_override),
            state_md=paths.state_md,
            index_file=paths.index_file,
            schema_path=paths.schema_path,
            run_events=paths.run_events,
        )

    # 标记来源到模块级，便于子命令做 telemetry / debug
    load_paths.last_source = source  # type: ignore[attr-defined]
    return paths


def _task_log_dir_from_cwd(override: str | None = None) -> Path:
    """从 cwd 推导 task_log_dir；override 直接生效。"""
    if override:
        return Path(override)
    repo_root = detect_repo_root()
    return task_log_dir_for(repo_root)


# v0.1 兼容入口；新增 deprecation 标记字段以便外部读取，但不在每次调用都 emit warning
# （否则会污染 stdout 的 JSON 输出）。
def load_paths_from_env(state_json_override: str | None = None) -> Paths:
    """[Deprecated since v0.2] 仅从 NPC_* 环境变量重建 Paths。

    新代码应使用 ``load_paths(args)``；保留此函数仅为兼容外部脚本与现有测试。
    """
    env_paths = _load_from_env()
    if env_paths is None:
        missing = [
            k
            for k in (
                "NPC_REPO_ROOT",
                "NPC_PROJ_KEY",
                "NPC_TASK_LOG_DIR",
                "NPC_RUN_TS",
                "NPC_RUN_DIR",
                "NPC_STATE_JSON",
                "NPC_STATE_MD",
                "NPC_INDEX_FILE",
                "NPC_SCHEMA_PATH",
                "NPC_RUN_EVENTS",
            )
            if not os.environ.get(k)
        ]
        raise PathsError(
            f"缺少环境变量 {missing}；v0.2 起推荐使用 `npc init`（自动落 run.json）"
        )
    if state_json_override:
        env_paths = Paths(
            repo_root=env_paths.repo_root,
            proj_key=env_paths.proj_key,
            task_log_dir=env_paths.task_log_dir,
            run_ts=env_paths.run_ts,
            run_dir=env_paths.run_dir,
            state_json=Path(state_json_override),
            state_md=env_paths.state_md,
            index_file=env_paths.index_file,
            schema_path=env_paths.schema_path,
            run_events=env_paths.run_events,
        )
    return env_paths
