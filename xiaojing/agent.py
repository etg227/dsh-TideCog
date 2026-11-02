"""唯一会话。被叫醒后转一圈：读取上下文 → 思考与选择 → 执行（调工具）→ 结束。

叫醒她的有三种：有人找她（私聊、@、群里攒够了）、心跳到点、早上睡醒。
她写在回复正文里的话是心里想的，只有 send 出去的才会发到 QQ。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .gate import Gate
from .llm import LLMError
from .onebot import OneBotError
from .prices import BJ, bj_time, cost
from .rhythm import Rhythm

log = logging.getLogger(__name__)

WAKE_MARK = "【唤醒"
MY_LOG_MAX_CHARS = 4000


def _fn(name, description, properties=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
    }}


CONV = {"type": "string", "description": "会话，比如 group:123456 或 private:123456"}
TOOLS = [
    _fn("inbox", "看收件箱：哪些对话有未读、几条、有没有人 @ 你。只给数量，不给内容。"),
    _fn("open", "打开一个对话，看最近的消息，顺便标成已读。往一个对话发消息之前，这一圈里必须先打开它。",
        {"conv": CONV, "n": {"type": "integer", "description": "看最近几条，默认 20，最多 50"}}, ["conv"]),
    _fn("scroll", "在一个对话里往上翻更早的消息。before 填你看到的最早那条的编号（# 后面的数字）。",
        {"conv": CONV, "before": {"type": "integer"}, "n": {"type": "integer", "description": "默认 20，最多 50"}},
        ["conv", "before"]),
    _fn("search", "在聊天记录里搜关键词。三个字以上搜得最准。",
        {"query": {"type": "string"}, "conv": {**CONV, "description": "只搜这个会话；不填就搜全部"}}, ["query"]),
    _fn("send", "往一个对话发一条 QQ 消息。只发你真想说出口的话；不想回可以不发。",
        {"conv": CONV, "text": {"type": "string", "description": "要发的话，纯文本"}}, ["conv", "text"]),
    _fn("my_log", "翻你自己以前某一天想了什么、做了什么（包括已经移出上下文的部分）。",
        {"day": {"type": "string", "description": "今天、昨天，或者 11-02 这种日期"},
         "keyword": {"type": "string", "description": "只看包含这个词的"}}, ["day"]),
    _fn("rest", "这一圈到此为止，过多少分钟再醒。什么都不想做的时候就调它。",
        {"minutes": {"type": "integer", "description": "几分钟后再醒"}}, ["minutes"]),
    _fn("sleep", "去睡觉，到早上起床时间再醒。睡着时只有主人私聊能叫醒你。"),
]


class Agent:
    def __init__(self, cfg, store, onebot, gate: Gate, llm, system_prompt: str):
        self.cfg, self.store, self.onebot, self.gate, self.llm = cfg, store, onebot, gate, llm
        self.system_prompt = system_prompt
        self.rhythm = Rhythm(cfg.rhythm)
        self.session_path: Path = cfg.data_dir / "session.jsonl"
        self.archive_path: Path = cfg.data_dir / "session-archive.jsonl"
        self.history: list[dict] = self._load()
        self.pending: dict[str, str] = {}      # 会话 -> 为什么叫醒她（马上要处理的）
        self.group_due: dict[str, float] = {}  # 群 -> 攒够了、该叫醒她的时间
        self._last_group_wake: dict[str, float] = {}
        self.next_heartbeat = time.time() + cfg.rhythm.heartbeat_default * 60
        self.idle_streak = 0                   # 连着几圈没说话
        self.last_prompt_tokens = 0
        self.running = False
        self.llm_status = {"ok": None, "last_ts": 0.0, "last_error": ""}
        self._was_sleeping = self.rhythm.sleeping(time.time())
        self._wake = asyncio.Event()
        self._turn_end = False
        self._rest_until: float | None = None
        self._sent_this_turn = 0

    @property
    def owner_conv(self) -> str:
        return f"private:{self.cfg.gate.owner_qq}" if self.cfg.gate.owner_qq else ""

    def allowed_convs(self) -> list[str]:
        g = self.cfg.gate
        return [f"group:{x}" for x in g.allow_groups] + [f"private:{x}" for x in g.allow_users]

    # ── 叫醒 ──────────────────────────────────────────
    def wake(self, conv: str, reason: str):
        """有人找她：马上排进下一圈（睡着时只有主人能真的叫醒）。"""
        old = self.pending.get(conv)
        self.pending[conv] = reason if not old or reason in old else f"{old}；{reason}"
        self.group_due.pop(conv, None)
        self._wake.set()

    def note_group(self, conv: str, now: float | None = None):
        """群里来了没 @ 她的消息：先攒着，到点再叫醒，同一个群不叫得太勤。"""
        now = time.time() if now is None else now
        if conv in self.pending or conv in self.group_due:
            return
        ib = self.cfg.inbox
        self.group_due[conv] = max(now + ib.group_batch_seconds,
                                   self._last_group_wake.get(conv, 0) + ib.group_min_interval)
        self._wake.set()

    def nudge(self):
        """暂停结束后调用：有积压或心跳到点就接着转。"""
        self._wake.set()

    def _take_ready(self, now: float):
        """决定这一圈要处理什么。返回 (会话 -> 原因, 叫醒类型)；没事就返回 ({}, None)。"""
        for conv, due in list(self.group_due.items()):
            if now >= due:
                del self.group_due[conv]
                self._last_group_wake[conv] = now
                self.pending.setdefault(conv, "群里有新消息")
        sleeping = self.rhythm.sleeping(now)
        just_woke = self._was_sleeping and not sleeping
        self._was_sleeping = sleeping
        if sleeping:
            ready = {c: r for c, r in self.pending.items() if c == self.owner_conv}
            kind = "message" if ready else None
        else:
            ready = dict(self.pending)
            if just_woke:
                kind = "morning"
            elif ready:
                kind = "message"
            elif now >= self.next_heartbeat:
                kind = "heartbeat"
            else:
                kind = None
        for c in ready:
            del self.pending[c]
        return ready, kind

    def _seconds_until_due(self, now: float) -> float:
        if self.rhythm.sleeping(now):
            candidates = [self.rhythm.next_wake(now)]
        else:
            candidates = [self.next_heartbeat]
        candidates += list(self.group_due.values())
        # 至少每分钟看一眼，好及时发现睡醒了
        return max(0.0, min(min(candidates) - now, 60.0))

    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self._seconds_until_due(time.time()))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self.gate.paused:
                continue
            batch, kind = self._take_ready(time.time())
            if kind is None:
                continue
            self.running = True
            try:
                await self.turn(batch, kind)
            except Exception as e:
                log.exception("这一圈出错了")
                self.store.add_feed("error", f"这一圈出错了：{e}")
            finally:
                self.running = False
            self._wake.set()  # 转的时候可能又来了消息

    # ── 一圈 ──────────────────────────────────────────
    def _wake_text(self, batch: dict[str, str], kind: str, now: float) -> str:
        head = {"message": "", "heartbeat": "心跳：没人找你，到点自己醒了。",
                "morning": "早上了，你刚睡醒。"}[kind]
        lines = [f"{WAKE_MARK} {bj_time(now)}】{head}"]
        if batch:
            lines.append("有新消息：")
            lines += [f"- {c}：{self.store.unread(c)} 条未读（{r}）" for c, r in batch.items()]
        others = [x for x in self.store.inbox(self.allowed_convs()) if x["conv"] not in batch]
        if others:
            lines.append("收件箱里还有：" + "；".join(f"{x['conv']} {x['unread']} 条" for x in others))
        elif not batch:
            lines.append("收件箱是空的。")
        if kind != "message" and self.idle_streak:
            lines.append(f"你已经连续 {self.idle_streak} 圈没说话了。")
        lines.append("想看就 open，想说就 send；什么都不想做就 rest，自己定几分钟后再醒。")
        return "\n".join(lines)

    async def turn(self, batch: dict[str, str], kind: str = "message"):
        now = time.time()
        self.gate.new_turn()
        self._turn_end, self._rest_until, self._sent_this_turn = False, None, 0
        self._append({"role": "user", "content": self._wake_text(batch, kind, now)})
        label = {"message": "", "heartbeat": "心跳", "morning": "睡醒"}[kind]
        self.store.add_feed("wake", "；".join([label] * bool(label) + [f"{c}（{r}）" for c, r in batch.items()]))

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
            if self._turn_end:
                break
        else:
            self.store.add_feed("warn", f"这一圈连续调了 {self.cfg.llm.max_steps} 次模型，强制结束")

        end = time.time()
        r = self.cfg.rhythm
        self.next_heartbeat = self._rest_until or end + r.heartbeat_default * 60
        self.idle_streak = 0 if self._sent_this_turn else self.idle_streak + 1
        self._evict_if_needed()

    def _messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, *self.history]

    def _record_usage(self, usage: dict):
        now = time.time()
        yuan, peak = cost(usage, self.cfg.llm, now)
        self.store.add_usage(now, usage.get("model", ""), usage["hit"], usage["miss"], usage["out"], yuan, peak)
        self.last_prompt_tokens = usage["hit"] + usage["miss"]
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
        handler = {"inbox": self._tool_inbox, "open": self._tool_open, "scroll": self._tool_scroll,
                   "search": self._tool_search, "my_log": self._tool_my_log, "rest": self._tool_rest,
                   "sleep": self._tool_sleep}.get(name)
        if name == "send":
            return await self._tool_send(args)
        if handler is None:
            return f"没有叫 {name} 的工具"
        return handler(args)

    @staticmethod
    def _int(value, default, lo, hi):
        try:
            return max(lo, min(int(value), hi))
        except (TypeError, ValueError):
            return default

    def _format_rows(self, rows) -> list[str]:
        out = []
        for r in rows:
            who = "我" if r["mine"] else f"{r['sender_name']}({r['sender_id']})"
            out.append(f"[#{r['id']} {bj_time(r['ts'])}] {who}：{r['text']}")
        return out

    def _tool_inbox(self, args: dict) -> str:
        items = self.store.inbox(self.allowed_convs())
        self.store.add_feed("tool", f"inbox（{len(items)} 个对话有未读）")
        if not items:
            return "收件箱是空的"
        return "\n".join(
            f"- {x['conv']}：{x['unread']} 条未读，最新 {bj_time(x['last_ts'])}（{x['last_sender']}）"
            + ("，有人 @ 你" if x["mentioned"] else "") for x in items)

    def _tool_open(self, args: dict) -> str:
        conv = str(args.get("conv", "")).strip()
        if err := self.gate.check_open(conv):
            self.store.add_feed("blocked", f"open {conv}：{err}", conv)
            return f"打不开：{err}"
        rows = self.store.recent(conv, self._int(args.get("n"), 20, 1, 50))
        self.store.mark_read(conv)
        self.gate.mark_opened(conv)
        self.store.add_feed("tool", f"open {conv}（{len(rows)} 条）", conv)
        if not rows:
            return f"{conv} 里还没有消息"
        return "\n".join([f"{conv} 最近 {len(rows)} 条："] + self._format_rows(rows))

    def _tool_scroll(self, args: dict) -> str:
        conv = str(args.get("conv", "")).strip()
        if err := self.gate.check_open(conv):
            return f"翻不了：{err}"
        before = self._int(args.get("before"), 0, 0, 2**62)
        rows = self.store.before(conv, before, self._int(args.get("n"), 20, 1, 50))
        self.store.add_feed("tool", f"scroll {conv} #{before} 之前（{len(rows)} 条）", conv)
        if not rows:
            return "再往上没有了"
        return "\n".join([f"{conv} #{before} 之前的 {len(rows)} 条："] + self._format_rows(rows))

    def _tool_search(self, args: dict) -> str:
        query = str(args.get("query", "")).strip()
        conv = str(args.get("conv", "")).strip() or None
        if conv and (err := self.gate.check_open(conv)):
            return f"搜不了：{err}"
        allowed = set(self.allowed_convs())
        rows = [r for r in self.store.search(query, conv, limit=50) if r["conv"] in allowed][:15]
        self.store.add_feed("tool", f"search「{query}」（{len(rows)} 条）", conv)
        if not rows:
            return f"没搜到「{query}」"
        lines = []
        for r in rows:
            who = "我" if r["mine"] else r["sender_name"]
            lines.append(f"[#{r['id']} {bj_time(r['ts'])}] {r['conv']} {who}：{r['text']}")
        return "\n".join(lines)

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
        self._sent_this_turn += 1
        self.store.add_feed("send", text, conv)
        return "已发出"

    def _tool_rest(self, args: dict) -> str:
        r = self.cfg.rhythm
        minutes = self._int(args.get("minutes"), int(r.heartbeat_default), int(r.heartbeat_min), int(r.heartbeat_max))
        self._rest_until = time.time() + minutes * 60
        self._turn_end = True
        self.store.add_feed("tool", f"rest {minutes} 分钟")
        return f"好，{minutes} 分钟后再醒"

    def _tool_sleep(self, args: dict) -> str:
        now = time.time()
        if self.rhythm.next_wake(now) - now > 12 * 3600:
            return "现在离起床时间还早，不能一觉睡到明天早上。想歇一会就用 rest"
        self.rhythm.sleep_until = self.rhythm.next_wake(now)
        self._rest_until = self.rhythm.sleep_until
        self._turn_end = True
        self.store.add_feed("tool", f"去睡了，{bj_time(self.rhythm.sleep_until)} 醒")
        return f"晚安，{bj_time(self.rhythm.sleep_until)} 醒"

    def _tool_my_log(self, args: dict) -> str:
        day = parse_day(str(args.get("day", "")), time.time())
        if day is None:
            return "日期看不懂，写今天、昨天，或者 11-02 这种"
        keyword = str(args.get("keyword", "")).strip()
        lines = my_log_lines(self.archive_path, day, keyword)
        self.store.add_feed("tool", f"my_log {day:%m-%d}" + (f"「{keyword}」" if keyword else ""))
        if not lines:
            return f"{day:%m-%d} 没有记录" + (f"（含「{keyword}」的）" if keyword else "")
        text = "\n".join(lines)
        if len(text) > MY_LOG_MAX_CHARS:  # 太长就留最后的部分
            text = "……（更早的省略了）\n" + text[-MY_LOG_MAX_CHARS:]
        return text

    # ── 会话存盘 ──────────────────────────────────────
    def _load(self) -> list[dict]:
        if not self.session_path.exists():
            return []
        with self.session_path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _append(self, msg: dict):
        self.history.append(msg)
        with self.session_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        with self.archive_path.open("a", encoding="utf-8") as f:  # 存档多记一个时间，my_log 按天翻
            f.write(json.dumps({"ts": time.time(), **msg}, ensure_ascii=False) + "\n")

    def turn_count(self) -> int:
        return sum(1 for m in self.history if m["role"] == "user" and m["content"].startswith(WAKE_MARK))

    def _evict_if_needed(self):
        """上下文超了上限，就把最旧的三分之一圈整块移出（不总结）。存档里一直留着全文，my_log 翻得到。

        一次移一大块而不是每圈挪一点，是为了让前缀尽量保持不变，多命中缓存。
        """
        if self.last_prompt_tokens <= self.cfg.context_budget:
            return
        turns = self.turn_count()
        if turns < 2:
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
        self.store.add_feed("info", f"上下文到了 {self.last_prompt_tokens} token：移出最旧的 {drop} 圈，存档里还在")
        self.last_prompt_tokens = 0


def parse_day(text: str, now: float):
    """今天 / 昨天 / 前天 / 11-02 / 2026-11-02 → 北京时间那一天的 date；看不懂返回 None。"""
    today = datetime.fromtimestamp(now, BJ).date()
    text = text.strip()
    offsets = {"今天": 0, "昨天": 1, "前天": 2}
    if text in offsets:
        return today - timedelta(days=offsets[text])
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})-(\d{1,2})", text):
        y, (mo, d) = today.year, map(int, m.groups())
    else:
        return None
    try:
        return today.replace(year=y, month=mo, day=d)
    except ValueError:
        return None


def my_log_lines(archive: Path, day, keyword: str = "") -> list[str]:
    """从存档里挑出某一天她自己的记录：叫醒、心里想的、调了什么工具。工具返回的内容不放，太长。"""
    if not archive.exists():
        return []
    out = []
    with archive.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            m = json.loads(line)
            ts = m.get("ts")
            if not ts or datetime.fromtimestamp(ts, BJ).date() != day:
                continue
            t = bj_time(ts, "%H:%M")
            if m["role"] == "user":
                out.append(f"[{t}] 被叫醒：{m['content'].splitlines()[0]}")
            elif m["role"] == "assistant":
                if m.get("content", "").strip():
                    out.append(f"[{t}] 想：{m['content'].strip()}")
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    out.append(f"[{t}] 调了 {fn.get('name')}({fn.get('arguments', '')})")
    if keyword:
        out = [x for x in out if keyword in x]
    return out
