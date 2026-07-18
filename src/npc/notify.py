"""npc notify —— best-effort webhook 推送（永不打断 run）。

编排 skill（v2/v3）在 implementer 完成 / 波次收尾 / change 归档 / run 收尾等时刻
调用本命令，把进度 POST 到外部 webhook（CI、群机器人、自建服务）。

设计规则：

- **无论何种失败都 exit 0**。webhook 挂了 / 超时 / 4xx 绝不允许中断 pipeline，
  失败只写 stderr 警告 + stdout 的 ``delivered:false``。
- URL 解析顺序：``--url`` > ``$NPC_WEBHOOK`` > ``$NPC_V3_WEBHOOK``（兼容 v3 旧名）。
  都为空则静默 no-op（通知未启用，不算失败）。
- stdlib only（urllib），默认 5s 超时。
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

from . import _io

FORMATS = ("raw", "slack", "feishu")


def build_payload(event: str, fields: dict, text: str, fmt: str) -> dict:
    """纯函数：按 payload 形状组装。

    raw    → ``{"event":.., "text":.., <kv...>}``
    slack  → ``{"text": "<text>"}``（Slack incoming webhook）
    feishu → ``{"msg_type":"text","content":{"text":"<text>"}}``（飞书自定义机器人）
    """
    if not text:
        bits = " ".join(f"{k}={v}" for k, v in fields.items())
        text = f"[npc] {event} {bits}".strip()
    if fmt == "slack":
        return {"text": text}
    if fmt == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"event": event, "text": text, **fields}


def parse_kv(pairs: list[str]) -> dict:
    fields: dict[str, str] = {}
    for pair in pairs:
        if "=" in pair:
            k, v = pair.split("=", 1)
            fields[k] = v
    return fields


def post(url: str, payload: dict, timeout: float, opener=urllib.request.urlopen) -> bool:
    """POST JSON；返回是否 2xx。任何异常都吞掉并 warn（永不上抛）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with opener(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", None) or resp.getcode())
            if 200 <= code < 300:
                return True
            _io.warn(f"notify: webhook 返回 HTTP {code}（已忽略）")
            return False
    except urllib.error.URLError as e:
        _io.warn(f"notify: webhook POST 失败：{e}（已忽略）")
        return False
    except Exception as e:  # 通知绝不允许打断 run
        _io.warn(f"notify: 未预期错误：{e}（已忽略）")
        return False


def run(args: argparse.Namespace) -> None:
    """``npc notify --event KIND [--url URL] [--format ...] [--kv k=v ...] [--text ...]``

    退出码：**总是 0**。stdout 单行 JSON ``{ok, event, url_set, delivered}``。
    """
    url = args.url or os.environ.get("NPC_WEBHOOK", "") or os.environ.get("NPC_V3_WEBHOOK", "")
    fields = parse_kv(args.kv)

    if not url:
        _io.emit({"ok": True, "event": args.event, "url_set": False, "delivered": False})
        return

    payload = build_payload(args.event, fields, args.text, args.format)
    delivered = post(url, payload, args.timeout)
    _io.emit({"ok": True, "event": args.event, "url_set": True, "delivered": delivered})
