"""把各部分接起来：OneBot 上报 → 事件库 → 叫醒她；管理台；定时探活。"""
from __future__ import annotations

import asyncio
import logging
import time

from .agent import Agent
from .config import ROOT, Config
from .console import Console
from .gate import Gate
from .httpserver import Response, Server, json_response
from .llm import DeepSeek
from .onebot import OneBot, OneBotError, parse_message_event
from .store import Store

log = logging.getLogger(__name__)

OWNER_COMMANDS = {"暂停": True, "继续": False}


class PortInUse(Exception):
    pass
PROBE_EVERY = 60  # 秒


def load_system_prompt(cfg: Config) -> str:
    """框架规矩 + 人设 + 主人是哪个号。都是固定文本，放在最前面，好命中缓存。"""
    parts = [(ROOT / "prompts" / "system.md").read_text(encoding="utf-8"),
             (ROOT / "persona.md").read_text(encoding="utf-8")]
    if owner := cfg.gate.owner_qq:
        parts.append(f"## 认人\n- {{owner_name}}的 QQ 号是 {owner}，你们的私聊是 private:{owner}。")
    return "\n\n".join(p.strip() for p in parts).replace("{owner_name}", cfg.gate.owner_name)


class App:
    def __init__(self, cfg: Config, llm=None, system_prompt: str | None = None):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.started = time.time()
        self.store = Store(cfg.data_dir / "events.db")
        self.onebot = OneBot(cfg.onebot)
        self.gate = Gate(cfg.gate)
        self.agent = Agent(cfg, self.store, self.onebot, self.gate, llm or DeepSeek(cfg.llm),
                           system_prompt if system_prompt is not None else load_system_prompt(cfg))
        self.receiver = Server(cfg.onebot.listen_host, cfg.onebot.listen_port, self.on_onebot)
        self.console = Console(cfg, self)
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        for name, server in (("[onebot] listen_port", self.receiver), ("[console] port", self.console.server)):
            try:
                await server.start()
            except OSError as e:
                await self.receiver.close()
                self.store.close()
                raise PortInUse(f"端口 {server.port} 用不了（{e.strerror}）。换一个：改 config.toml 里的 {name}") from None
        self._tasks = [asyncio.create_task(self.agent.run()), asyncio.create_task(self._probe_loop())]
        log.info("OneBot 上报地址：http://%s:%s/", self.cfg.onebot.listen_host, self.receiver.port)
        log.info("管理台：http://%s:%s/ （口令在 %s）", self.cfg.console.host, self.console.server.port,
                 self.console.token_path)

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.receiver.close()
        await self.console.server.close()
        self.store.close()

    async def run_forever(self):
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    # ── 暂停 / 继续 ───────────────────────────────────
    def set_paused(self, paused: bool, by: str):
        if self.gate.paused == paused:
            return
        self.gate.paused = paused
        self.store.add_feed("info", f"{'已暂停' if paused else '继续运转'}（{by}）")
        if not paused:
            self.agent.nudge()

    # ── OneBot 上报 ───────────────────────────────────
    async def on_onebot(self, req) -> Response:
        if req.method != "POST":
            return json_response({"error": "只收 POST"}, 405)
        if not self.onebot.verify(req.body, req.headers.get("x-signature", "")):
            return json_response({"error": "签名不对"}, 401)
        try:
            evt = req.json()
        except ValueError:
            return json_response({"error": "不是 JSON"}, 400)
        self.onebot.last_event = time.time()
        if evt.get("self_id"):
            self.onebot.self_id = str(evt["self_id"])
        if m := parse_message_event(evt):
            await self.handle_message(m)
        return Response(204)

    def _call_reason(self, m: dict, reply) -> str:
        """这条消息是不是在叫她（受话人识别）：私聊、@ 她、回复她的消息、喊她名字。不是就返回空。"""
        if m["conv"].startswith("private:"):
            return "私聊"
        if m["mentions_me"]:
            return "有人 @ 你"
        if reply is not None and reply["mine"]:
            return "有人回复你"
        if any(n and n in m["text"] for n in self.cfg.inbox.call_names):
            return "有人叫你名字"
        return ""

    async def handle_message(self, m: dict):
        conv = m["conv"]
        reply = self.store.by_msg_id(conv, m["reply_to"])
        reason = self._call_reason(m, reply)
        text = m["text"]
        if reply is not None:  # 把"[回复]"换成回复的是谁、哪句话，她不用翻也看得懂
            quoted = " ".join(reply["text"].split())
            quoted = quoted[:20] + "…" if len(quoted) > 20 else quoted
            text = text.replace("[回复]", f"[回复 {'我' if reply['mine'] else reply['sender_name']}：{quoted}]", 1)
        self.store.add_event(conv, text, ts=m["ts"], sender_id=m["sender_id"],
                             sender_name=m["sender_name"], msg_id=m["msg_id"],
                             mentions_me=bool(reason) and conv.startswith("group:"), raw=m["raw"])
        owner = self.cfg.gate.owner_qq
        if owner and conv == f"private:{owner}" and m["text"] in OWNER_COMMANDS:
            paused = OWNER_COMMANDS[m["text"]]
            self.store.mark_read(conv)  # 指令是给系统的，不算她的未读
            self.set_paused(paused, f"{self.cfg.gate.owner_name}私聊")
            try:  # 这是系统回执，不经过她，也不受闸门限制
                await self.onebot.send_text(conv, "已暂停。私聊发「继续」恢复。" if paused else "继续了。")
            except OneBotError as e:
                self.store.add_feed("error", f"暂停回执没发出去：{e}")
            return
        if not self.gate.allowed(conv):
            return
        if reason:
            self.agent.wake(conv, reason)
        else:
            self.agent.note_group(conv)

    async def _probe_loop(self):
        while True:
            try:
                await self.onebot.probe()
            except OneBotError:
                pass  # 错误已经记在 onebot.last_error，管理台会显示
            await asyncio.sleep(PROBE_EVERY)
