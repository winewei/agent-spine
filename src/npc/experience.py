"""OpenViking 经验层：把 archived 轨迹回流为 coder 先验。

设计蓝本：``docs/optimization-proposals/2026-09-05-openviking-experience-layer.md``。

定位是**旁路软失败增强**：缺失只降低 prompt 质量，不影响正确性——任何 phase 都
不因它失败，只有显式命令（``doctor`` / ``--strict``）才对 server 不可用返回非 0。

集成面只有 HTTP。**禁止 import openviking 任何包**（AGPLv3 vs npc 的 MIT +
``dependencies = []``），本模块全部走 stdlib ``urllib.request``；该约束由
``npc verify deps`` 执法。

模块分五层，每层可独立单测：

- 传输：:class:`Client` / :func:`from_config` / :func:`probe_health`
- 写入侧组装：:func:`build_case` / :func:`build_messages` / :func:`strip_injection`
  / :func:`write_gate_ok`（纯函数，只读 ``<base>`` 下的 summary 与 review JSON）
- 提交：:func:`commit`
- 召回：:func:`recall` / :func:`render_block` / :func:`write_injection_record`
  / :func:`detect_contamination`
- 状态探活：:func:`fetch_task` / :func:`doctor_report`

**脱敏白名单（硬约束）**：只提交 implement/fix summary 的指定段与 review findings
的短字段；绝不读 ``events.jsonl`` / ``*.prompt.md`` / ``*.focus.md`` / diff / 代码正文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import _io, config as _config, focus as _focus, paths as _paths


DEFAULT_BASE_URL = "http://127.0.0.1:1933"
BASE_URL_ENV = "OPENVIKING_BASE_URL"
ROOT_API_KEY_ENV = "OPENVIKING_ROOT_API_KEY"

# 召回结果按 uri 前缀过滤为 experiences（服务端 context_type 无法精确锁定）
EXPERIENCE_URI_MARKER = "/memories/experiences/"

CASE_HEADER = "# OpenViking Batch Training CaseSpec v1"
CASE_PROTOCOL = "openviking.batch_train.case_spec.v1"
DEFAULT_CRITERION = "实现通过独立 review，无 blocking finding"

QUERY_MAX_CHARS = 200
PROPOSAL_SUMMARY_MAX_CHARS = 600

IMPLEMENT_SECTION_PATTERNS = ("Key Decisions", "Issues Encountered")
FIX_SECTION_PATTERNS = ("Per-Finding Resolution", "Locations Scanned", "Invariant Sweep")

INJECTION_HEADING_PREFIX = "历史经验（外部召回"
INJECTION_TAG = "<npc-experience"
# 回抄检测：注入正文的连续片段长度阈值（短于此的重合视为巧合）
CONTAMINATION_SPAN = 120


class ExperienceError(Exception):
    """经验层传输失败。``code`` 是稳定的机器可读分类，供 JSON 回执与 telemetry 用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ============================================================
