"""整条链路：假 SnowLuma 上报 → 叫醒她 → 假模型调工具 → 真的经过闸门 → 假 SnowLuma 收到发送。"""
import asyncio
import hashlib
import hmac
import json
import tempfile
import unittest

from xiaojing.app import App, PortInUse
from xiaojing.httpserver import Server

from .helpers import (GROUP, SELF_ID, FakeLLM, FakeQQ, group_event, http, make_cfg, private_event,
                      tool_call, wait_for)

REPLY_SCRIPT = [
    {"content": "有人找我，看看", "tool_calls": [tool_call("c1", "open", conv=f"group:{GROUP}")]},
    {"content": "", "tool_calls": [tool_call("c2", "send", conv=f"group:{GROUP}", text="在呢在呢")]},
    {"content": "回完了"},
]


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qq = FakeQQ()
        self.qq_server = Server("127.0.0.1", 0, self.qq.handle)
        await self.qq_server.start()
        cfg = make_cfg(self.tmp.name)
        cfg.onebot.api_url = f"http://127.0.0.1:{self.qq_server.port}"
        self.llm = FakeLLM(REPLY_SCRIPT)
        self.app = App(cfg, llm=self.llm, system_prompt="测试用的设定")
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        await self.qq_server.close()
        self.tmp.cleanup()

    async def post(self, evt, secret="s3cret"):
        body = json.dumps(evt, ensure_ascii=False).encode()
        sig = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        status, _, _ = await http("POST", f"http://127.0.0.1:{self.app.receiver.port}/", body,
                                  {"Content-Type": "application/json", "X-Signature": sig})
        return status

    def console(self, path):
        return f"http://127.0.0.1:{self.app.console.server.port}{path}"

    async def test_mention_gets_a_reply(self):
        self.assertEqual(await self.post(group_event("在吗")), 204)
        self.assertTrue(await wait_for(lambda: self.qq.sends()))
        self.assertEqual(self.qq.sends()[0], {"group_id": int(GROUP),
                                              "message": [{"type": "text", "data": {"text": "在呢在呢"}}]})
        self.assertTrue(all(auth == "Bearer tok" for _, _, auth in self.qq.calls))
        await wait_for(lambda: not self.app.agent.running)

        first_call = self.llm.seen[0]
        self.assertEqual(first_call[0], {"role": "system", "content": "测试用的设定"})
        self.assertIn(f"group:{GROUP}：1 条未读（有人 @ 你）", first_call[1]["content"])
        rows = self.app.store.recent(f"group:{GROUP}")
        self.assertEqual([(r["text"], r["mine"]) for r in rows], [(f"@{SELF_ID}在吗", 0), ("在呢在呢", 1)])
        self.assertEqual(self.app.store.cost_today()["calls"], 3)
        kinds = [f["kind"] for f in self.app.store.feed()]
        for k in ("wake", "think", "tool", "send"):
            self.assertIn(k, kinds)
        self.assertEqual(self.app.onebot.self_id, str(SELF_ID))  # 探活拿到了自己的号

    async def test_plain_group_message_does_not_wake(self):
        self.assertEqual(await self.post(group_event("大家好", at_me=False)), 204)
        await asyncio.sleep(0.3)
        self.assertEqual(self.llm.seen, [])
        self.assertEqual(len(self.app.store.recent(f"group:{GROUP}")), 1)  # 但存下来了

    async def test_bad_signature_rejected(self):
        self.assertEqual(await self.post(group_event(), secret="wrong"), 401)
        self.assertEqual(self.app.store.recent(f"group:{GROUP}"), [])

    async def test_send_without_open_is_blocked(self):
        self.llm.script = [
            {"content": "", "tool_calls": [tool_call("c1", "send", conv=f"group:{GROUP}", text="直接说")]},
            {"content": "好吧"},
        ]
        await self.post(group_event())
        self.assertTrue(await wait_for(lambda: len(self.llm.seen) == 2))
        await wait_for(lambda: not self.app.agent.running)
        self.assertEqual(self.qq.sends(), [])
        tool_result = self.llm.seen[1][-1]
        self.assertEqual(tool_result["role"], "tool")
        self.assertIn("先 open", tool_result["content"])

    async def test_owner_can_pause_and_resume(self):
        await self.post(private_event("暂停"))
        self.assertTrue(self.app.gate.paused)
        self.assertIn("已暂停", self.qq.sends("send_private_msg")[0]["message"][0]["data"]["text"])

        await self.post(group_event())
        await asyncio.sleep(0.3)
        self.assertEqual(self.llm.seen, [])  # 暂停时不转
        self.assertIn(f"group:{GROUP}", self.app.agent.pending)

        await self.post(private_event("继续"))
        self.assertFalse(self.app.gate.paused)
        self.assertTrue(await wait_for(lambda: self.qq.sends()))

    async def test_someone_else_cannot_pause(self):
        await self.post(private_event("暂停", user_id=2000))
        self.assertFalse(self.app.gate.paused)

    async def test_console_login_and_pause(self):
        status, _, _ = await http("GET", self.console("/api/state"))
        self.assertEqual(status, 401)
        status, _, _ = await http("GET", self.console("/api/state"), headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = await http("POST", self.console("/api/login"), b'{"token":"nope"}',
                                  {"Content-Type": "application/json"})
        self.assertEqual(status, 401)

        token = self.app.console.token
        status, headers, _ = await http("POST", self.console("/api/login"),
                                        json.dumps({"token": token}).encode(), {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])

        status, _, body = await http("GET", self.console("/api/state"), headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertFalse(state["paused"])
        self.assertEqual(state["allow"]["groups"], [GROUP])

        status, _, _ = await http("POST", self.console("/api/pause"), b"{}", {"Cookie": cookie})
        self.assertEqual(status, 404)  # 不是 JSON 请求，不认
        status, _, body = await http("POST", self.console("/api/pause"), b"{}",
                                     {"Cookie": cookie, "Content-Type": "application/json"})
        self.assertTrue(json.loads(body)["paused"])

        status, headers, body = await http("GET", self.console("/"))
        self.assertEqual(status, 200)
        self.assertIn("小鲸管理台", body.decode("utf-8"))


class PortInUseTest(unittest.IsolatedAsyncioTestCase):
    async def test_busy_console_port_is_reported_and_cleaned_up(self):
        blocker = Server("127.0.0.1", 0, None)
        await blocker.start()
        tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(tmp.name)
        cfg.console.port = blocker.port
        app = App(cfg, llm=FakeLLM(), system_prompt="设定")
        with self.assertRaises(PortInUse) as ctx:
            await app.start()
        self.assertIn("[console] port", str(ctx.exception))
        await blocker.close()
        tmp.cleanup()  # 数据库已经关掉，临时文件夹删得掉


if __name__ == "__main__":
    unittest.main()
