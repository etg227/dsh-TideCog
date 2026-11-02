import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from xiaojing.agent import Agent, MY_LOG_MAX_CHARS
from xiaojing.app import App
from xiaojing.gate import Gate
from xiaojing.onebot import OneBot
from xiaojing.setup import apply, missing
from xiaojing.store import Store

from .helpers import FakeLLM, make_cfg


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = make_cfg(tmp.name)
        self.store = Store(Path(tmp.name) / "test.db")
        self.addCleanup(self.store.close)

    def agent(self):
        return Agent(self.cfg, self.store, None, Gate(self.cfg.gate), FakeLLM(), "test")

    def test_pages_drain_backlog_without_marking_unseen_rows(self):
        ids = [self.store.add_event("group:111", str(i)) for i in range(55)]
        self.store.mark_seen([ids[22]])  # a system command already handled separately
        received = set()
        for remaining in (34, 14, 0):
            received.update(r["id"] for r in self.store.open_page("group:111", 20))
            self.assertEqual(self.store.unread("group:111"), remaining)
            inbox = self.store.inbox(["group:111"])
            self.assertEqual(inbox[0]["unread"] if inbox else 0, remaining)
        self.assertTrue(set(ids) - {ids[22]} <= received)

    def test_old_read_cursor_is_respected(self):
        self.store.add_event("group:111", "old")
        self.store.mark_read("group:111")
        self.store.add_event("group:111", "new")
        self.assertEqual(self.store.unread("group:111"), 1)
        self.assertEqual(self.store.open_page("group:111", 1)[0]["text"], "new")

    def test_scheduler_survives_recreation(self):
        a = self.agent()
        a.next_heartbeat = time.time() + 1234
        a.idle_streak = 4
        a.rhythm.sleep_until = a.next_heartbeat
        a.note_group("group:111")
        a.wake("private:1000", "message")
        b = self.agent()
        for name in ("group_due", "pending", "next_heartbeat", "idle_streak"):
            self.assertEqual(getattr(a, name), getattr(b, name))
        self.assertEqual(a.rhythm.sleep_until, b.rhythm.sleep_until)

    def test_pause_survives_backend_recreation(self):
        first = App(self.cfg, llm=FakeLLM(), system_prompt="test")
        first.set_paused(True, "test")
        first.store.close()
        second = App(self.cfg, llm=FakeLLM(), system_prompt="test")
        self.addCleanup(second.store.close)
        self.assertTrue(second.gate.paused)

    def test_overdue_pause_waits_for_resume(self):
        async def exercise():
            a = self.agent()
            a.next_heartbeat = time.time() - 1
            a.gate.paused = True
            a._seconds_until_due = Mock(wraps=a._seconds_until_due)
            task = asyncio.create_task(a.run())
            try:
                await asyncio.sleep(0.02)
                a._seconds_until_due.assert_not_called()
                a.gate.paused = False
                a.nudge()
                await asyncio.sleep(0.02)
                self.assertTrue(a.llm.seen)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(exercise())

    def test_archive_paging_reconstructs_a_long_entry(self):
        a = self.agent()
        a._append({"role": "assistant", "content": "0123456789" * 1000})
        args = {"day": "今天", "keyword": "0123456789"}
        pages = []
        while True:
            result = a._tool_my_log(args)
            if not result.startswith("【更早一页"):
                pages.append(result)
                break
            header, page = result.split("\n", 1)
            self.assertEqual(len(page), MY_LOG_MAX_CHARS)
            args["before"] = int(header.split("=")[1].rstrip("】"))
            pages.append(page)
        self.assertTrue("".join(reversed(pages)).endswith("0123456789" * 1000))

    def test_unsigned_receiver_and_malformed_owner_fail_closed(self):
        self.cfg.onebot.secret = ""
        self.assertFalse(OneBot(self.cfg.onebot).verify(b"{}", ""))
        self.assertIn("OneBot 上报签名密钥", missing(self.cfg))
        values = {"owner_qq": "1234567890123", "api_url": "http://127.0.0.1:3020"}
        _, _, errors = apply(self.cfg, values)
        self.assertIn("owner_qq", errors)
        self.assertIn("secret", errors)
