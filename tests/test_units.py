import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from xiaojing.agent import WAKE_MARK, Agent
from xiaojing.config import load
from xiaojing.gate import Gate
from xiaojing.onebot import OneBot, parse_message_event
from xiaojing.prices import BJ, cost, is_peak
from xiaojing.store import Store

from .helpers import SELF_ID, group_event, make_cfg, private_event


def bj(*args):
    return datetime(*args, tzinfo=BJ).timestamp()


class PricesTest(unittest.TestCase):
    def test_peak_hours(self):
        self.assertTrue(is_peak(bj(2026, 11, 2, 10, 0)))    # 周一上午
        self.assertFalse(is_peak(bj(2026, 11, 2, 12, 30)))  # 午休
        self.assertTrue(is_peak(bj(2026, 11, 2, 17, 59)))
        self.assertFalse(is_peak(bj(2026, 11, 7, 10, 0)))   # 周六

    def test_idle_loop_cost(self):
        cfg = make_cfg(".").llm
        usage = {"hit": 60000, "miss": 1000, "out": 300}
        yuan, peak = cost(usage, cfg, bj(2026, 11, 2, 21, 0))
        self.assertFalse(peak)
        self.assertAlmostEqual(yuan, 0.0034, places=6)
        yuan_peak, _ = cost(usage, cfg, bj(2026, 11, 2, 10, 0))
        self.assertAlmostEqual(yuan_peak, 0.0068, places=6)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.gate = Gate(make_cfg(".").gate)

    def test_must_open_first(self):
        self.assertIn("先 open", self.gate.check_send("group:111", "hi"))
        self.gate.mark_opened("group:111")
        self.assertIsNone(self.gate.check_send("group:111", "hi"))
        self.gate.new_turn()
        self.assertIn("先 open", self.gate.check_send("group:111", "hi"))

    def test_allow_list(self):
        self.assertIn("允许名单", self.gate.check_open("group:222"))
        self.gate.mark_opened("group:222")
        self.assertIn("允许名单", self.gate.check_send("group:222", "hi"))

    def test_rate_limit(self):
        self.gate.mark_opened("group:111")
        now = 1000.0
        for i in range(4):
            self.assertIsNone(self.gate.check_send("group:111", "hi", now + i))
            self.gate.record_send("group:111", now + i)
        self.assertIn("太多", self.gate.check_send("group:111", "hi", now + 5))
        self.assertIsNone(self.gate.check_send("group:111", "hi", now + 61))

    def test_pause_empty_and_long(self):
        self.gate.mark_opened("group:111")
        self.assertIn("空", self.gate.check_send("group:111", "  "))
        self.assertIn("太长", self.gate.check_send("group:111", "啊" * 501))
        self.gate.paused = True
        self.assertIn("暂停", self.gate.check_send("group:111", "hi"))


class OneBotParseTest(unittest.TestCase):
    def test_array_with_at(self):
        m = parse_message_event(group_event("在吗"))
        self.assertEqual(m["conv"], "group:111")
        self.assertTrue(m["mentions_me"])
        self.assertEqual(m["text"], f"@{SELF_ID}在吗")
        self.assertEqual(m["sender_name"], "阿北")

    def test_string_format(self):
        evt = group_event()
        evt["message"] = f"[CQ:at,qq={SELF_ID}] 你好&#91;doge&#93;[CQ:image,file=a.jpg]"
        m = parse_message_event(evt)
        self.assertTrue(m["mentions_me"])
        self.assertEqual(m["text"], f"@{SELF_ID} 你好[doge][图片]")

    def test_at_someone_else(self):
        evt = group_event()
        evt["message"] = [{"type": "at", "data": {"qq": "123"}}, {"type": "text", "data": {"text": "hi"}}]
        self.assertFalse(parse_message_event(evt)["mentions_me"])

    def test_private_and_non_message(self):
        self.assertEqual(parse_message_event(private_event("暂停"))["conv"], "private:1000")
        self.assertIsNone(parse_message_event({"post_type": "notice"}))

    def test_signature(self):
        ob = OneBot(make_cfg(".").onebot)
        body = b'{"a":1}'
        good = "sha1=" + hmac.new(b"s3cret", body, hashlib.sha1).hexdigest()
        self.assertTrue(ob.verify(body, good))
        self.assertFalse(ob.verify(body, "sha1=00"))
        self.assertFalse(ob.verify(body, ""))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "e.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_unread_and_recent(self):
        s = self.store
        s.add_event("group:111", "第一条消息", sender_name="阿北")
        s.add_event("group:111", "我说的", mine=True)
        s.add_event("group:111", "第三条消息", sender_name="阿北")
        self.assertEqual(s.unread("group:111"), 2)  # 自己发的不算未读
        s.mark_read("group:111")
        self.assertEqual(s.unread("group:111"), 0)
        self.assertEqual([r["text"] for r in s.recent("group:111", 2)], ["我说的", "第三条消息"])

    def test_search(self):
        s = self.store
        s.add_event("group:111", "周末一起去爬山吗")
        s.add_event("private:1000", "我想吃火锅")
        self.assertEqual(len(s.search("去爬山")), 1)        # 三个字以上走全文检索
        self.assertEqual(len(s.search("火锅")), 1)          # 两个字退回 LIKE
        self.assertEqual(len(s.search("火锅", conv="group:111")), 0)

    def test_cost_today(self):
        now = time.time()
        self.store.add_usage(now, "m", 900, 100, 50, 0.01, False)
        self.store.add_usage(now - 3 * 86400, "m", 900, 100, 50, 0.01, False)
        today = self.store.cost_today(now)
        self.assertEqual(today["calls"], 1)
        self.assertEqual(today["hit_rate"], 0.9)


class EvictTest(unittest.TestCase):
    def test_moves_oldest_third_and_keeps_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            store = Store(Path(tmp) / "e.db")
            agent = Agent(cfg, store, None, Gate(cfg.gate), None, "设定")
            for i in range(31):
                agent._append({"role": "user", "content": f"{WAKE_MARK} 第{i}圈】"})
                agent._append({"role": "assistant", "content": "嗯"})
            agent._evict_if_needed()
            self.assertEqual(agent.turn_count(), 21)
            self.assertTrue(agent.history[0]["content"].startswith(f"{WAKE_MARK} 第10圈"))
            on_disk = agent.session_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(on_disk), len(agent.history))
            archive = agent.archive_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(archive), 62)
            reloaded = Agent(cfg, store, None, Gate(cfg.gate), None, "设定")
            self.assertEqual(reloaded.history, agent.history)
            store.close()


class ConfigTest(unittest.TestCase):
    def test_numbers_become_strings_and_secrets_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text('[gate]\nowner_qq = 1000\nallow_groups = [111, "222"]\n', encoding="utf-8")
            cfg = load(p, env={"DEEPSEEK_API_KEY": "k", "ONEBOT_TOKEN": "t"})
            self.assertEqual(cfg.gate.owner_qq, "1000")
            self.assertEqual(cfg.gate.allow_groups, ["111", "222"])
            self.assertEqual(cfg.llm.api_key, "k")
            self.assertEqual(cfg.onebot.token, "t")
            self.assertEqual(cfg.onebot.secret, "")


if __name__ == "__main__":
    unittest.main()
