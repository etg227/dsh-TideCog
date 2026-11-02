"""测试共用的小工具：造配置、假 QQ、假模型、发 HTTP 请求。全部只在本机随机端口上跑，碰不到真 QQ。"""
import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

from xiaojing.config import (Config, ConsoleConfig, GateConfig, InboxConfig, LLMConfig, OneBotConfig,
                             RhythmConfig)
from xiaojing.httpserver import json_response

SELF_ID = 9999
OWNER = "1000"
GROUP = "111"


def make_cfg(tmp) -> Config:
    return Config(
        data_dir=Path(tmp),
        onebot=OneBotConfig(api_url="http://127.0.0.1:1", listen_port=0, secret="s3cret", token="tok"),
        llm=LLMConfig(),
        gate=GateConfig(owner_qq=OWNER, allow_groups=[GROUP], allow_users=[OWNER, "2000"]),
        console=ConsoleConfig(port=0),
        # 测试里群消息只攒 0.3 秒；心跳一小时，测试期间不会自己响；作息窗口为空，永远醒着
        inbox=InboxConfig(group_batch_seconds=0.3, group_min_interval=0),
        rhythm=RhythmConfig(heartbeat_default=60, sleep_start="00:00", wake_time="00:00"),
        context_budget=64000,
    )


class FakeQQ:
    """假的 SnowLuma HTTP 服务器：记下每次调用。"""

    def __init__(self):
        self.calls = []

    async def handle(self, req):
        action = req.path.strip("/")
        self.calls.append((action, req.json(), req.headers.get("authorization", "")))
        if action == "get_login_info":
            return json_response({"status": "ok", "retcode": 0, "data": {"user_id": SELF_ID, "nickname": "小鲸"}})
        if action.startswith("send_"):
            return json_response({"status": "ok", "retcode": 0, "data": {"message_id": len(self.calls)}})
        return json_response({"status": "failed", "retcode": 1404, "data": None})

    def sends(self, action="send_group_msg"):
        return [params for a, params, _ in self.calls if a == action]


class FakeLLM:
    """按剧本一步步回话的假模型。剧本用完了就回一句不调工具的话，免得卡住。"""

    def __init__(self, script=()):
        self.script = list(script)
        self.seen = []

    async def chat(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        step = self.script.pop(0) if self.script else {"content": "（剧本用完了）"}
        return step, {"hit": 1000, "miss": 200, "out": 50, "model": "fake"}


def tool_call(cid, name, **args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def group_event(text="在吗", at_me=True, user_id=2000, group_id=int(GROUP)):
    message = ([{"type": "at", "data": {"qq": str(SELF_ID)}}] if at_me else []) + \
              [{"type": "text", "data": {"text": text}}]
    return {"post_type": "message", "message_type": "group", "group_id": group_id, "user_id": user_id,
            "self_id": SELF_ID, "message_id": 1, "time": 1793500000, "message": message,
            "sender": {"card": "阿北", "nickname": "beibei"}}


def private_event(text, user_id=int(OWNER)):
    return {"post_type": "message", "message_type": "private", "user_id": user_id, "self_id": SELF_ID,
            "message_id": 2, "time": 1793500000, "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"nickname": "主人"}}


def _http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


async def http(method, url, body=None, headers=None):
    return await asyncio.to_thread(_http, method, url, body, headers)


async def wait_for(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()
