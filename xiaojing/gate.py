"""权限闸门：她每次想看、想发都要过这里。规则写在 config.toml，只有主人能改，她自己改不了。"""
from __future__ import annotations

import time
from collections import deque


class Gate:
    def __init__(self, cfg):
        self.cfg = cfg
        self.paused = False
        self.opened: set[str] = set()          # 这一圈 open 过的会话
        self._sent: dict[str, deque] = {}      # 会话 -> 最近一分钟的发送时间
        self._sent_all: deque = deque()

    def allowed(self, conv: str) -> bool:
        kind, _, cid = conv.partition(":")
        return (kind == "group" and cid in self.cfg.allow_groups) or \
               (kind == "private" and cid in self.cfg.allow_users)

    def new_turn(self):
        self.opened.clear()

    def check_open(self, conv: str) -> str | None:
        if not self.allowed(conv):
            return f"{conv} 不在允许名单里"
        return None

    def mark_opened(self, conv: str):
        self.opened.add(conv)

    def check_send(self, conv: str, text: str, now: float | None = None) -> str | None:
        """能发就返回 None，不能发就返回原因（原因会原样告诉她）。"""
        now = time.time() if now is None else now
        if self.paused:
            return "现在是暂停状态，不能发消息"
        if not self.allowed(conv):
            return f"{conv} 不在允许名单里"
        if conv not in self.opened:
            return f"这一圈还没 open 过 {conv}，先 open 看看再发"
        if not text.strip():
            return "内容是空的"
        if len(text) > self.cfg.max_text_len:
            return f"太长了（{len(text)} 字），一条最多 {self.cfg.max_text_len} 字"
        recent = self._sent.setdefault(conv, deque())
        for q in (recent, self._sent_all):
            while q and now - q[0] >= 60:
                q.popleft()
        if len(recent) >= self.cfg.per_conv_per_minute:
            return "这个会话一分钟内发得太多了，缓一缓"
        if len(self._sent_all) >= self.cfg.global_per_minute:
            return "这一分钟发得太多了，缓一缓"
        return None

    def record_send(self, conv: str, now: float | None = None):
        now = time.time() if now is None else now
        self._sent.setdefault(conv, deque()).append(now)
        self._sent_all.append(now)
