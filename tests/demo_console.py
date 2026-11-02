"""看看管理台长什么样：假 QQ + 假模型，不连真 QQ、不花钱。

运行：python -m tests.demo_console
然后打开 http://127.0.0.1:5319/ ，口令是 demo。Ctrl+C 退出，演示数据放在临时文件夹，退出就删。
"""
import asyncio
import json
import tempfile
from pathlib import Path

from xiaojing.app import App
from xiaojing.httpserver import Server

from .helpers import GROUP, FakeLLM, FakeQQ, group_event, http, make_cfg, private_event, tool_call

SCRIPT = [
    {"content": "有人 @ 我，看看是谁", "tool_calls": [tool_call("a1", "open", conv=f"group:{GROUP}")]},
    {"content": "阿北问周末去不去爬山，想去！", "tool_calls": [
        tool_call("a2", "send", conv=f"group:{GROUP}", text="去去去！几点集合"),
        tool_call("a3", "send", conv=f"group:{GROUP}", text="我带零食"),
    ]},
    {"content": "回完了，等他们定时间"},
    {"content": "", "tool_calls": [tool_call("b1", "send", conv=f"group:{GROUP}", text="诶我刚想起来")]},
    {"content": "哦对，这一圈还没打开群，先不说了"},
]


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "console-token.txt").write_text("demo", encoding="utf-8")
        qq = FakeQQ()
        qq_server = Server("127.0.0.1", 0, qq.handle)
        await qq_server.start()
        cfg = make_cfg(tmp)
        cfg.onebot.api_url = f"http://127.0.0.1:{qq_server.port}"
        cfg.onebot.secret = ""
        cfg.console.port = 5319
        app = App(cfg, llm=FakeLLM(SCRIPT), system_prompt="演示")
        await app.start()

        async def post(evt):
            await http("POST", f"http://127.0.0.1:{app.receiver.port}/",
                       json.dumps(evt, ensure_ascii=False).encode(), {"Content-Type": "application/json"})

        await post(group_event("周末去爬山不", at_me=False))
        await post(group_event("小鲸你去不去"))
        await asyncio.sleep(1)
        await post(group_event("小鲸？", at_me=True))
        await asyncio.sleep(1)
        await post(private_event("暂停"))
        await post(group_event("人呢", at_me=True))
        print("管理台：http://127.0.0.1:5319/ 口令 demo（现在是暂停状态，可以点「继续」试试）")
        try:
            await asyncio.Event().wait()
        finally:
            await app.stop()
            await qq_server.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
