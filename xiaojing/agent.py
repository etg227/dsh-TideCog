"""唯一会话。被叫醒后转一圈：读取上下文 → 思考与选择 → 执行（调工具）→ 结束。

第 1 步只接了 open / send 两个工具；收件箱、心跳、记忆在后面几步加。
她写在回复正文里的话是心里想的，只有 send 出去的才会发到 QQ。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from .gate import Gate
from .llm import LLMError
from .onebot import OneBotError
from .prices import bj_time, cost

log = logging.getLogger(__name__)

WAKE_MARK = "【唤醒"

TOOLS = [
    {"type": "function", "function": {
        "name": "open",
        "description": "打开一个对话，看最近的消息，顺便标成已读。往一个对话发消息之前，这一圈里必须先打开它。",
        "parameters": {"type": "object", "properties": {
            "conv": {"type": "string", "description": "会话，比如 group:123456 或 private:123456"},
            "n": {"type": "integer", "description": "看最近几条，默认 20，最多 50"},
        }, "required": ["conv"]},
    }},
    {"type": "function", "function": {
        "name": "send",
        "description": "往一个对话发一条 QQ 消息。只发你真想说出口的话；不想回可以不发。",
        "parameters": {"type": "object", "properties": {
            "conv": {"type": "string", "description": "会话，比如 group:123456"},
            "text": {"type": "string", "description": "要发的话，纯文本"},
        }, "required": ["conv", "text"]},
    }},
]


class Agent:
    def __init__(self, cfg, store, onebot, gate: Gate, llm, system_prompt: str):
        self.cfg, self.store, self.onebot, self.gate, self.llm = cfg, store, onebot, gate, llm
        self.system_prompt = system_prompt
        self.session_path: Path = cfg.data_dir / "session.jsonl"
        self.archive_path: Path = cfg.data_dir / "session-archive.jsonl"
        self.history: list[dict] = self._load()
        self.pending: dict[str, str] = {}   # 会话 -> 为什么叫醒她
        self.running = False
        self.llm_status = {"ok": None, "last_ts": 0.0, "last_error": ""}
        self._wake = asyncio.Event()

    # ── 叫醒 ──────────────────────────────────────────
    def wake(self, conv: str, reason: str):
        old = self.pending.get(conv)
        self.pending[conv] = reason if not old or reason == old else f"{old}；{reason}"
        self._wake.set()

    def nudge(self):
        """暂停结束后调用：有积压就接着转。"""
        if self.pending:
            self._wake.set()

    async def run(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self.gate.paused or not self.pending:
                continue
            batch, self.pending = self.pending, {}
            self.running = True
            try:
                await self.turn(batch)
            except Exception as e:
                log.exception("这一圈出错了")
                self.store.add_feed("error", f"这一圈出错了：{e}")
            finally:
                self.running = False
            if self.pending:
                self._wake.set()

    # ── 一圈 ──────────────────────────────────────────
    async def turn(self, batch: dict[str, str]):
        self.gate.new_turn()
        lines = []
        for conv, reason in batch.items():
            lines.append(f"- {conv}：{self.store.unread(conv)} 条未读（{reason}）")
        wake = (f"{WAKE_MARK} {bj_time(time.time())}】有新消息：\n" + "\n".join(lines)
                + "\n想看就 open，想说就 send；不想回也可以什么都不做。")
        self._append({"role": "user", "content": wake})
        self.store.add_feed("wake", "；".join(f"{c}（{r}）" for c, r in batch.items()))

        for _ in range(self.cfg.llm.max_steps):
            try:
                msg, usage = await self.llm.chat(self._messages(), TOOLS)
            except LLMError as e:
                self.llm_status.update(ok=False, last_ts=time.time(), last_error=str(e))
                self.store.add_feed("error", f"调模型失败：{e}")
                break
            self._record_usage(usage)
            reply = {"role": "assistant", "content": msg.get("content") or ""}
            calls = msg.get("tool_calls") or []
            if calls:
                reply["tool_calls"] = calls
            self._append(reply)
            if reply["content"].strip():
                self.store.add_feed("think", reply["content"].strip())
            if not calls:
                break
            for call in calls:
                result = await self._run_tool(call)
                self._append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        else:
            self.store.add_feed("warn", f"这一圈连续调了 {self.cfg.llm.max_steps} 次模型，强制结束")
        self._evict_if_needed()

    def _messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, *self.history]

    def _record_usage(self, usage: dict):
        now = time.time()
        yuan, peak = cost(usage, self.cfg.llm, now)
        self.store.add_usage(now, usage.get("model", ""), usage["hit"], usage["miss"], usage["out"], yuan, peak)
        self.llm_status.update(ok=True, last_ts=now, last_error="")

    # ── 工具 ──────────────────────────────────────────
    async def _run_tool(self, call: dict) -> str:
        fn = call.get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            return "参数不是合法的 JSON"
        if not isinstance(args, dict):
            return "参数应该是一个对象"
        if name == "open":
            return self._tool_open(args)
        if name == "send":
            return await self._tool_send(args)
        return f"没有叫 {name} 的工具"

    def _tool_open(self, args: dict) -> str:
        conv = str(args.get("conv", "")).strip()
        if err := self.gate.check_open(conv):
            self.store.add_feed("blocked", f"open {conv}：{err}", conv)
            return f"打不开：{err}"
        try:
            n = max(1, min(int(args.get("n", 20)), 50))
        except (TypeError, ValueError):
            n = 20
        rows = self.store.recent(conv, n)
        self.store.mark_read(conv)
        self.gate.mark_opened(conv)
        self.store.add_feed("tool", f"open {conv}（{len(rows)} 条）", conv)
        if not rows:
            return f"{conv} 里还没有消息"
        out = [f"{conv} 最近 {len(rows)} 条："]
        for r in rows:
            who = "我" if r["mine"] else f"{r['sender_name']}({r['sender_id']})"
            out.append(f"[{bj_time(r['ts'])}] {who}：{r['text']}")
        return "\n".join(out)

    async def _tool_send(self, args: dict) -> str:
        conv = str(args.get("conv", "")).strip()
        text = str(args.get("text", ""))
        if err := self.gate.check_send(conv, text):
            self.store.add_feed("blocked", f"send {conv}：{err}", conv)
            return f"没发出去：{err}"
        try:
            msg_id = await self.onebot.send_text(conv, text)
        except OneBotError as e:
            self.store.add_feed("error", f"发到 {conv} 失败：{e}", conv)
            return f"没发出去：QQ 那边出错了（{e}）"
        self.gate.record_send(conv)
        self.store.add_event(conv, text, sender_id=self.onebot.self_id, sender_name="我",
                             msg_id=msg_id, mine=True)
        self.store.add_feed("send", text, conv)
        return "已发出"

    # ── 会话存盘 ──────────────────────────────────────
    def _load(self) -> list[dict]:
        if not self.session_path.exists():
            return []
        with self.session_path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _append(self, msg: dict):
        self.history.append(msg)
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        for path in (self.session_path, self.archive_path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

    def turn_count(self) -> int:
        return sum(1 for m in self.history if m["role"] == "user" and m["content"].startswith(WAKE_MARK))

    def _evict_if_needed(self):
        """圈数超了上限，就把最旧的三分之一整块移出（不总结）。存档里一直留着全文。

        一次移一大块而不是每圈挪一点，是为了让前缀尽量保持不变，多命中缓存。
        """
        turns = self.turn_count()
        if turns <= self.cfg.max_turns:
            return
        drop = max(1, turns // 3)
        seen = 0
        for i, m in enumerate(self.history):
            if m["role"] == "user" and m["content"].startswith(WAKE_MARK):
                if seen == drop:
                    break
                seen += 1
        else:
            return
        self.history = self.history[i:]
        tmp = self.session_path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in self.history), encoding="utf-8")
        tmp.replace(self.session_path)
        self.store.add_feed("info", f"上下文满了：移出最旧的 {drop} 圈，存档里还在")
