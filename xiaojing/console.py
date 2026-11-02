"""管理台：只在本机能开，要口令。她自己调不到这里。"""
from __future__ import annotations

import hmac
import secrets
import time
from importlib import resources

from .httpserver import Response, Server, json_response

COOKIE = "xj_console"


class Console:
    def __init__(self, cfg, app):
        self.cfg, self.app = cfg, app
        self.token_path = cfg.data_dir / "console-token.txt"
        if not self.token_path.exists():
            self.token_path.write_text(secrets.token_urlsafe(18), encoding="utf-8")
        self.token = self.token_path.read_text(encoding="utf-8").strip()
        self.server = Server(cfg.console.host, cfg.console.port, self.handle)

    async def start(self):
        await self.server.start()

    def _authed(self, req) -> bool:
        return hmac.compare_digest(req.cookie(COOKIE), self.token)

    async def handle(self, req) -> Response:
        # 只认本机地址，挡住借别的域名指过来的网页
        port = self.server.port
        if req.headers.get("host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return json_response({"error": "只能从本机打开"}, 403)

        if req.path in ("/", "/setup") and req.method == "GET":
            # 设置页本身不带任何数据，读写都走桌面程序的接口，所以不用登录
            name = "console.html" if req.path == "/" else "setup.html"
            page = resources.files("xiaojing").joinpath(name).read_text(encoding="utf-8")
            return Response(200, page, "text/html; charset=utf-8")

        if req.path == "/api/login" and req.method == "POST":
            try:
                given = str(req.json().get("token", "")).strip()
            except ValueError:
                given = ""
            if not hmac.compare_digest(given, self.token):
                return json_response({"error": "口令不对"}, 401)
            cookie = f"{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
            return json_response({"ok": True}, headers={"Set-Cookie": cookie})

        if not req.path.startswith("/api/"):
            return json_response({"error": "没有这个页面"}, 404)
        if not self._authed(req):
            return json_response({"error": "要先登录"}, 401)

        if req.path == "/api/state" and req.method == "GET":
            return json_response(self.state())
        if req.method == "POST" and req.headers.get("content-type", "").startswith("application/json"):
            if req.path == "/api/pause":
                self.app.set_paused(True, "管理台")
                return json_response(self.state())
            if req.path == "/api/resume":
                self.app.set_paused(False, "管理台")
                return json_response(self.state())
        return json_response({"error": "没有这个接口"}, 404)

    def state(self) -> dict:
        app, ob, agent = self.app, self.app.onebot, self.app.agent
        now = time.time()
        return {
            "now": now,
            "uptime": now - app.started,
            "paused": app.gate.paused,
            "running": agent.running,
            "pending": [{"conv": c, "reason": r} for c, r in agent.pending.items()],
            "group_due": [{"conv": c, "due": d} for c, d in agent.group_due.items()],
            "inbox": app.store.inbox(agent.allowed_convs()),
            "rhythm": {"sleeping": agent.rhythm.sleeping(now), "next_heartbeat": agent.next_heartbeat,
                       "next_wake": agent.rhythm.next_wake(now), "idle_streak": agent.idle_streak,
                       "sleep_start": self.cfg.rhythm.sleep_start, "wake_time": self.cfg.rhythm.wake_time},
            "qq": {"self_id": ob.self_id, "nickname": ob.nickname, "last_ok": ob.last_ok,
                   "last_event": ob.last_event, "last_error": ob.last_error,
                   "last_error_ts": ob.last_error_ts, "api_url": self.cfg.onebot.api_url},
            "llm": {**agent.llm_status, "model": self.cfg.llm.model},
            "dsh": {"status": "later"},
            "cost": app.store.cost_today(now),
            "context": {"tokens": agent.last_prompt_tokens, "budget": self.cfg.context_budget,
                        "turns": agent.turn_count(), "messages": len(agent.history)},
            "allow": {"groups": self.cfg.gate.allow_groups, "users": self.cfg.gate.allow_users,
                      "owner": bool(self.cfg.gate.owner_qq)},
            "feed": app.store.feed(80),
        }