# (a) 传输层
# ============================================================


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 env 文件；不存在或不可读返回空 dict。

    刻意不引入 dotenv：``dependencies = []`` 是硬约束。支持 ``export KEY=VALUE``
    前缀、``#`` 注释行与成对引号包裹的值。
    """
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export ") :].lstrip()
        key, sep, val = s.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def expand_path(raw: str, repo_root: Path | None = None) -> Path:
    """展开 ``~`` 并把相对路径锚到 repo_root（缺省锚到 cwd）。"""
    p = Path(raw).expanduser()
    if not p.is_absolute() and repo_root is not None:
        p = repo_root / p
    return p


class Client:
    """OpenViking HTTP 客户端（stdlib only，单次不重试）。

    增强项重试只会拖慢主流程——与 review 的重试策略刻意相反。所有失败统一抛
    :class:`ExperienceError`，``code`` 取值：``unreachable`` / ``timeout`` /
    ``http_<status>`` / ``bad_json``。
    """

    def __init__(self, base_url: str, api_key: str, timeout_ms: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_ms = timeout_ms

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body=body)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data: bytes | None = None
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000.0) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ExperienceError(
                f"http_{e.code}", f"{method} {path} 返回 HTTP {e.code}"
            ) from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise ExperienceError(
                    "timeout", f"{method} {path} 超时（{self.timeout_ms}ms）"
                ) from e
            raise ExperienceError(
                "unreachable", f"{method} {path} 不可达：{reason or e}"
            ) from e
        except TimeoutError as e:
            raise ExperienceError(
                "timeout", f"{method} {path} 超时（{self.timeout_ms}ms）"
            ) from e
        except OSError as e:
            raise ExperienceError("unreachable", f"{method} {path} 不可达：{e}") from e

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ExperienceError("bad_json", f"{method} {path} 返回非 JSON：{e}") from e
        if not isinstance(parsed, dict):
            raise ExperienceError(
                "bad_json", f"{method} {path} 返回顶层非对象：{type(parsed).__name__}"
            )
        return parsed


def resolve_base_url(cfg: _config.ExperienceConfig, env: dict[str, str]) -> str:
    """base_url 解析优先级：显式配置 > env 文件 > 内置默认。"""
    return cfg.base_url or env.get(BASE_URL_ENV) or DEFAULT_BASE_URL


def from_config(
    cfg: _config.ExperienceConfig,
    repo_root: Path | None = None,
    *,
    timeout_ms: int | None = None,
) -> Client | None:
    """按配置构造 client；env 文件缺失或缺 api key 时返回 ``None``（软失败）。

    不检查 ``cfg.enabled``——启用与否是命令面的分支，构造是纯装配。
    """
    env = parse_env_file(expand_path(cfg.env_file, repo_root))
    api_key = (env.get(cfg.api_key_env) or "").strip()
    if not api_key:
        return None
    return Client(
        resolve_base_url(cfg, env),
        api_key,
        cfg.timeout_recall_ms if timeout_ms is None else timeout_ms,
    )


def probe_health(client: Client) -> dict:
    """``GET /health``（免鉴权）。抛 :class:`ExperienceError`。"""
    return client.get("/health")


def _result_of(payload: dict) -> dict:
    """取 OpenViking 统一响应信封的 ``result``；非 dict 时返回空 dict。"""
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


# ============================================================
# (b) 写入侧组装（纯函数）
# ============================================================

_INJECTION_TAG_RE = re.compile(r"<npc-experience\b[^>]*>.*?</npc-experience>", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ROUND_FILE_RE_TPL = r"^round-(\d+)\.{suffix}$"


def strip_injection(text: str) -> str:
    """剥离本轮注入块，切断「经验 → prompt → summary → 经验」自激环。

    删除 ``<npc-experience …>…</npc-experience>`` 标签块与整个「历史经验（外部召回」
    章节（到下一个同级或更高级标题为止）。所有提交给 OpenViking 的正文都先过它。
    """
    if not text:
        return ""
    out = _INJECTION_TAG_RE.sub("", text)

    kept: list[str] = []
    skip_level: int | None = None
    for line in out.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            heading = m.group(2)
            if heading.startswith(INJECTION_HEADING_PREFIX):
                skip_level = level
                continue
            if skip_level is not None and level <= skip_level:
                skip_level = None
        if skip_level is None:
            kept.append(line)
    return "\n".join(kept).strip()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _round_files(base: Path, suffix: str) -> list[tuple[int, Path]]:
    """按轮次升序枚举 ``<base>/round-N.<suffix>``。"""
    pattern = re.compile(_ROUND_FILE_RE_TPL.format(suffix=re.escape(suffix)))
    out: list[tuple[int, Path]] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return out
    for child in children:
        m = pattern.match(child.name)
        if m and child.is_file():
            out.append((int(m.group(1)), child))
    out.sort(key=lambda x: x[0])
    return out


def collect_findings(base: Path) -> list[dict]:
    """汇总各轮 ``round-N.review.json`` 的 in_scope findings（只取短字段）。

    刻意不带 ``detail`` / ``recommendation``：长正文既污染检索又抬高抽取成本，
    信息密度最高的是 ``severity / category / title / file``。
    """
    out: list[dict] = []
    for rnd, path in _round_files(base, "review.json"):
        try:
            data = json.loads(_read_text(path) or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for f in data.get("findings") or []:
            if not isinstance(f, dict) or not f.get("in_scope"):
                continue
            out.append(
                {
                    "round": rnd,
                    "id": str(f.get("id") or ""),
                    "severity": str(f.get("severity") or "unknown"),
                    "category": str(f.get("category") or "unknown"),
                    "title": str(f.get("title") or "").strip(),
                    "file": str(f.get("file") or "").strip(),
                }
            )
    return out


def build_criteria(findings: list[dict]) -> list[dict]:
    """findings → ``rubric.criteria``（按 finding id 序去重）。

    ``rubric.criteria`` 不能为空——空 rubric 会让 fast path 报错，故无 finding 时
    兜底为一条「通过独立 review」。
    """
    criteria: list[dict] = []
    seen: set[str] = set()
    for f in sorted(findings, key=lambda x: (x["id"], x["round"])):
        title = f["title"]
        if not title:
            continue
        desc = f"{f['category']}: {title}"
        if desc in seen:
            continue
        seen.add(desc)
        criteria.append({"description": desc, "weight": 1.0})
    if not criteria:
        criteria = [{"description": DEFAULT_CRITERION, "weight": 1.0}]
    return criteria


def proposal_summary(repo_root: Path | None, change_id: str) -> str:
    """``openspec/changes/<id>/proposal.md`` 的 Why / What Changes 段前 600 字符。

    文件不存在或抽不到章节时返回空串（经验层永不因缺文件失败）。
    """
    if repo_root is None:
        return ""
    path = repo_root / "openspec" / "changes" / change_id / "proposal.md"
    text = _read_text(path)
    if not text:
        return ""
    section = _focus._extract_section(text, ("Why", "What Changes"))
    if not section:
        return ""
    return strip_injection(section)[:PROPOSAL_SUMMARY_MAX_CHARS]


def total_rounds_of(entry: dict, base: Path) -> int:
    """轮次数：优先取 archive 写入的 ``total_rounds``，回退数 review JSON 文件。"""
    val = entry.get("total_rounds")
    if isinstance(val, int) and val >= 0:
        return val
    return len(_round_files(base, "review.json"))


def tests_of(entry: dict) -> str:
    """测试结论：implement 自报优先，其次末轮 fix；都没有则 ``unknown``。"""
    phases = entry.get("phases") or {}
    fix_rounds = sorted(
        (
            (int(m.group(1)), v)
            for k, v in phases.items()
            if (m := re.match(r"^fix-r(\d+)$", k)) and isinstance(v, dict)
        ),
        key=lambda x: x[0],
    )
    if fix_rounds:
        tests = (fix_rounds[-1][1]).get("tests")
        if isinstance(tests, str) and tests:
            return tests
    impl = phases.get("implement")
    if isinstance(impl, dict):
        tests = impl.get("tests")
        if isinstance(tests, str) and tests:
            return tests
    return "unknown"


def build_case(
    change_id: str,
    proj_key: str,
    base: Path,
    entry: dict,
    *,
    repo_root: Path | None = None,
) -> dict:
    """组装 CaseSpec v1 的 Case JSON（命中 fast path，跳过服务端 LLM case 判定）。"""
    findings = collect_findings(base)
    return {
        "protocol": CASE_PROTOCOL,
        "case": {
            "name": change_id,
            "task_signature": f"{proj_key}:{change_id}",
            "input": {
                "proposal_summary": proposal_summary(repo_root, change_id),
                "total_rounds": total_rounds_of(entry, base),
                "tests": tests_of(entry),
            },
            "rubric": {"criteria": build_criteria(findings)},
        },
    }


def _extract_sections(text: str, patterns: tuple[str, ...]) -> list[str]:
    """逐 pattern 抽章节（复用 focus._extract_section），缺段跳过。"""
    out: list[str] = []
    for pattern in patterns:
        section = _focus._extract_section(text, (pattern,))
        if section:
            out.append(section)
    return out


def _extract_files_modified(text: str) -> str:
    """抽 ``Files Modified:`` 行及其后的连续 bullet（它不是 markdown 标题）。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip().lower().startswith("files modified:"):
            continue
        block = [line.strip()]
        for nxt in lines[i + 1 :]:
            s = nxt.strip()
            if s.startswith(("-", "*")) or (s and block[-1].endswith(":")):
                if s.startswith(("#", "Commit:", "Tests:")):
                    break
                block.append(s)
                continue
            break
        return "\n".join(b for b in block if b)
    return ""


