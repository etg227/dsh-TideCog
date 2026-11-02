"""桌面版：双击 launcher.exe 打开一个窗口，里面就是管理台；关窗口缩到托盘，她继续在后台转。

不弹黑窗口、不开浏览器、不用输口令。第一次打开先进设置页，填好就上线。
只能开一个：再双击会把已经开着的窗口叫出来。
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
MB_ICONERROR = 0x10

log = logging.getLogger("xiaojing.desktop")


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


class Controller:
    """管着后台、窗口和托盘；设置保存后在这里重启后台。"""

    def __init__(self, root: Path):
        self.root = root
        self.backend: Backend | None = None
        self.window = None
        self.tray = None
        self.quitting = threading.Event()
        self._lock = threading.Lock()
        self.token_used = threading.Event()
        self._hinted = threading.Event()  # 第一次缩到托盘时提示一下

    def start_backend(self) -> Backend:
        from .config import load
        backend = Backend(load())
        if not backend.start():
            raise RuntimeError(f"小鲸没能启动：{backend.error}")
        self.backend = backend
        return backend

    def url(self, path="") -> str:
        return f"http://127.0.0.1:{self.backend.app.console.server.port}/{path}"

    def restart_backend(self):
        with self._lock:
            log.info("按新设置重启小鲸")
            self.backend.stop()
            self.start_backend()

    def warn_if_incomplete(self):
        from .setup import missing
        cfg = self.backend.cfg
        warnings = [f"还没填：{'、'.join(m)}" for m in [missing(cfg)] if m]
        if not cfg.gate.allow_groups and not [u for u in cfg.gate.allow_users if u != cfg.gate.owner_qq]:
            warnings.append("允许名单里只有你自己")
        for w in warnings:
            log.warning(w)
        if warnings and self.tray:
            self.tray.notify("小鲸启动了，但有问题", "；".join(warnings) + "。从托盘菜单点「设置」去改。")

    # ── 窗口和托盘的动作 ──────────────────────────────
    def hide_to_tray(self):
        self.window.hide()
        if not self._hinted.is_set():
            self._hinted.set()
            self.tray.notify("小鲸还在后台", "点右下角的托盘图标就能再打开；右键可以暂停、设置或退出。")

    def on_closing(self):
        if self.quitting.is_set():
            return True
        threading.Thread(target=self.hide_to_tray, daemon=True).start()
        return False  # 不真关，缩到托盘

    def open_window(self, path=None):
        if path is not None:
            self.window.load_url(self.url(path))
        self.window.show()
        self.window.restore()

    def set_paused(self, paused: bool):
        self.backend.call(self.backend.app.set_paused, paused, "托盘")

    def quit_all(self):
        self.quitting.set()
        log.info("从托盘退出")
        self.backend.stop()
        self.window.destroy()


class DesktopApi:
    """给页面用的接口（管理台自动登录、设置页读写）。属性都带下划线，免得被暴露给网页。"""

    def __init__(self, ctl: Controller):
        self._ctl = ctl

    def token(self) -> str:
        if not self._ctl.token_used.is_set():
            log.info("管理台已自动登录")
        self._ctl.token_used.set()
        return self._ctl.backend.app.console.token

    def setup_state(self) -> dict:
        from .setup import state
        return state(self._ctl.backend.cfg, self._ctl.root)

    def save_setup(self, values: dict) -> dict:
        from .setup import apply, save
        ctl = self._ctl
        cfg, saved, errors = apply(ctl.backend.cfg, values or {})
        if errors:
            return {"ok": False, "errors": errors, "message": "有几项要改一下"}
        try:
            save(cfg, saved, ctl.root)
            ctl.restart_backend()
        except Exception as e:
            log.exception("保存设置失败")
            return {"ok": False, "errors": {}, "message": f"保存失败：{e}"}
        log.info("设置已保存")
        ctl.warn_if_incomplete()
        return {"ok": True, "url": ctl.url()}


def main():
    if sys.platform != "win32":
        print("桌面版只支持 Windows；别的系统用 python -m xiaojing")
        return
    from .config import ROOT
    from .setup import bundled, ensure_defaults, missing
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        created = ensure_defaults(ROOT)
        (ROOT / "data").mkdir(exist_ok=True)
    except Exception as e:
        message_box(f"准备文件夹失败：{e}\n\n位置：{ROOT}")
        return
    setup_logging(ROOT / "data")
    if created:
        log.info("第一次运行，放好了默认文件：%s（在 %s）", "、".join(created), ROOT)
    if not claim_single_instance():
        show_existing_window()
        return

    import webview  # 放在这里导入，缺包时上面的弹窗还能正常工作

    from .tray import Tray

    ctl = Controller(ROOT)
    try:
        ctl.start_backend()
    except Exception as e:
        message_box(f"{e}\n\n配置文件：{ROOT / 'config.toml'}")
        return
    first = bool(missing(ctl.backend.cfg))
    ctl.window = webview.create_window(TITLE, ctl.url("setup" if first else ""), width=1320, height=860,
                                       min_size=(960, 640), background_color="#080d1a",
                                       js_api=DesktopApi(ctl), text_select=True)
    ctl.window.events.closing += ctl.on_closing
    ctl.tray = Tray(bundled("assets/launcher.ico"), "小鲸", on_open=ctl.open_window,
                    on_settings=lambda: ctl.open_window("setup"),
                    on_pause=lambda: ctl.set_paused(True), on_resume=lambda: ctl.set_paused(False),
                    on_quit=ctl.quit_all, is_paused=lambda: ctl.backend.app.gate.paused)
    ctl.tray.start()
    if not first:
        ctl.warn_if_incomplete()

    if mode := os.environ.get("XIAOJING_DESKTOP_SELFTEST"):
        threading.Thread(target=_selftest, args=(ctl, mode), daemon=True).start()
    log.info("桌面窗口打开：%s", ctl.url("setup" if first else ""))
    webview.start(private_mode=True)
    ctl.tray.stop()
    ctl.backend.stop()
    log.info("桌面版已退出")


def _selftest(ctl: Controller, mode: str):
    """自动走一遍，结果写进日志。mode=setup 时先走第一次的设置流程。"""
    api = DesktopApi(ctl)
    if mode == "setup":
        time.sleep(3)
        log.info("自测：设置页状态 %s", {k: v for k, v in api.setup_state().items() if k != "root"})
        bad = api.save_setup({"owner_qq": "12", "api_url": "nope"})
        log.info("自测：故意填错，返回 %s", bad)
        good = api.save_setup({"owner_qq": "10001", "owner_name": "测试员", "allow_groups": "123456, 234567",
                               "api_url": "http://127.0.0.1:3020", "token": "t-selftest", "secret": "s-selftest",
                               "api_key": "sk-selftest", "model": ""})
        log.info("自测：正确填写，返回 %s", good)
        ctl.window.load_url(good.get("url", ctl.url()))
    ok = ctl.token_used.wait(20)
    log.info("自测：自动登录%s", "成功" if ok else "没发生")
    time.sleep(1)
    log.info("自测：点关闭，返回 %s（False 表示缩到托盘）", ctl.on_closing())
    time.sleep(1.5)
    ctl.open_window()
    log.info("自测：从托盘打开")
    time.sleep(1.5)
    log.info("自测：退出")
    ctl.quit_all()


if __name__ == "__main__":
    main()
