"""事件库：所有消息永久存一份（SQLite，带全文检索），外加花费记录和给管理台看的动态。

所有读写都在主事件循环的线程里做，所以一条连接就够。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .prices import bj_day_start

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  conv TEXT NOT NULL,
  msg_id TEXT,
  sender_id TEXT,
  sender_name TEXT,
  text TEXT NOT NULL,
  ts REAL NOT NULL,
  mine INTEGER NOT NULL DEFAULT 0,
  mentions_me INTEGER NOT NULL DEFAULT 0,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS events_conv ON events(conv, id);
CREATE TABLE IF NOT EXISTS reads (conv TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, model TEXT,
  hit INTEGER NOT NULL, miss INTEGER NOT NULL, out INTEGER NOT NULL,
  cost REAL NOT NULL, peak INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feed (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, conv TEXT, text TEXT
);
-- 中文没有空格分词，用 trigram：三个字以上的片段都能搜到
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  text, content='events', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

FEED_TEXT_MAX = 600


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ── 消息 ──────────────────────────────────────────
    def add_event(self, conv, text, *, ts=None, sender_id="", sender_name="", msg_id="",
                  mine=False, mentions_me=False, raw=None) -> int:
        cur = self.db.execute(
            "INSERT INTO events (conv, msg_id, sender_id, sender_name, text, ts, mine, mentions_me, raw)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conv, str(msg_id), str(sender_id), sender_name, text, ts or time.time(),
             int(mine), int(mentions_me), json.dumps(raw, ensure_ascii=False) if raw is not None else None),
        )
        self.db.commit()
        return cur.lastrowid

    def recent(self, conv, n=20) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE conv = ? ORDER BY id DESC LIMIT ?", (conv, n)
        ).fetchall()
        return rows[::-1]

    def unread(self, conv) -> int:
        last = self._last_read(conv)
        return self.db.execute(
            "SELECT COUNT(*) FROM events WHERE conv = ? AND id > ? AND mine = 0", (conv, last)
        ).fetchone()[0]

    def mark_read(self, conv):
        top = self.db.execute("SELECT MAX(id) FROM events WHERE conv = ?", (conv,)).fetchone()[0]
        if top is not None:
            self.db.execute(
                "INSERT INTO reads (conv, last_id) VALUES (?, ?)"
                " ON CONFLICT(conv) DO UPDATE SET last_id = excluded.last_id", (conv, top)
            )
            self.db.commit()

    def _last_read(self, conv) -> int:
        row = self.db.execute("SELECT last_id FROM reads WHERE conv = ?", (conv,)).fetchone()
        return row[0] if row else 0

    def before(self, conv, before_id, n=20) -> list[sqlite3.Row]:
        """往上翻：比 before_id 更早的 n 条。"""
        rows = self.db.execute(
            "SELECT * FROM events WHERE conv = ? AND id < ? ORDER BY id DESC LIMIT ?", (conv, before_id, n)
        ).fetchall()
        return rows[::-1]

    def inbox(self, convs) -> list[dict]:
        """这些会话里有未读的：几条、最新一条的时间和发言人、有没有人 @ 她。"""
        out = []
        for conv in convs:
            row = self.db.execute(
                "SELECT COUNT(*), MAX(ts), MAX(mentions_me) FROM events WHERE conv = ? AND id > ? AND mine = 0",
                (conv, self._last_read(conv)),
            ).fetchone()
            if row[0]:
                last = self.db.execute(
                    "SELECT sender_name FROM events WHERE conv = ? AND mine = 0 ORDER BY id DESC LIMIT 1", (conv,)
                ).fetchone()
                out.append({"conv": conv, "unread": row[0], "last_ts": row[1], "mentioned": bool(row[2]),
                            "last_sender": last[0] if last else ""})
        return sorted(out, key=lambda x: -x["last_ts"])

    def search(self, query, conv=None, limit=20) -> list[sqlite3.Row]:
        """全文检索。trigram 至少要三个字，更短的退回到 LIKE。"""
        query = query.strip()
        if not query:
            return []
        if len(query) >= 3:
            phrase = '"' + query.replace('"', '""') + '"'
            sql = ("SELECT e.* FROM events_fts f JOIN events e ON e.id = f.rowid"
                   " WHERE events_fts MATCH ?")
            args = [phrase]
        else:
            sql = "SELECT e.* FROM events e WHERE e.text LIKE ?"
            args = ["%" + query.replace("%", r"\%").replace("_", r"\_") + "%"]
            sql += r" ESCAPE '\'"
        if conv:
            sql += " AND e.conv = ?"
            args.append(conv)
        sql += " ORDER BY e.id DESC LIMIT ?"
        args.append(limit)
        return self.db.execute(sql, args).fetchall()

    # ── 花费 ──────────────────────────────────────────
    def add_usage(self, ts, model, hit, miss, out, cost, peak):
        self.db.execute(
            "INSERT INTO usage (ts, model, hit, miss, out, cost, peak) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, model, hit, miss, out, cost, int(peak)),
        )
        self.db.commit()

    def cost_today(self, now=None) -> dict:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost), 0), COALESCE(SUM(hit), 0), COALESCE(SUM(miss), 0),"
            " COALESCE(SUM(out), 0) FROM usage WHERE ts >= ?", (bj_day_start(now),)
        ).fetchone()
        calls, yuan, hit, miss, out = row
        prompt = hit + miss
        return {"calls": calls, "yuan": round(yuan, 4), "hit": hit, "miss": miss, "out": out,
                "hit_rate": round(hit / prompt, 3) if prompt else None}

    # ── 管理台动态 ────────────────────────────────────
    def add_feed(self, kind, text, conv=None, ts=None):
        if len(text) > FEED_TEXT_MAX:
            text = text[:FEED_TEXT_MAX] + "…"
        self.db.execute("INSERT INTO feed (ts, kind, conv, text) VALUES (?, ?, ?, ?)",
                        (ts or time.time(), kind, conv, text))
        self.db.commit()

    def feed(self, limit=80) -> list[dict]:
        rows = self.db.execute("SELECT * FROM feed ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
