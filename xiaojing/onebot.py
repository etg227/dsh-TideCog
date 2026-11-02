"""OneBot v11，走 HTTP：调动作发给 SnowLuma 的 HTTP 服务器，事件由 SnowLuma 的 HTTP 上报推过来。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request

CQ_AT = re.compile(r"\[CQ:at,qq=(\d+|all)[^\]]*\]")
CQ_ANY = re.compile(r"\[CQ:([a-z_]+)[^\]]*\]")
SEGMENT_LABELS = {"face": "[表情]", "image": "[图片]", "record": "[语音]", "video": "[视频]",
                  "reply": "[回复]", "forward": "[合并转发]", "file": "[文件]", "mface": "[表情]"}


class OneBotError(Exception):
    pass


class OneBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.self_id = ""
        self.nickname = ""
        self.last_ok = 0.0      # 上次调动作成功
        self.last_event = 0.0   # 上次收到上报
        self.last_error = ""
        self.last_error_ts = 0.0

    async def call(self, action: str, params: dict):
        return await asyncio.to_thread(self._call, action, params)

    def _call(self, action, params):
        headers = {"Content-Type": "application/json"}
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        req = urllib.request.Request(
            f"{self.cfg.api_url.rstrip('/')}/{action}",
            data=json.dumps(params, ensure_ascii=False).encode(), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                data = json.loads(res.read())
        except urllib.error.HTTPError as e:
            return self._fail(f"{action}：HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            return self._fail(f"{action}：{getattr(e, 'reason', e)}")
        if data.get("retcode", 0) != 0:
            return self._fail(f"{action}：retcode {data.get('retcode')} {data.get('message') or data.get('wording') or ''}".strip())
        self.last_ok = time.time()
        return data.get("data")

    def _fail(self, msg):
        self.last_error, self.last_error_ts = msg, time.time()
        raise OneBotError(msg)

    async def send_text(self, conv: str, text: str) -> str:
        """发一条纯文本。用消息段数组发，她写的 [CQ:...] 不会被当成指令。"""
        kind, _, cid = conv.partition(":")
        message = [{"type": "text", "data": {"text": text}}]
        if kind == "group":
            data = await self.call("send_group_msg", {"group_id": int(cid), "message": message})
        elif kind == "private":
            data = await self.call("send_private_msg", {"user_id": int(cid), "message": message})
        else:
            raise OneBotError(f"不认识的会话：{conv}")
        return str((data or {}).get("message_id", ""))

    async def probe(self):
        """探活，顺便拿到自己的 QQ 号。只探活，不启动 SnowLuma（它的条款不允许脚本部署）。"""
        data = await self.call("get_login_info", {})
        self.self_id = str((data or {}).get("user_id", self.self_id))
        self.nickname = (data or {}).get("nickname", self.nickname)

    def verify(self, body: bytes, signature: str) -> bool:
        """校验上报签名（X-Signature: sha1=...）。没配 secret 就不校验。"""
        if not self.cfg.secret:
            return True
        expected = "sha1=" + hmac.new(self.cfg.secret.encode(), body, hashlib.sha1).hexdigest()
        return hmac.compare_digest(expected, signature or "")


def _unescape_cq(text: str) -> str:
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def parse_message_event(evt: dict) -> dict | None:
    """把一条 OneBot 消息上报变成我们的格式；不是群聊或私聊消息就返回 None。"""
    if evt.get("post_type") != "message":
        return None
    mtype = evt.get("message_type")
    if mtype == "group":
        conv = f"group:{evt.get('group_id')}"
    elif mtype == "private":
        conv = f"private:{evt.get('user_id')}"
    else:
        return None

    self_id = str(evt.get("self_id", ""))
    msg = evt.get("message")
    mentions = False
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            kind, data = seg.get("type"), seg.get("data") or {}
            if kind == "text":
                parts.append(data.get("text", ""))
            elif kind == "at":
                qq = str(data.get("qq", ""))
                mentions = mentions or qq == self_id
                parts.append("@全体成员" if qq == "all" else f"@{data.get('name') or qq}")
            else:
                parts.append(SEGMENT_LABELS.get(kind, f"[{kind}]"))
        text = "".join(parts)
    else:
        raw = str(msg if msg is not None else evt.get("raw_message", ""))
        mentions = self_id in CQ_AT.findall(raw)
        text = CQ_AT.sub(lambda m: "@全体成员" if m.group(1) == "all" else f"@{m.group(1)}", raw)
        text = CQ_ANY.sub(lambda m: SEGMENT_LABELS.get(m.group(1), f"[{m.group(1)}]"), text)
        text = _unescape_cq(text)

    sender = evt.get("sender") or {}
    return {
        "conv": conv,
        "text": text.strip(),
        "ts": float(evt.get("time") or time.time()),
        "sender_id": str(evt.get("user_id", "")),
        "sender_name": sender.get("card") or sender.get("nickname") or str(evt.get("user_id", "")),
        "msg_id": str(evt.get("message_id", "")),
        "mentions_me": mentions,
        "self_id": self_id,
        "raw": evt,
    }
