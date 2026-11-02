"""桌面版：双击 launcher.exe 打开一个窗口，里面就是管理台；关窗口缩到托盘，她继续在后台转。

不弹黑窗口、不开浏览器、不用输口令。只能开一个：再双击会把已经开着的窗口叫出来。
运行：.venv\\Scripts\\python -m xiaojing.desktop（或者打包好的 launcher.exe）
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path

TITLE = "小鲸"
MUTEX_NAME = "Local\\xiaojing-launcher"
MB_ICONERROR, MB_ICONWARNING = 0x10, 0x30

log = logging.getLogger("xiaojing.desktop")


def resource(rel: str) -> Path:
    """打包进 exe 的文件（图标等）。从源码跑时就在项目里。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def message_box(text: str, icon=MB_ICONERROR):
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, icon)


def claim_single_instance() -> bool:
    """拿到"只开一个"的锁就返回 True；已经有一个在跑就返回 False。"""
    global _mutex  # 留住句柄，进程退出前锁一直在
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def show_existing_window():
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE，藏在托盘里的也会出来
        user32.SetForegroundWindow(hwnd)


def setup_logging(data_dir: Path):
    handlers = [logging.FileHandler(data_dir / "xiaojing.log", encoding="utf-8")]
    if sys.stderr:  # 打包成不带黑窗口的 exe 后，stderr 是 None
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s：%(message)s",
                        handlers=handlers)


class Backend:
    """在后台线程里跑小鲸本体（她自己的事件循环），窗口在主线程。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.app = None
        self.error: Exception | None = None
        self.ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self.thread = threading.Thread(target=self._run, name="xiaojing-backend", daemon=True)

    def start(self, timeout=30) -> bool:
        self.thread.start()
        if not self.ready.wait(timeout):
            self.error = TimeoutError("启动超时")
        return self.error is None

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            log.exception("后台出错")
            self.error = self.error or e
            self.ready.set()

    async def _main(self):
        from .app import App
        self.app = App(self.cfg)
        try:
            await self.app.start()
        except Exception as e:
            self.error = e
            self.ready.set()
            return
        self._stop = asyncio.Event()
        self.ready.set()
        await self._stop.wait()
        await self.app.stop()

    def call(self, fn, *args):
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout=10):
        if self._stop and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self._stop.set)
            self.thread.join(timeout)


class DesktopApi:
    """给管理台页面用的接口：页面要登录时，直接从这里拿口令，不用人输。属性都带下划线，免得被暴露给网页。"""

    def __init__(self, token: str):
        self._token = token
        self._used = threading.Event()

    def token(self) -> str:
        if not self._used.is_set():
            log.info("管理台已自动登录")
        self._used.set()
        return self._token


def startup_warnings(cfg) -> list[str]:
    warnings = []
    if not cfg.llm.api_key:
        warnings.append("没有设置环境变量 DEEPSEEK_API_KEY，她会被叫醒，但调不了模型。")
    if not cfg.gate.allow_groups and not cfg.gate.allow_users:
        warnings.append("config.toml 里允许名单是空的，她谁也不会回。")
    return warnings


def main():
    if sys.platform != "win32":
        print("桌面版只支持 Windows；别的系统用 python -m xiaojing")
        return
    from .config import ROOT, load
    try:
        cfg = load()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        message_box(f"config.toml 读不了：{e}\n\n位置：{ROOT / 'config.toml'}")
        return
    setup_logging(cfg.data_dir)
    if not claim_single_instance():
        show_existing_window()
        return

    import webview  # 放在这里导入，缺包时上面的弹窗还能正常工作

    from .tray import Tray

    backend = Backend(cfg)
    if not backend.start():
        message_box(f"小鲸没能启动：\n\n{backend.error}")
        return
    api = DesktopApi(backend.app.console.token)
    url = f"http://127.0.0.1:{backend.app.console.server.port}/"
    window = webview.create_window(TITLE, url, width=1320, height=860, min_size=(960, 640),
                                   background_color="#080d1a", js_api=api, text_select=True)
    quitting = threading.Event()
    hinted = threading.Event()

    def hide_to_tray():
        window.hide()
        if not hinted.is_set():
            hinted.set()
            tray.notify("小鲸还在后台", "点右下角的托盘图标就能再打开；右键可以暂停或退出。")

    def on_closing():
        if quitting.is_set():
            return True
        threading.Thread(target=hide_to_tray, daemon=True).start()
        return False  # 不真关，缩到托盘

    def open_window():
        window.show()
        window.restore()

    def quit_all():
        quitting.set()
        log.info("从托盘退出")
        backend.stop()
        window.destroy()

    def set_paused(paused: bool):
        backend.call(backend.app.set_paused, paused, "托盘")

    window.events.closing += on_closing
    tray = Tray(resource("assets/launcher.ico"), "小鲸", on_open=open_window,
                on_pause=lambda: set_paused(True), on_resume=lambda: set_paused(False),
                on_quit=quit_all, is_paused=lambda: backend.app.gate.paused)
    tray.start()
    for w in startup_warnings(cfg):
        log.warning(w)
    if warnings := startup_warnings(cfg):
        tray.notify("小鲸启动了，但有问题", " ".join(warnings))

    if os.environ.get("XIAOJING_DESKTOP_SELFTEST"):
        threading.Thread(target=_selftest, args=(window, api, on_closing, open_window, quit_all),
                         daemon=True).start()
    log.info("桌面窗口打开：%s", url)
    webview.start(private_mode=True)
    tray.stop()
    backend.stop()
    log.info("桌面版已退出")


def _selftest(window, api, on_closing, open_window, quit_all):
    """自动走一遍：等自动登录 → 关窗口（应缩到托盘）→ 从托盘打开 → 退出。结果写进日志。"""
    ok = api._used.wait(20)
    log.info("自测：自动登录%s", "成功" if ok else "没发生")
    time.sleep(1)
    log.info("自测：点关闭，返回 %s（False 表示缩到托盘）", on_closing())
    time.sleep(1.5)
    open_window()
    log.info("自测：从托盘打开")
    time.sleep(1.5)
    log.info("自测：退出")
    quit_all()


if __name__ == "__main__":
    main()