def build_implement_message(base: Path) -> str:
    """messages[1]：implement.summary.md 的 Key Decisions / Issues Encountered / Files Modified。"""
    text = strip_injection(_read_text(base / "implement.summary.md"))
    if not text:
        return ""
    parts = _extract_sections(text, IMPLEMENT_SECTION_PATTERNS)
    files = _extract_files_modified(text)
    if files:
        parts.append(files)
    return "\n\n".join(parts).strip()


def render_findings_message(findings: list[dict]) -> str:
    """messages[2]：独立验证者指出的错——信息密度最高的一段。"""
    lines: list[str] = []
    for f in findings:
        if not f["title"]:
            continue
        suffix = f" @{f['file']}" if f["file"] else ""
        lines.append(f"- [{f['severity']}|{f['category']}] {f['title']}{suffix}")
    return "\n".join(lines)


def build_fix_message(base: Path) -> str:
    """messages[3]：各轮 fix.summary.md 的修复手法与根因扫描段。"""
    parts: list[str] = []
    for _rnd, path in _round_files(base, "fix.summary.md"):
        text = strip_injection(_read_text(path))
        if not text:
            continue
        parts.extend(_extract_sections(text, FIX_SECTION_PATTERNS))
    return "\n\n".join(parts).strip()


def build_outcome_message(total_rounds: int, tests: str) -> str:
    """messages[4]：outcome 信号 + 「归纳规则而非事件叙事」引导语。"""
    return (
        f"归档成功：共 {total_rounds} 轮 review，tests={tests}。"
        "请把可复用的做法归纳为规则（Situation/Approach/Reflect），不要复述事件经过。"
    )


def build_messages(
    change_id: str,
    proj_key: str,
    base: Path,
    entry: dict,
    *,
    repo_root: Path | None = None,
) -> list[dict]:
    """组装提交用的五段 session messages（见蓝本 §3.3）。

    首尾两段恒存在（CaseSpec header 决定 fast path，outcome 决定学习信号）；
    中间三段缺文件时整段跳过——空 content 对抽取链路无信息量。
    """
    case = build_case(change_id, proj_key, base, entry, repo_root=repo_root)
    findings = collect_findings(base)

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"{CASE_HEADER}\n```json\n"
                + json.dumps(case, ensure_ascii=False)
                + "\n```"
            ),
        }
    ]
    for role, content in (
        ("assistant", build_implement_message(base)),
        ("user", render_findings_message(findings)),
        ("assistant", build_fix_message(base)),
    ):
        if content:
            messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": build_outcome_message(
                total_rounds_of(entry, base), tests_of(entry)
            ),
        }
    )
    return messages


