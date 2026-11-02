"""右下角托盘图标：单击打开窗口，右键菜单打开 / 暂停 / 继续 / 退出。只用 ctypes，不多装包。"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_NULL, WM_DESTROY, WM_CLOSE = 0x0000, 0x0002, 0x0010
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY = 0x0400 + 20
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
IDI_APPLICATION = 32512
SM_CXSMICON = 49


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


def _proto(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


_proto(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_proto(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_proto(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE,
       wintypes.LPVOID)
_proto(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_proto(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
       ctypes.c_int, wintypes.UINT)
_proto(user32.LoadIconW, wintypes.HICON, wintypes.HINSTANCE, ctypes.c_void_p)
_proto(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_proto(user32.CreatePopupMenu, wintypes.HMENU)
_proto(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
_proto(user32.TrackPopupMenu, ctypes.c_int, wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       wintypes.HWND, ctypes.c_void_p)
_proto(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
_proto(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_proto(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_proto(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_proto(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_proto(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_proto(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_proto(user32.PostQuitMessage, None, ctypes.c_int)
_proto(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_proto(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)

MENU = [(1, "打开管理台"), (2, "暂停"), (3, "继续"), (None, None), (4, "退出")]


class Tray:
    def __init__(self, icon_path: Path | None, tip: str, *, on_open, on_pause, on_resume, on_quit, is_paused):
        self.icon_path, self.tip = icon_path, tip
        self.actions = {1: on_open, 2: on_pause, 3: on_resume, 4: on_quit}
        self.is_paused = is_paused
        self.hwnd = None
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)  # 留住引用，不然回调会被回收

    def start(self):
        threading.Thread(target=self._run, name="xiaojing-tray", daemon=True).start()
        self._ready.wait(5)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def notify(self, title: str, text: str):
        """右下角弹一条气泡提示。"""
        if not self.hwnd:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = title[:63], text[:255], NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _nid(self, flags) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize, nid.hWnd, nid.uID, nid.uFlags = ctypes.sizeof(NOTIFYICONDATAW), self.hwnd, 1, flags
        return nid

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize, wc.lpfnWndProc, wc.hInstance = ctypes.sizeof(WNDCLASSEXW), self._proc, hinst
        wc.lpszClassName = "XiaojingTray"
        user32.RegisterClassExW(ctypes.byref(wc))
        # 一个永远不显示的普通窗口，用来接托盘的消息（纯消息窗口当不了前台，菜单点别处不会自动收起）
        self.hwnd = user32.CreateWindowExW(0, "XiaojingTray", "XiaojingTray", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
        icon = None
        if self.icon_path and self.icon_path.exists():
            icon = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = icon or user32.LoadIconW(None, IDI_APPLICATION)
        nid.szTip = self.tip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self._fire(1)
                elif event == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("托盘处理消息出错")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        paused = self.is_paused()
        menu = user32.CreatePopupMenu()
        for cid, label in MENU:
            if cid is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            grayed = (cid == 2 and paused) or (cid == 3 and not paused)
            user32.AppendMenuW(menu, MF_STRING | (MF_GRAYED if grayed else 0), cid, label)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)  # 微软文档要求的写法，让菜单正常收起
        user32.DestroyMenu(menu)
        if cmd:
            self._fire(cmd)

    def _fire(self, cid):
        """菜单动作放到别的线程跑，不卡托盘的消息循环。"""
        action = self.actions.get(cid)
        if action:
            threading.Thread(target=self._safe, args=(action,), daemon=True).start()

    @staticmethod
    def _safe(action):
        try:
            action()
        except Exception:
            log.exception("托盘菜单动作出错")
