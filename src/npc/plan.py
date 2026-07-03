"""npc plan —— SDD 阶段前置门 + change 脚手架。

两个子命令：

- ``npc plan check``：在进入 implement 前，确定性校验 openspec change 的
  ``applyRequires`` 所列产物是否都已 ``done``。绝不裸信 LLM 自报"已就绪"，而是
  实际调 ``openspec status --change <id> --json`` 解析其产物状态，emit 结构化判定。

- ``npc plan new-change``：调 ``openspec new change <id>`` 生成 change 脚手架，
  成功后扫描生成目录列出文件，emit 结构化结果。

退出码约定（与其余 npc 命令一致）：
- 0：ready / 成功
- 1：not ready / openspec 调用失败 / 非法 JSON
- 2：缺必需参数（--change）
- 3：非 git 仓库（repo 定位失败）
- 4：openspec 依赖缺失
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import _io
from . import paths as _paths
from . import config as _config


# change-id 合法字符集：首字符必须是字母数字，其余允许 . _ -。
# 既挡参数注入（前导 ``-`` 被下游当 flag），又挡路径遍历（``/`` 与 ``..``）。
_CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _is_safe_change_id(change: str) -> bool:
    """change-id 必须匹配 ``^[A-Za-z0-9][A-Za-z0-9._-]*$`` 且不得为纯 ``.`` / ``..``。"""
    if not _CHANGE_ID_RE.match(change):
        return False
    # ``.`` / ``..`` 不会匹配（首字符须字母数字），此处冗余保险。
    if change in (".", ".."):
        return False
    return True


def _is_injection_arg(value: str | None) -> bool:
    """非 None 且以 ``-`` 开头 → 会被下游 CLI 误判为 flag（参数注入）。"""
    return value is not None and value.startswith("-")


# ============================================================
# 共享：repo 定位 + openspec 发现（便于测试 monkeypatch）
# ============================================================


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """定位 repo_root。plan 只需 git 仓库（无需 active run / npc init）：

    优先 git toplevel；仅当 cwd 不在 git 仓库时回退 load_paths（兼容显式 --run-ts 调试）。
    """
    try:
        return _paths.detect_repo_root()
    except _paths.PathsError:
        return _paths.load_paths(args).repo_root


def _find_openspec_bin(override: str | None = None) -> str:
    """在 PATH 中发现 openspec 命令；找不到抛 FileNotFoundError（→ exit 4）。"""
    if override:
        return override
    p = shutil.which("openspec")
    if not p:
        raise FileNotFoundError("未在 PATH 中找到 openspec 命令")
    return p


# ============================================================
# 子命令 1：npc plan check
# ============================================================


def _parse_status_payload(payload: dict, apply_requires: list[str]) -> list[str]:
    """从 openspec status 的 artifacts 计算未 done 的 applyRequires 产物 id。

    纯函数：``artifacts`` 每项形如 ``{"id": ..., "status": ...}``；返回 missing 列表
    （applyRequires 中 status != "done" 或根本不存在于 artifacts 的产物 id）。
    """
    artifacts = payload.get("artifacts") or []
    status_by_id: dict[str, str | None] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        aid = item.get("id")
        if isinstance(aid, str):
            status_by_id[aid] = item.get("status")
    missing: list[str] = []
    for req in apply_requires:
        if status_by_id.get(req) != "done":
            missing.append(req)
    return missing


def run_check(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc plan check``：openspec status 解析 applyRequires 产物就绪度。

    ``runner`` 可注入（默认 :func:`subprocess.run`），测试用假 runner 返回预设 stdout。
    退出码：ready → 0；not ready → 1；openspec 缺失 → 4；
    openspec 调用失败/非法 JSON → 1；缺 --change → 2；非 git 仓库 → 3。
    """
    change = getattr(args, "change", None)
    if not change:
        _io.emit_error("invalid_args", "必须提供 --change", exit_code=2)
        return
    # 参数注入防护：change 以 ``-`` 开头会被 openspec 当作 flag。在构造 argv 前挡。
    if _is_injection_arg(change):
        _io.emit_error(
            "invalid_args", f"--change 不得以 '-' 开头（疑似参数注入）：{change!r}", exit_code=2
        )
        return

    phase = getattr(args, "phase", None) or "implement"

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        osp = _find_openspec_bin(getattr(args, "openspec_bin", None))
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return

    try:
        proc = runner(
            [osp, "status", "--change", change, "--json"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _io.emit_error(
            "subprocess_error", f"openspec status 子进程执行失败：{e}", exit_code=1
        )
        return
    if proc.returncode != 0:
        _io.emit_error(
            "openspec_failed",
            f"openspec status 失败（exit={proc.returncode}）：{(proc.stderr or '').strip()[-1000:]}",
            exit_code=1,
        )
        return

    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError as e:
        _io.emit_error(
            "invalid_json",
            f"openspec status 输出不是合法 JSON：{e}",
            exit_code=1,
        )
        return
    if not isinstance(payload, dict):
        _io.emit_error(
            "invalid_json",
            "openspec status 输出顶层不是 JSON 对象",
            exit_code=1,
        )
        return

    apply_requires = payload.get("applyRequires") or []
    if not isinstance(apply_requires, list):
        apply_requires = []
    apply_requires = [a for a in apply_requires if isinstance(a, str)]

    missing = _parse_status_payload(payload, apply_requires)
    ready = len(missing) == 0
    _io.emit(
        {
            "ok": ready,
            "change": change,
            "phase": phase,
            "ready": ready,
            "apply_requires": apply_requires,
            "missing": missing,
        }
    )
    if not ready:
        raise SystemExit(1)


# ============================================================
# 子命令 2：npc plan new-change
# ============================================================


def _scaffold_files(change_dir: Path) -> list[str]:
    """扫描 change 目录，返回相对 change_dir 的文件路径列表（排序、含子目录）。"""
    if not change_dir.is_dir():
        return []
    files = [
        str(p.relative_to(change_dir))
        for p in sorted(change_dir.rglob("*"))
        if p.is_file()
    ]
    return files


def run_new_change(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc plan new-change``：调 openspec new change 生成脚手架并列出文件。

    ``runner`` 可注入（默认 :func:`subprocess.run`）。
    退出码：成功 → 0；openspec 缺失 → 4；openspec 失败 → 1（error 带 stderr 尾段）；
    缺 --change → 2；非 git 仓库 → 3。
    """
    change = getattr(args, "change", None)
    if not change:
        _io.emit_error("invalid_args", "必须提供 --change", exit_code=2)
        return
    # change-id 强约束 ``^[A-Za-z0-9._-]+$``：同时挡参数注入（leading ``-``）与
    # 路径遍历（``/`` / ``..``）。change_dir 直接由 change 拼接，必须先净化。
    if not _is_safe_change_id(change):
        _io.emit_error(
            "invalid_args",
            f"--change 仅允许字母数字与 . _ - （禁路径分隔/前导 '-'）：{change!r}",
            exit_code=2,
        )
        return

    description = getattr(args, "description", None)
    schema = getattr(args, "schema", None)
    # 参数注入防护：description / schema 以 ``-`` 开头会被 openspec 当作 flag。
    for name, value in (("--description", description), ("--schema", schema)):
        if _is_injection_arg(value):
            _io.emit_error(
                "invalid_args",
                f"{name} 不得以 '-' 开头（疑似参数注入）：{value!r}",
                exit_code=2,
            )
            return

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        osp = _find_openspec_bin(getattr(args, "openspec_bin", None))
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return

    # change_dir 用于 scaffold 扫描与输出（保持与 repo_root 同源、不 resolve 以稳定输出）。
    changes_root = repo_root / "openspec" / "changes"
    change_dir = changes_root / change
    # resolve 后纵深校验：即便 change-id 已净化，仍核对解析路径未越出边界。
    resolved_root = changes_root.resolve()
    resolved_dir = change_dir.resolve()
    if resolved_dir != resolved_root and resolved_root not in resolved_dir.parents:
        _io.emit_error(
            "invalid_args",
            f"change 目录越出 openspec/changes/ 边界：{resolved_dir}",
            exit_code=2,
        )
        return

    cmd = [osp, "new", "change", change]
    if description:
        cmd += ["--description", description]
    if schema:
        cmd += ["--schema", schema]

    try:
        proc = runner(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _io.emit_error(
            "subprocess_error", f"openspec new change 子进程执行失败：{e}", exit_code=1
        )
        return
    if proc.returncode != 0:
        _io.emit_error(
            "openspec_failed",
            f"openspec new change 失败（exit={proc.returncode}）：{(proc.stderr or '').strip()[-1000:]}",
            exit_code=1,
        )
        return

    files = _scaffold_files(change_dir)
    _io.emit(
        {
            "ok": True,
            "change": change,
            "path": str(change_dir),
            "files": files,
        }
    )


# ============================================================
# 子命令 3：npc plan dag（DAG 分层分析）
# ============================================================

# 从 markdown 文本提取文件路径的正则集：
# 匹配反引号代码片段中的路径、src/ plugins/ tests/ openspec/ docs/ 前缀路径，
# 以及 *.py *.md *.json *.toml 后缀路径。
# 同时支持 glob 模式（含 * 或 **）如 `src/npc/*.py`。
_PATH_RE = re.compile(
    r"""
    `([^`\s]+)`                                           # backtick — 任意非空格内容（含 glob）
    | (?<![A-Za-z0-9_/.-])                               # word boundary
      ((?:src|plugins|tests|openspec|docs|\.npc)/[A-Za-z0-9/_\-\.\*]+\.(?:py|md|json|toml|yaml|yml|sh))
    | (?<![A-Za-z0-9_/.-])
      ([A-Za-z0-9_\-]+\.(?:py|json|toml|yaml|yml|sh))   # bare filename with extension
    """,
    re.VERBOSE,
)

# 路径必须含有效扩展名或 glob 通配符才算路径（过滤纯自然语言词）
_VALID_PATH_RE = re.compile(
    r'(?:\.(?:py|md|json|toml|yaml|yml|sh|txt)|[*])'
)


def _extract_paths_from_text(text: str) -> set[str]:
    """从文本中静态提取文件路径集合（正则匹配，归一化为 posix 字符串）。

    支持 glob 模式：含 * 的路径（如 src/npc/*.py）会被原样保留，
    后续由 _paths_overlap 做 glob 感知的重叠判断。
    """
    paths: set[str] = set()
    for m in _PATH_RE.finditer(text):
        for group in m.groups():
            if group:
                p = group.strip()
                # 过滤极短的（<3 字符）、含参数前缀、或不含有效扩展名/通配符
                if len(p) >= 3 and not p.startswith("-") and _VALID_PATH_RE.search(p):
                    paths.add(p)
    return paths


def _is_glob(path: str) -> bool:
    """判断路径是否为 glob 模式（含 * 字符）。"""
    return "*" in path


def _glob_overlaps_path(glob_pat: str, concrete: str) -> bool:
    """保守判断 glob 模式 glob_pat 是否与具体路径 concrete 重叠。

    策略：
    1. 提取 glob 的目录前缀（第一个 * 之前的目录部分）。
    2. 提取 glob 的文件扩展名（如果 * 后面有 .ext 则取之）。
    3. concrete 路径必须以该目录前缀开头，且（若有扩展名约束）扩展名匹配。

    例：
    - glob=src/npc/*.py, concrete=src/npc/state.py → True（前缀+扩展名匹配）
    - glob=src/**/*.py, concrete=src/npc/state.py → True（前缀匹配）
    - glob=src/npc/*.py, concrete=src/other/a.py  → False（不同目录）
    - glob=src/npc/*.py, concrete=src/npc/a.md   → False（扩展名不符）
    """
    # 找到第一个 * 的位置，取其左侧作为目录前缀
    star_idx = glob_pat.index("*")
    prefix = glob_pat[:star_idx]
    # prefix 可能以 / 结尾（如 "src/npc/"），也可能不以 / 结尾（如 "src/npc"）
    # 取到最后一个 / 处作为目录前缀（确保只匹配目录边界）
    if "/" in prefix:
        dir_prefix = prefix[: prefix.rfind("/") + 1]
    else:
        dir_prefix = ""

    if not concrete.startswith(dir_prefix):
        return False

    # 提取 glob 中 * 之后的扩展名约束（如 *.py → .py）
    suffix_part = glob_pat[star_idx:]
    # 找到后缀的扩展名：去掉所有 * 后，剩余部分若以 .ext 结尾则匹配
    ext_match = re.search(r'(\.[a-zA-Z0-9]+)$', suffix_part.replace("*", ""))
    if ext_match:
        required_ext = ext_match.group(1)
        if not concrete.endswith(required_ext):
            return False

    return True


def _paths_overlap(paths_a: set[str], paths_b: set[str]) -> bool:
    """Glob 感知的路径集合重叠判断。

    两个集合存在重叠当且仅当：
    - 存在精确字符串相同的路径；或
    - paths_a 中有 glob 模式匹配 paths_b 中的具体路径（反之亦然）；或
    - 两个 glob 模式都存在且目录前缀+扩展名约束相互包含（保守：前缀相同即视为重叠）。
    """
    # 快速路径：精确交集
    if paths_a & paths_b:
        return True

    globs_a = {p for p in paths_a if _is_glob(p)}
    concretes_a = paths_a - globs_a
    globs_b = {p for p in paths_b if _is_glob(p)}
    concretes_b = paths_b - globs_b

    # glob_a vs concrete_b
    for gp in globs_a:
        for cp in concretes_b:
            if _glob_overlaps_path(gp, cp):
                return True

    # glob_b vs concrete_a
    for gp in globs_b:
        for cp in concretes_a:
            if _glob_overlaps_path(gp, cp):
                return True

    # glob_a vs glob_b：两个 glob 前缀相同 → 保守视为重叠
    for ga in globs_a:
        for gb in globs_b:
            star_a = ga.index("*")
            star_b = gb.index("*")
            prefix_a = ga[:star_a]
            prefix_b = gb[:star_b]
            # 取各自到最后一个 / 的目录前缀
            dir_a = prefix_a[: prefix_a.rfind("/") + 1] if "/" in prefix_a else ""
            dir_b = prefix_b[: prefix_b.rfind("/") + 1] if "/" in prefix_b else ""
            if dir_a and dir_b and (dir_a.startswith(dir_b) or dir_b.startswith(dir_a)):
                return True

    return False


def _overlapping_paths_for_hotspot(paths_a: set[str], paths_b: set[str]) -> set[str]:
    """返回两集合之间重叠的路径（用于 hotspot 命名，glob 感知）。

    返回的是具体路径名（用于显示），glob 模式本身也可能被返回。
    """
    result: set[str] = set()

    # 精确交集
    result |= paths_a & paths_b

    globs_a = {p for p in paths_a if _is_glob(p)}
    concretes_a = paths_a - globs_a
    globs_b = {p for p in paths_b if _is_glob(p)}
    concretes_b = paths_b - globs_b

    for gp in globs_a:
        for cp in concretes_b:
            if _glob_overlaps_path(gp, cp):
                result.add(gp)
                result.add(cp)

    for gp in globs_b:
        for cp in concretes_a:
            if _glob_overlaps_path(gp, cp):
                result.add(gp)
                result.add(cp)

    for ga in globs_a:
        for gb in globs_b:
            star_a = ga.index("*")
            star_b = gb.index("*")
            prefix_a = ga[:star_a]
            prefix_b = gb[:star_b]
            dir_a = prefix_a[: prefix_a.rfind("/") + 1] if "/" in prefix_a else ""
            dir_b = prefix_b[: prefix_b.rfind("/") + 1] if "/" in prefix_b else ""
            if dir_a and dir_b and (dir_a.startswith(dir_b) or dir_b.startswith(dir_a)):
                result.add(ga)
                result.add(gb)

    return result


def _extract_paths_for_change(change_dir: Path) -> set[str]:
    """从 tasks.md 和 specs/**/*.md 中提取 touched 路径集合。"""
    paths: set[str] = set()
    # tasks.md
    tasks_file = change_dir / "tasks.md"
    if tasks_file.is_file():
        paths |= _extract_paths_from_text(tasks_file.read_text(encoding="utf-8", errors="ignore"))
    # specs/**/*.md
    specs_dir = change_dir / "specs"
    if specs_dir.is_dir():
        for spec_file in specs_dir.rglob("*.md"):
            paths |= _extract_paths_from_text(spec_file.read_text(encoding="utf-8", errors="ignore"))
    # proposal.md
    proposal_file = change_dir / "proposal.md"
    if proposal_file.is_file():
        paths |= _extract_paths_from_text(proposal_file.read_text(encoding="utf-8", errors="ignore"))
    return paths


# 从 proposal.md / tasks.md 中提取显式依赖声明的正则
# 匹配如：
#   "依赖前置：orchestrator-check-record-result、..."
#   "前置：x、y"
#   "applyRequires: [x, y]"
#   "depends on: x"
_DEP_EXPLICIT_RE = re.compile(
    r"""
    (?:依赖前置|依赖|前置|applyRequires|depends?[ _]on|prerequisite)
    [：:\s]+
    (\[?[A-Za-z0-9,、，\s_\-\[\]]+\]?)
    """,
    re.VERBOSE | re.IGNORECASE,
)
_CHANGE_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}")


def _extract_deps_for_change(change_dir: Path, known_ids: set[str]) -> set[str]:
    """从 proposal.md / tasks.md 中提取显式依赖的 change-id（仅返回 known_ids 的子集）。"""
    deps: set[str] = set()
    for fname in ("proposal.md", "tasks.md"):
        f = change_dir / fname
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in _DEP_EXPLICIT_RE.finditer(text):
            raw = m.group(1)
            for tok in _CHANGE_ID_TOKEN_RE.findall(raw):
                if tok in known_ids:
                    deps.add(tok)
    return deps


def _topological_sort(nodes: list[str], deps: dict[str, set[str]]) -> list[str] | None:
    """Kahn 算法拓扑排序。返回排序列表，有环返回 None。"""
    in_degree: dict[str, int] = {n: 0 for n in nodes}
    graph: dict[str, list[str]] = {n: [] for n in nodes}
    for node, d_set in deps.items():
        for dep in d_set:
            if dep in graph:
                graph[dep].append(node)
                in_degree[node] += 1

    queue = [n for n in nodes if in_degree[n] == 0]
    order: list[str] = []
    while queue:
        queue.sort()  # 确定性排序
        node = queue.pop(0)
        order.append(node)
        for succ in graph[node]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)
    if len(order) != len(nodes):
        return None  # cycle
    return order


def _build_dag_layers(
    plan_order: list[str],
    deps: dict[str, set[str]],
    paths_map: dict[str, set[str]],
    max_parallel: int,
    topo_order: list[str] | None = None,
) -> tuple[list[list[str]], dict[str, list[str]]]:
    """构建 DAG 分层。

    返回 (layers, serialization_reasons)。
    layers 是分好的层（每层是 change_id 列表）。
    serialization_reasons 是每个被串行化的 change 的原因列表。

    topo_order：拓扑排序结果（依赖先于被依赖者），用于保证 layer_of 计算时
    所有依赖已处理完毕。若未传入则退化到 plan_order 顺序（保持向后兼容）。
    """
    serialization_reasons: dict[str, list[str]] = {cid: [] for cid in plan_order}
    layers: list[list[str]] = []

    # 按拓扑序（依赖先处理）计算每个节点的层深
    # 保证：对任意 dep -> cid，layer_of[dep] 已确定，layer_of[cid] = max(dep 层) + 1
    process_order = topo_order if topo_order is not None else plan_order
    layer_of: dict[str, int] = {}
    for cid in process_order:
        dep_layers = [layer_of[d] for d in deps.get(cid, set()) if d in layer_of]
        layer_of[cid] = (max(dep_layers) + 1) if dep_layers else 0

    # 按 plan_order 分层（保持顺序）
    max_layer = max(layer_of.values()) if layer_of else 0
    raw_layers: list[list[str]] = [[] for _ in range(max_layer + 1)]
    for cid in plan_order:
        raw_layers[layer_of[cid]].append(cid)

    # 在每层内检查路径冲突，冲突的 change 被拆到下一层
    for raw_layer in raw_layers:
        # 分配层内：贪心放置，路径不重叠
        current_layer: list[str] = []
        current_paths: set[str] = set()

        for cid in raw_layer:
            cid_paths = paths_map.get(cid, set())
            if not cid_paths:
                # 无路径信息：单独成层（保守退化）
                if current_layer:
                    layers.append(current_layer)
                current_layer = []
                current_paths = set()
                layers.append([cid])
                serialization_reasons[cid].append("no-paths")
                continue

            overlap = _paths_overlap(cid_paths, current_paths)
            if overlap:
                # 路径重叠：检查与当前层中谁冲突，记录 hotspot
                for placed in current_layer:
                    placed_paths = paths_map.get(placed, set())
                    hot = _overlapping_paths_for_hotspot(cid_paths, placed_paths)
                    if hot:
                        hotspot_names = sorted(Path(p).name for p in hot)[:3]
                        reason = "hotspot=" + ",".join(hotspot_names)
                        serialization_reasons[cid].append(reason)
                        serialization_reasons[placed].append(reason)
                # 先提交当前层，开新层
                if current_layer:
                    layers.append(current_layer)
                current_layer = [cid]
                current_paths = set(cid_paths)
            else:
                current_layer.append(cid)
                current_paths |= cid_paths

        if current_layer:
            layers.append(current_layer)

    # max_parallel 切片
    if max_parallel > 1:
        sliced: list[list[str]] = []
        for layer in layers:
            if len(layer) > max_parallel:
                for i in range(0, len(layer), max_parallel):
                    chunk = layer[i:i + max_parallel]
                    sliced.append(chunk)
                    if len(layer[i:]) > max_parallel:
                        for cid in chunk:
                            serialization_reasons[cid].append("max-parallel-slice")
            else:
                sliced.append(layer)
        layers = sliced
    elif max_parallel == 1:
        # 强制每层只有一个元素
        single: list[list[str]] = []
        for layer in layers:
            for cid in layer:
                single.append([cid])
        layers = single

    return layers, serialization_reasons


def run_dag(args: argparse.Namespace) -> None:
    """``npc plan dag``：分析 plan_order 中的 change 产出 DAG 分层。

    从 --plan-order（JSON 数组）或当前 run state 读取 change 列表，
    静态分析文件路径重叠与依赖，产出分层 JSON。
    """
    # 1. 解析 plan_order
    plan_order_raw = getattr(args, "plan_order", None)
    if plan_order_raw:
        try:
            plan_order = json.loads(plan_order_raw)
            if not isinstance(plan_order, list) or not all(isinstance(x, str) for x in plan_order):
                raise ValueError("plan_order 必须是字符串数组")
        except (json.JSONDecodeError, ValueError) as e:
            _io.emit_error("invalid_plan_order", f"--plan-order 解析失败：{e}", exit_code=2)
            return
    else:
        _io.emit_error("invalid_args", "必须提供 --plan-order", exit_code=2)
        return

    if not plan_order:
        _io.emit({"ok": True, "layers": [], "parallelizable_fraction": 0.0,
                  "serialization_reason": {}, "degraded_reason": None})
        return

    # 2. 定位 repo_root 和 openspec/changes 目录
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    changes_root = repo_root / "openspec" / "changes"

    # 3. 加载配置读取 max_parallel
    config_path = getattr(args, "config", None)
    try:
        cfg = _config.load_config(
            repo_root,
            override_path=Path(config_path) if config_path else None,
        )
    except _config.ConfigError as e:
        _io.emit_error("config_error", str(e), exit_code=1)
        return
    max_parallel = cfg.scheduler.max_parallel
    max_evictions = cfg.scheduler.max_evictions

    known_ids = set(plan_order)

    # 4. 提取每个 change 的路径和依赖
    paths_map: dict[str, set[str]] = {}
    deps_map: dict[str, set[str]] = {}
    for cid in plan_order:
        change_dir = changes_root / cid
        # 尝试 archive 子目录（已归档的 change）
        if not change_dir.is_dir():
            for arc_candidate in (changes_root / "archive").glob(f"*-{cid}"):
                if arc_candidate.is_dir():
                    change_dir = arc_candidate
                    break
        paths_map[cid] = _extract_paths_for_change(change_dir) if change_dir.is_dir() else set()
        deps_map[cid] = _extract_deps_for_change(change_dir, known_ids) if change_dir.is_dir() else set()
        # 确保依赖只含 known_ids（防止指向 plan_order 外）
        unknown_deps = deps_map[cid] - known_ids
        deps_map[cid] = deps_map[cid] & known_ids

    # 5. 检查依赖问题
    degraded_reason: str | None = None
    degraded = False

    # 检查未知依赖（指向不在 plan_order 的 change）
    unknown_dep_pairs: list[str] = []
    for cid in plan_order:
        change_dir = changes_root / cid
        if not change_dir.is_dir():
            for arc_candidate in (changes_root / "archive").glob(f"*-{cid}"):
                if arc_candidate.is_dir():
                    change_dir = arc_candidate
                    break
        all_deps = _extract_deps_for_change(change_dir, known_ids | {"_all_"}) if change_dir.is_dir() else set()
        all_deps_before_filter = {d for d in all_deps}
        unknown = all_deps_before_filter - known_ids
        # 再次提取完整依赖（忽略 known 过滤）
        if change_dir.is_dir():
            full_deps = _extract_deps_for_change_all(change_dir)
            unknown_ext = {d for d in full_deps if d not in known_ids and len(d) > 5}
            if unknown_ext:
                for ud in unknown_ext:
                    unknown_dep_pairs.append(f"{cid}→{ud}")

    if unknown_dep_pairs:
        degraded = True
        degraded_reason = f"unknown-dep: {', '.join(unknown_dep_pairs[:5])}"

    # 检查依赖环
    if not degraded:
        sorted_order = _topological_sort(plan_order, deps_map)
        if sorted_order is None:
            degraded = True
            # 找到有环的节点
            cycle_nodes = [cid for cid in plan_order if deps_map.get(cid)]
            degraded_reason = f"cycle: {cycle_nodes}"

    if degraded:
        # 完全串行：每个 change 单独成层
        layers = [[cid] for cid in plan_order]
        serialization_reasons = {cid: [degraded_reason or "degraded"] for cid in plan_order}
        _io.emit({
            "ok": True,
            "layers": layers,
            "serialization_reason": serialization_reasons,
            "parallelizable_fraction": 0.0,
            "degraded_reason": degraded_reason,
            "max_parallel": max_parallel,
            "max_evictions": max_evictions,
            # 退化情况下仍输出已解析的依赖边（供 propagate-dep-failed 使用）
            "deps_map": {
                cid: sorted(deps) for cid, deps in deps_map.items() if deps
            },
        })
        return

    # 6. 构建 DAG 层（传入拓扑序以保证依赖深度计算正确）
    layers, serialization_reasons = _build_dag_layers(
        plan_order, deps_map, paths_map, max_parallel, topo_order=sorted_order
    )

    # 7. 计算 parallelizable_fraction
    parallel_count = sum(len(layer) for layer in layers if len(layer) > 1)
    total = len(plan_order)
    parallelizable_fraction = parallel_count / total if total > 0 else 0.0

    _io.emit({
        "ok": True,
        "layers": layers,
        "serialization_reason": {
            cid: reasons for cid, reasons in serialization_reasons.items() if reasons
        },
        "parallelizable_fraction": round(parallelizable_fraction, 3),
        "degraded_reason": None,
        "max_parallel": max_parallel,
        "max_evictions": max_evictions,
        # deps_map: 显式依赖边（仅 plan_order 内部依赖），供编排者做依赖失败传播。
        # 格式：{change_id: [dep_id, ...]}（只含有依赖的条目）
        "deps_map": {
            cid: sorted(deps) for cid, deps in deps_map.items() if deps
        },
    })


def _extract_deps_for_change_all(change_dir: Path) -> set[str]:
    """从 proposal.md / tasks.md 中提取所有疑似 change-id 的依赖声明（不过滤 known）。"""
    deps: set[str] = set()
    for fname in ("proposal.md", "tasks.md"):
        f = change_dir / fname
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in _DEP_EXPLICIT_RE.finditer(text):
            raw = m.group(1)
            for tok in _CHANGE_ID_TOKEN_RE.findall(raw):
                if len(tok) >= 5 and "-" in tok:  # change-id 通常含连字符
                    deps.add(tok)
    return deps


def cli_dag(args: argparse.Namespace) -> None:
    """``npc plan dag`` handler。"""
    run_dag(args)


# ============================================================
# propagate-dep-failed：依赖失败传播
# ============================================================


def run_propagate_dep_failed(args: argparse.Namespace) -> None:
    """``npc plan propagate-dep-failed``：标记依赖失败的下游 change 为 skipped-auto。

    当某 change（``--failed-change``）到达非 ``archived`` 终态（failed / skipped-auto）时，
    调用本命令可确定性地找出所有显式依赖它的下游 change，并把其中仍为非终态的条目
    原子写入 ``skipped-auto`` + ``skipped_reason=dep-failed``。

    输入：
      --failed-change <id>   已失败/已跳过的前置 change-id
      --deps-map <JSON>      npc plan dag 输出的 deps_map 字段（dict: change_id → [dep_ids]）

    输出 JSON：
      {"ok": true, "failed_change": "...", "skipped": ["downstream-a", ...]}
      {"ok": true, "failed_change": "...", "skipped": []}  — 无下游需处理

    错误输出：exit 1/2/3（同其他 npc 命令约定）。
    """
    from . import state as _state

    failed_change: str = getattr(args, "failed_change", None) or ""
    deps_map_raw: str = getattr(args, "deps_map", None) or ""

    if not failed_change:
        _io.emit_error("invalid_args", "--failed-change 不能为空", exit_code=2)
        return

    if not deps_map_raw:
        _io.emit_error("invalid_args", "--deps-map 不能为空", exit_code=2)
        return

    # 解析 deps_map（{change_id: [dep_id, ...]}）
    try:
        deps_map: dict[str, list[str]] = json.loads(deps_map_raw)
        if not isinstance(deps_map, dict):
            raise ValueError("deps_map 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as e:
        _io.emit_error("invalid_args", f"--deps-map 解析失败：{e}", exit_code=2)
        return

    # 找到所有直接或间接依赖 failed_change 的下游（传递性闭包，BFS）
    # deps_map[child] = [parent, ...] ——> reverse: dependents_of[parent] = [child, ...]
    dependents_of: dict[str, list[str]] = {}
    for child, parents in deps_map.items():
        for parent in parents:
            dependents_of.setdefault(parent, []).append(child)

    downstream: set[str] = set()
    queue = list(dependents_of.get(failed_change, []))
    while queue:
        cid = queue.pop(0)
        if cid not in downstream:
            downstream.add(cid)
            queue.extend(dependents_of.get(cid, []))

    if not downstream:
        _io.emit({"ok": True, "failed_change": failed_change, "skipped": []})
        return

    # 加载 state，找 plan_order，把 downstream 中非终态的条目写 skipped-auto
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = _state.read_state(p.state_json)
    except FileNotFoundError:
        _io.emit_error("state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3)
        return

    plan_order: list[str] = state.get("plan_order") or []
    progress: list[dict] = state.get("progress") or []

    terminal_statuses = {"archived", "failed", "skipped-auto", "needs-user-decision"}

    skipped: list[str] = []

    def mutate(s: dict) -> None:
        prog = s.get("progress") or []
        plan = s.get("plan_order") or []
        for idx, cid in enumerate(plan):
            if cid not in downstream:
                continue
            if idx >= len(prog):
                continue
            entry = prog[idx]
            if entry.get("status") in terminal_statuses:
                continue
            # 非终态 → 标记 skipped-auto + dep-failed
            entry["status"] = "skipped-auto"
            entry["skipped_reason"] = "dep-failed"
            skipped.append(cid)

    try:
        _state.update_state(p.state_json, p.state_md, mutate)
    except FileNotFoundError:
        _io.emit_error("state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3)
        return

    _io.emit({"ok": True, "failed_change": failed_change, "skipped": skipped})


def cli_propagate_dep_failed(args: argparse.Namespace) -> None:
    """``npc plan propagate-dep-failed`` handler。"""
    run_propagate_dep_failed(args)


# ============================================================
# 子命令 5：npc plan complexity（前置软性复杂度门）
# ============================================================


def _count_top_level_modules(paths: set[str]) -> int:
    """从路径集合计算跨领域广度（顶层模块/目录数）。

    仅计算含 ``/`` 的路径的首段目录（如 ``src/npc/agent.py`` → ``src``）。
    同一首段只计一次。单文件路径（无 ``/``）不计入广度（归入当前目录）。
    """
    top_dirs: set[str] = set()
    for p in paths:
        # 去除 glob 通配符中的 * 部分，仅取实际目录前缀
        clean = p.split("*")[0] if "*" in p else p
        parts = clean.split("/")
        if len(parts) > 1 and parts[0]:
            top_dirs.add(parts[0])
    return len(top_dirs)


def _count_concrete_files(paths: set[str]) -> int:
    """计算路径集合中非 glob 路径（具体文件）的数量。"""
    return sum(1 for p in paths if "*" not in p)


def compute_complexity(
    change_id: str,
    change_dir: Path,
) -> dict:
    """计算单个 change 的复杂度信号。

    返回::

        {
          "change_id": str,
          "breadth": int,          # 跨领域广度（顶层模块数）
          "files": int,            # 具体文件数（非 glob）
          "paths": list[str],      # 提取到的路径列表
        }
    """
    paths = _extract_paths_for_change(change_dir)
    breadth = _count_top_level_modules(paths)
    files = _count_concrete_files(paths)
    return {
        "change_id": change_id,
        "breadth": breadth,
        "files": files,
        "paths": sorted(paths),
    }


def run_complexity_check(args: argparse.Namespace) -> None:
    """``npc plan complexity``：前置软性复杂度门。

    对 ``--change`` 指定的 change（或 ``--plan-order`` 的全部 change）
    计算跨领域广度，超阈值时输出结构化 warning；**不阻断 run、不自动拆分**。

    当建议为 ``large`` 时，将 ``large=true`` 标记写入 change 对应的 plan-state progress 条目
    （需要 active STATE_JSON）。state 更新失败为 warning 级别，不影响返回码。

    退出码：
    - 0：检查完成（无论是否触发告警，均为 0，软门不阻断）
    - 2：缺必需参数
    - 3：repo 定位失败
    """
    # 1. 获取 change 列表
    change_raw = getattr(args, "change", None)
    plan_order_raw = getattr(args, "plan_order", None)

    if change_raw:
        changes = [change_raw]
    elif plan_order_raw:
        try:
            changes = json.loads(plan_order_raw)
            if not isinstance(changes, list) or not all(isinstance(x, str) for x in changes):
                raise ValueError("plan_order 必须是字符串数组")
        except (json.JSONDecodeError, ValueError) as e:
            _io.emit_error("invalid_plan_order", f"--plan-order 解析失败：{e}", exit_code=2)
            return
    else:
        _io.emit_error("invalid_args", "必须提供 --change 或 --plan-order", exit_code=2)
        return

    # 2. 定位 repo_root
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    changes_root = repo_root / "openspec" / "changes"

    # 3. 加载配置
    config_path = getattr(args, "config", None)
    try:
        cfg = _config.load_config(
            repo_root,
            override_path=Path(config_path) if config_path else None,
        )
    except _config.ConfigError as e:
        _io.emit_error("config_error", str(e), exit_code=1)
        return

    breadth_threshold = cfg.review.complexity_breadth_threshold
    files_threshold = cfg.review.complexity_files_threshold
    max_rounds_large = cfg.review.max_rounds_large

    # 4. 尝试加载 state（用于写 large 标记；失败不致命）
    state_obj = None
    state_json_path = None
    state_md_path = None
    try:
        p = _paths.load_paths(args)
        state_json_path = p.state_json
        state_md_path = p.state_md
        from .state import read_state
        state_obj = read_state(p.state_json)
    except Exception:
        pass  # state 不可用时跳过 large 标记

    # 5. 计算每个 change 的复杂度
    warnings: list[dict] = []
    results: list[dict] = []

    for cid in changes:
        change_dir = changes_root / cid
        if not change_dir.is_dir():
            # 尝试 archive 子目录
            for arc_candidate in (changes_root / "archive").glob(f"*-{cid}"):
                if arc_candidate.is_dir():
                    change_dir = arc_candidate
                    break

        if not change_dir.is_dir():
            results.append({"change_id": cid, "skipped": True, "reason": "change_dir_not_found"})
            continue

        complexity = compute_complexity(cid, change_dir)
        breadth = complexity["breadth"]
        files = complexity["files"]

        # 文件数只作为辅助信号，不能在 breadth 未超阈值时单独触发 warning（plan-complexity-gate spec）：
        # - 单领域大 change（breadth < breadth_threshold，files >= files_threshold）→
        #     triggered=False，不输出 warning，但仍写 large 标记以获得更多 review 预算。
        # - 跨领域（breadth >= breadth_threshold）→ triggered=True，输出 warning，建议 split。
        triggered = breadth >= breadth_threshold
        is_large = files >= files_threshold  # 辅助信号：文件数超阈值即视为 large change
        suggestion: str | None = None

        if triggered:
            suggestion = "split"

            warn_entry = {
                "change_id": cid,
                "breadth": breadth,
                "files": files,
                "suggestion": suggestion,
                "breadth_threshold": breadth_threshold,
                "files_threshold": files_threshold,
            }
            warnings.append(warn_entry)

        # large 标记写入 plan-state（与 warning 是否触发解耦）：
        # files >= files_threshold 时即标记为 large，赋予更大 max_rounds 预算；
        # 这确保单领域大 change 在 review-fix 循环中得到更多修复机会。
        if is_large and state_obj is not None and state_json_path and state_md_path:
            try:
                from .state import update_state

                def _mark_large(state: dict) -> None:
                    progress = state.get("progress") or []
                    plan_order = state.get("plan_order") or []
                    for idx, pid in enumerate(plan_order):
                        if pid == cid and idx < len(progress):
                            progress[idx]["large"] = True
                            progress[idx]["max_rounds_large"] = max_rounds_large
                            break

                update_state(state_json_path, state_md_path, _mark_large)
            except Exception:
                pass  # state 写失败是 warning 级别，不影响主流程

        result_entry = {
            "change_id": cid,
            "breadth": breadth,
            "files": files,
            "triggered": triggered,
            "suggestion": suggestion,
        }
        results.append(result_entry)

    _io.emit({
        "ok": True,
        "breadth_threshold": breadth_threshold,
        "files_threshold": files_threshold,
        "max_rounds_large": max_rounds_large,
        "results": results,
        "warnings": warnings,
        "warning_count": len(warnings),
    })


def cli_complexity(args: argparse.Namespace) -> None:
    """``npc plan complexity`` handler。"""
    run_complexity_check(args)


# ============================================================
# CLI handler 入口
# ============================================================


def cli_check(args: argparse.Namespace) -> None:
    """``npc plan check`` handler。"""
    run_check(args)


def cli_new_change(args: argparse.Namespace) -> None:
    """``npc plan new-change`` handler。"""
    run_new_change(args)