def write_gate_ok(entry: dict, mode: str) -> tuple[bool, str]:
    """写入闸门：返回 ``(ok, reason)``。

    ``verified``（默认）额外要求「末轮 blocking==0 ∧ 非 force-archive / 人工
    override ∧ 未被回抄污染」——若把未通过独立验证的轨迹写进经验库，下一个
    change 的 coder 会把错误做法当先验，爆炸半径是此后所有 change。
    """
    if entry.get("status") != "archived":
        return False, "not-archived"
    if mode == "any":
        return True, "ok"

    trend = entry.get("blocking_trend") or []
    if not isinstance(trend, list) or not trend:
        return False, "no-blocking-trend"
    if trend[-1] != 0:
        return False, "blocking-nonzero"
    if entry.get("override"):
        return False, "override"
    if entry.get("reason") == "force-archive":
        return False, "force-archive"
    if entry.get("experience_contaminated"):
        return False, "contaminated"
    return True, "ok"


# ============================================================
# (c) 提交
# ============================================================


def session_id_for(cfg: _config.ExperienceConfig, p, seq: int, change_id: str) -> str:
    """幂等 session id（可用 ``ov session get`` 排查）。"""
    return f"{cfg.session_prefix}-{p.proj_key}-{p.run_ts}-{seq}-{change_id}"


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def commit(
    client: Client | None,
    cfg: _config.ExperienceConfig,
    *,
    p_like,
    seq: int,
    entry: dict,
    dry_run: bool = False,
) -> dict:
    """提交一个 change 的轨迹（fire-and-forget，不等 Phase 2 异步抽取）。

    三步：``POST /sessions`` → 逐条 ``POST /sessions/{id}/messages`` →
    ``POST /sessions/{id}/commit``。session 已存在（409）视为幂等继续。

    返回 dict、**永不抛**——写入侧失败不得影响 archive 判定。成功与失败都落盘
    ``<base>/experience.commit.json``，让补偿提交与事后审计有据可查。
    """
    change_id = entry.get("change_id") or ""
    base = Path(entry.get("base") or _paths.base_for(p_like, seq, change_id))
    messages = build_messages(
        change_id,
        p_like.proj_key,
        base,
        entry,
        repo_root=getattr(p_like, "repo_root", None),
    )
    sid = session_id_for(cfg, p_like, seq, change_id)

    if dry_run:
        path = _write_json(
            base / "experience.messages.json",
            {"session_id": sid, "messages": messages, "ts": _io.now_iso()},
        )
        return {
            "ok": True,
            "dry_run": True,
            "session_id": sid,
            "path": str(path),
            "messages": len(messages),
        }

    if client is None:
        result = {"ok": False, "reason": "no_client", "detail": "缺少 OpenViking 凭据"}
        _write_json(base / "experience.commit.json", {**result, "ts": _io.now_iso()})
        return result

    try:
        _ensure_session(client, sid)
        for msg in messages:
            client.post(f"/api/v1/sessions/{urllib.parse.quote(sid)}/messages", msg)
        payload = client.post(f"/api/v1/sessions/{urllib.parse.quote(sid)}/commit", {})
    except ExperienceError as e:
        result = {"ok": False, "reason": e.code, "detail": e.message, "session_id": sid}
        _write_json(base / "experience.commit.json", {**result, "ts": _io.now_iso()})
        return result

    res = _result_of(payload)
    result = {
        "ok": True,
        "session_id": sid,
        "task_id": res.get("task_id"),
        "archive_uri": res.get("archive_uri"),
        "messages": len(messages),
    }
    _write_json(base / "experience.commit.json", {**result, "ts": _io.now_iso()})
    return result


def _ensure_session(client: Client, session_id: str) -> None:
    """建 session；已存在（409）幂等继续。

    ``memory_policy`` 只允许 experiences——服务端会自动扩展为 cases +
    trajectories + experiences，不写 profile / preferences / entities。
    """
    try:
        client.post(
            "/api/v1/sessions",
            {
                "session_id": session_id,
                "memory_policy": {"memory_types": ["experiences"]},
            },
        )
    except ExperienceError as e:
        if e.code != "http_409":
            raise


def fetch_task(client: Client, task_id: str) -> dict:
    """``GET /api/v1/tasks/{id}``；抛 :class:`ExperienceError`。"""
    return client.get(f"/api/v1/tasks/{urllib.parse.quote(task_id)}")


# ============================================================
# (d) 召回
# ============================================================


def estimate_tokens(text: str) -> int:
    """bytes/4 估算，与 telemetry 口径一致。"""
    from .telemetry import estimate_tokens_text

    return estimate_tokens_text(text)


@dataclass(frozen=True)
class RecallResult:
    """一次召回的结果。``error`` 非 None 时 ``entries`` 必为空（软失败）。"""

    entries: tuple[dict, ...] = ()
    tokens: int = 0
    error: str | None = None
    query: str = ""

    @property
    def uris(self) -> list[str]:
        return [e["uri"] for e in self.entries]


def build_query(
    phase: str,
    *,
    change_id: str,
    proposal_title: str = "",
    findings_titles: tuple[str, ...] | list[str] = (),
    stack: str = "",
) -> str:
    """构造 ≤200 字符的检索 query。

    implement 用 change_id + proposal 标题 + 语言栈；fix 用 blocking findings 的
    ``category: title`` 拼接 + change_id——findings 已结构化，命中最容易验证。
    """
    if phase == "fix":
        parts = [t.strip() for t in findings_titles if t and t.strip()]
        parts.append(change_id)
    else:
        parts = [change_id, proposal_title.strip(), stack.strip()]
    return " ".join(p for p in parts if p)[:QUERY_MAX_CHARS]


def recall(
    client: Client | None,
    cfg: _config.ExperienceConfig,
    *,
    phase: str,
    query: str,
    exclude_uris: tuple[str, ...] | list[str] = (),
) -> RecallResult:
    """``POST /api/v1/search/search``，只保留 experiences 且正文非空的条目。

    ``rewrite`` / ``query_expansion`` 一律关闭：它们各加一次 LLM 调用，而召回是
    热路径上的增强项。永不抛——失败即注入块为空串，prompt 照常渲染。
    """
    q = (query or "").strip()[:QUERY_MAX_CHARS]
    if client is None:
        return RecallResult(error="no_client", query=q)
    if not q:
        return RecallResult(error="empty_query", query=q)

    body = {
        "query": q,
        "mode": "context",
        "quotas": {"experiences": cfg.quota_experiences},
        "max_tokens": cfg.inject_max_tokens(phase),
        "score_threshold": cfg.score_threshold,
        "rewrite": False,
        "query_expansion": "off",
        "detail": {"experiences": "full"},
        "exclude_uris": list(exclude_uris),
    }
    try:
        payload = client.post("/api/v1/search/search", body)
    except ExperienceError as e:
        return RecallResult(error=e.code, query=q)

    raw_entries = _result_of(payload).get("entries")
    if not isinstance(raw_entries, list):
        return RecallResult(query=q)

    entries: list[dict] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        uri = str(item.get("uri") or "")
        if EXPERIENCE_URI_MARKER not in uri:
            continue
        score = float(item.get("score") or 0.0)
        text = str(item.get("text") or "").strip()
        tier = str(item.get("detail") or "")
        # 服务端按总预算分档：预算紧时只回 uri / abstract / overview 档，而 coder 需要的
        # 是 Approach / Reflect 规则正文。非 full 档按 uri 单独读全文，预算由 render_block
        # 以"按 score 从低到高丢弃整条"的方式在本地执行，不做截断。
        if tier != "full":
            full = _read_full(client, uri)
            if full:
                text, tier = full, "full"
        if not text:
            continue
        entries.append({"uri": uri, "score": score, "text": text, "detail": tier})
    entries.sort(key=lambda e: e["score"], reverse=True)
    tokens = sum(estimate_tokens(e["text"]) for e in entries)
    return RecallResult(entries=tuple(entries), tokens=tokens, query=q)


def _read_full(client: Client, uri: str) -> str:
    """``GET /api/v1/content/read?uri=`` 取经验正文（已剥离 MEMORY_FIELDS 注释）；失败返回空串。"""
    try:
        payload = client.get("/api/v1/content/read", params={"uri": uri})
    except ExperienceError:
        return ""
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        result = result.get("content") or result.get("text") or ""
    return str(result or "").strip()


BLOCK_HEADING = "## 历史经验（外部召回，非本 change 的规格）"
BLOCK_FOOTER = """以上是从历史 run 召回的先验参考，不是本次任务的需求，也不是验收标准。
与 spec.md / proposal.md 冲突时，一律以 spec 为准。
其中的文件路径与 commit 可能已过期，以 repo 当前状态为准。
不要把本节内容抄进 summary 文件。"""


def render_block(result: RecallResult, max_tokens: int | None = None) -> str:
    """渲染注入块外壳（蓝本 §3.5）。空结果返回 ``""``。

    外壳由 npc 控制而非用服务端 ``rendered``：它同时是回抄检测的锚点。
    超预算时按 score 从低到高丢弃条目——低分条目的边际价值最低。
    """
    entries = list(result.entries)
    if max_tokens is not None:
        while entries and sum(estimate_tokens(e["text"]) for e in entries) > max_tokens:
            entries.pop()  # entries 已按 score 降序，末位即最低分
    if not entries:
        return ""

    parts = [BLOCK_HEADING, ""]
    for e in entries:
        parts.append(f'<npc-experience uri="{e["uri"]}" score="{e["score"]:.2f}">')
        parts.append(e["text"])
        parts.append("</npc-experience>")
        parts.append("")
    parts.append(BLOCK_FOOTER)
    return "\n".join(parts) + "\n"


def injection_record_stem(phase: str, round_n: int | None) -> str:
    """``<phase>`` 或 ``<phase>-rN``——注入回执与注入块共用此文件名主干。"""
    return f"{phase}-r{round_n}" if round_n else phase


def write_injection_record(
    base: Path,
    phase: str,
    round_n: int | None,
    result: RecallResult,
    head: str | None,
    block: str | None = None,
) -> Path:
    """落盘注入回执，使 prompt 可完整重放（不变量 2）。

    记 uri / score / tokens / 当时 HEAD / 注入块 sha256——没有这些，「经验是否有用」
    与「哪条经验导致了哪次错误修复」都不可回溯。``block`` 省略时按 result 重新渲染；
    调用方已做预算裁剪时应把实际写盘的块传进来，保证 sha 与 tokens 描述的是真正
    注入的内容。
    """
    text = render_block(result) if block is None else block
    payload = {
        "phase": phase,
        "round": round_n,
        "query": result.query,
        "uris": [{"uri": e["uri"], "score": e["score"]} for e in result.entries],
        "tokens": estimate_tokens(text),
        "head": head,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "ts": _io.now_iso(),
    }
    return _write_json(
        base / f"{injection_record_stem(phase, round_n)}.experience.json", payload
    )


def read_injection_records(base: Path) -> list[dict]:
    """读 ``<base>/*.experience.json`` 注入回执（跳过 commit 回执与坏 JSON）。

    每条附带 ``block_text``（同名 ``.experience.md`` 的正文），供
    :func:`detect_contamination` 做片段比对；JSON 本身保持精简，不重复存正文。
    """
    out: list[dict] = []
    try:
        children = sorted(base.glob("*.experience.json"))
    except OSError:
        return out
    for path in children:
        try:
            data = json.loads(_read_text(path) or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "phase" not in data:
            continue
        data["block_text"] = _read_text(path.with_suffix(".md"))
        out.append(data)
    return out


def detect_contamination(summary_text: str, injection_records: list[dict]) -> bool:
    """回抄检测：summary 是否把注入的经验抄了回来。

    三种命中：``<npc-experience`` 字面；任一注入 uri 字面；任一注入正文的连续
    ≥120 字符片段。命中即该 change 轨迹不提交为经验——这是切断自激环最高性价比
    的一环（一条被自己抄回去的经验会在后续每个 change 里被反复放大）。
    """
    if not summary_text:
        return False
    if INJECTION_TAG in summary_text:
        return True
    for rec in injection_records:
        for item in rec.get("uris") or []:
            uri = item.get("uri") if isinstance(item, dict) else item
            if uri and str(uri) in summary_text:
                return True
        body = rec.get("block_text") or ""
        if body and _has_common_span(summary_text, str(body), CONTAMINATION_SPAN):
            return True
    return False


def _has_common_span(haystack: str, needle: str, span: int) -> bool:
    """needle 是否有长度 ≥span 的连续片段出现在 haystack 中。"""
    if len(needle) < span:
        return False
    for i in range(0, len(needle) - span + 1):
        if needle[i : i + span] in haystack:
            return True
    return False


# ============================================================
# (e) 状态 / 探活
# ============================================================


def list_experiences(client: Client) -> int | None:
    """本 user scope 下的 experiences 计数；端点不可用返回 ``None``。"""
    try:
        payload = client.get(
            "/api/v1/fs/ls", {"uri": "viking://~/memories/experiences"}
        )
    except ExperienceError:
        return None
    result = payload.get("result")
    if not isinstance(result, list):
        return None
    return sum(1 for item in result if isinstance(item, dict) and not item.get("isDir"))


def _is_local_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def doctor_report(cfg: _config.ExperienceConfig, repo_root: Path | None = None) -> dict:
    """详细探活：配置 → /health → agent_evolution → experiences 计数 → 声明比对。

    ``required=False``：本项永不让任何 phase exit 4，doctor 自身对 health 不通才
    返回非 0。
    """
    env_path = expand_path(cfg.env_file, repo_root)
    env = parse_env_file(env_path)
    base_url = resolve_base_url(cfg, env)
    warnings: list[str] = []
    notes: list[str] = []

    report: dict = {
        "enabled": cfg.enabled,
        "env_file": str(env_path),
        "env_file_found": env_path.is_file(),
        "base_url": base_url,
        "health": None,
        "agent_evolution": None,
        "experiences_count": None,
        "warnings": warnings,
        "notes": notes,
    }

    client = from_config(cfg, repo_root)
    if client is None:
        warnings.append(
            f"未从 {env_path} 读到 {cfg.api_key_env}；经验层将静默降级为不可用"
        )
    else:
        try:
            health = _health_summary(probe_health(client))
            report["health"] = health
            if health.get("auth_mode") != "api_key":
                warnings.append(
                    f"server auth_mode={health.get('auth_mode')!r}（非 api_key = dev 模式，"
                    "本机任意进程具 ROOT 权限）"
                )
            report["experiences_count"] = list_experiences(client)
            if report["experiences_count"] is None:
                notes.append("experiences 计数端点不可用（GET /api/v1/fs/ls）")
        except ExperienceError as e:
            report["health"] = {"ok": False, "error": e.code, "message": e.message}

    if not _is_local_url(base_url):
        warnings.append(f"base_url 非本地地址：{base_url}（数据将流出本机）")

    report["agent_evolution"] = _probe_agent_evolution(cfg, repo_root, base_url, notes)
    if report["agent_evolution"] is False:
        warnings.append("服务端 agent_evolution.enabled=false，commit 不会产出 experiences")

    if not cfg.extraction_model_declared:
        warnings.append(
            "[experience].extraction_model_declared 未声明；"
            "抽取属「分析」，模型档位不得低于 coder（不变量 4），无声明即不可核对"
        )
    return report


def _health_summary(payload: dict) -> dict:
    return {
        "ok": bool(payload.get("healthy", payload.get("status") == "ok")),
        "version": payload.get("version"),
        "auth_mode": payload.get("auth_mode"),
    }


def _probe_agent_evolution(
    cfg: _config.ExperienceConfig,
    repo_root: Path | None,
    base_url: str,
    notes: list[str],
) -> bool | None:
    """``GET /api/v1/admin/agent-evolution``（root key）；无 root 凭据返回 None。"""
    root_env = parse_env_file(expand_path(cfg.root_env_file, repo_root))
    root_key = (root_env.get(ROOT_API_KEY_ENV) or "").strip()
    if not root_key:
        notes.append(f"无 root 凭据（{cfg.root_env_file}），跳过 agent_evolution 检查")
        return None
    try:
        payload = Client(base_url, root_key, cfg.timeout_recall_ms).get(
            "/api/v1/admin/agent-evolution"
        )
    except ExperienceError as e:
        notes.append(f"agent_evolution 查询失败：{e.code}")
        return None
    enabled = _result_of(payload).get("enabled")
    return bool(enabled) if isinstance(enabled, bool) else None


# ============================================================
# 命令面
# ============================================================


def _load_ctx(args: argparse.Namespace):
    """公共前奏：resolve Paths + 加载 config。失败经 emit_error 退出。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        raise SystemExit(3)  # pragma: no cover - emit_error 已退出
    try:
        cfg = _config.load_config(p.repo_root)
    except _config.ConfigError as e:
        _io.emit_error("config_error", f"配置加载失败：{e}", exit_code=1)
        raise SystemExit(1)  # pragma: no cover
    return p, cfg.experience


def _entry_for(p, seq: int) -> dict | None:
    from .state import read_state

    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    progress = state.get("progress") or []
    for e in progress:
        if e.get("seq") == seq:
            return e
    return None


def _skip(reason: str, *, strict: bool, **extra) -> None:
    """未启用 / 无凭据 / 闸门不通过的统一出口：非 strict 一律 exit 0。"""
    _io.emit({"ok": False, "skipped": True, "reason": reason, **extra})
    if strict:
        raise SystemExit(1)


def cli_commit(args: argparse.Namespace) -> None:
    """``npc experience commit --seq N [--dry-run] [--strict] [--gate verified|any]``。"""
    p, cfg = _load_ctx(args)
    strict = bool(getattr(args, "strict", False))
    dry_run = bool(getattr(args, "dry_run", False))

    if not cfg.enabled:
        _skip("disabled", strict=strict)
        return

    entry = _entry_for(p, args.seq)
    if entry is None:
        _io.emit_error("seq_not_found", f"state 中无 seq={args.seq} 的条目", exit_code=3)
        return

    gate = getattr(args, "gate", None) or cfg.write_gate
    ok, reason = write_gate_ok(entry, gate)
    if not ok:
        _skip(reason, strict=strict, seq=args.seq, gate=gate)
        return

    client = None if dry_run else from_config(cfg, p.repo_root, timeout_ms=cfg.timeout_commit_ms)
    if client is None and not dry_run:
        _skip("no_client", strict=strict, seq=args.seq)
        return

    result = commit(client, cfg, p_like=p, seq=args.seq, entry=entry, dry_run=dry_run)
    _io.emit({**result, "seq": args.seq, "change_id": entry.get("change_id")})
    if not result.get("ok") and strict:
        raise SystemExit(1)


def cli_recall(args: argparse.Namespace) -> None:
    """``npc experience recall --phase implement|fix --seq N [--round M] [--query TEXT]``。"""
    p, cfg = _load_ctx(args)
    strict = bool(getattr(args, "strict", False))
    phase = args.phase
    round_n = getattr(args, "round_n", None)

    if not cfg.enabled:
        _skip("disabled", strict=strict)
        return

    entry = _entry_for(p, args.seq)
    if entry is None:
        _io.emit_error("seq_not_found", f"state 中无 seq={args.seq} 的条目", exit_code=3)
        return

    change_id = entry.get("change_id") or ""
    base = Path(entry.get("base") or _paths.base_for(p, args.seq, change_id))
    query = getattr(args, "query", None) or _auto_query(p, base, entry, phase, change_id)

    client = from_config(cfg, p.repo_root, timeout_ms=cfg.timeout_recall_ms)
    exclude = _already_injected_uris(p, args.seq)
    result = recall(client, cfg, phase=phase, query=query, exclude_uris=exclude)

    block = render_block(result, max_tokens=cfg.inject_max_tokens(phase))
    stem = injection_record_stem(phase, round_n)
    base.mkdir(parents=True, exist_ok=True)
    md_path = base / f"{stem}.experience.md"
    md_path.write_text(block, encoding="utf-8")
    record = write_injection_record(
        base, phase, round_n, result, _head_of(p.repo_root), block=block
    )

    _io.emit(
        {
            "ok": result.error is None,
            "entries": len(result.entries),
            "tokens": estimate_tokens(block),
            "uris": result.uris,
            "path": str(md_path),
            "record": str(record),
            "error": result.error,
        }
    )
    if result.error is not None and strict:
        raise SystemExit(1)


def _auto_query(p, base: Path, entry: dict, phase: str, change_id: str) -> str:
    """未传 --query 时按 entry 自动构造。"""
    if phase == "fix":
        titles = [
            f"{f['category']}: {f['title']}"
            for f in collect_findings(base)
            if f["title"]
        ]
        return build_query("fix", change_id=change_id, findings_titles=titles[-5:])
    title = proposal_summary(p.repo_root, change_id).splitlines()
    first = next((ln.strip("# ").strip() for ln in title if ln.strip()), "")
    return build_query("implement", change_id=change_id, proposal_title=first)


def _already_injected_uris(p, seq: int) -> list[str]:
    """本 run 内已注入过的 uri（作为 exclude_uris，避免反复注入同一条）。"""
    out: list[str] = []
    seen: set[str] = set()
    from .state import read_state

    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return out
    for e in state.get("progress") or []:
        change_id = e.get("change_id") or ""
        base = Path(e.get("base") or _paths.base_for(p, e.get("seq") or seq, change_id))
        for rec in read_injection_records(base):
            for item in rec.get("uris") or []:
                uri = item.get("uri") if isinstance(item, dict) else item
                if uri and uri not in seen:
                    seen.add(str(uri))
                    out.append(str(uri))
    return out


def _head_of(repo_root: Path) -> str | None:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def cli_status(args: argparse.Namespace) -> None:
    """``npc experience status [--seq N]``：各 change 的经验流状态汇总。"""
    p, cfg = _load_ctx(args)
    from .state import read_state

    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        _io.emit_error("state_missing", f"读取 state 失败：{e}", exit_code=3)
        return

    only = getattr(args, "seq", None)
    client = from_config(cfg, p.repo_root) if cfg.enabled else None

    items: list[dict] = []
    for e in state.get("progress") or []:
        seq = e.get("seq")
        if only is not None and seq != only:
            continue
        change_id = e.get("change_id") or ""
        base = Path(e.get("base") or _paths.base_for(p, seq or 0, change_id))
        items.append(_status_of(base, seq, change_id, client))

    _io.emit({"ok": True, "enabled": cfg.enabled, "items": items})


def _status_of(base: Path, seq, change_id: str, client: Client | None) -> dict:
    commit_path = base / "experience.commit.json"
    committed_raw: dict = {}
    if commit_path.is_file():
        try:
            loaded = json.loads(_read_text(commit_path) or "{}")
            committed_raw = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            committed_raw = {}

    task_id = committed_raw.get("task_id")
    task_status = None
    if task_id and client is not None:
        try:
            task_status = _result_of(fetch_task(client, str(task_id))).get("status")
        except ExperienceError:
            task_status = None

    records = read_injection_records(base)
    return {
        "seq": seq,
        "change_id": change_id,
        "committed": bool(committed_raw.get("ok")),
        "task_id": task_id,
        "task_status": task_status,
        "injected_count": sum(len(r.get("uris") or []) for r in records),
        "injected_tokens": sum(int(r.get("tokens") or 0) for r in records),
    }


def cli_doctor(args: argparse.Namespace) -> None:
    """``npc experience doctor``：health 不通 exit 1，其余 warn exit 0。"""
    try:
        repo_root = _paths.detect_repo_root()
    except _paths.PathsError:
        repo_root = Path.cwd()
    try:
        cfg = _config.load_config(repo_root).experience
    except _config.ConfigError as e:
        _io.emit_error("config_error", f"配置加载失败：{e}", exit_code=1)
        return

    report = doctor_report(cfg, repo_root)
    health = report.get("health")
    healthy = bool(health and health.get("ok"))
    _io.emit({"ok": healthy, **report})
    if not healthy:
        raise SystemExit(1)
