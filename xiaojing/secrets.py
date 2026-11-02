"""密钥存盘：用 Windows 自带的 DPAPI 加密，只有当前这个 Windows 账号能解开，文件拷走了也没用。

环境变量优先；没设环境变量时才读这里。设置页填的密钥存在 data/secrets.bin。
"""
from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path

NAMES = ("DEEPSEEK_API_KEY", "ONEBOT_TOKEN", "ONEBOT_SECRET")
FILE = "secrets.bin"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _call(fn_name: str, data: bytes) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = getattr(crypt32, fn_name)
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError(f"{fn_name} 失败（错误码 {ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def available() -> bool:
    return sys.platform == "win32"


def load(data_dir: Path) -> dict:
    path = data_dir / FILE
    if not available() or not path.exists():
        return {}
    try:
        return json.loads(_call("CryptUnprotectData", path.read_bytes()))
    except (OSError, ValueError):
        return {}  # 换了 Windows 账号或者文件坏了：当没存过，设置页会提示重新填


def save(data_dir: Path, updates: dict):
    """只更新有值的项；空字符串表示"不改"。"""
    if not available():
        raise OSError("只有 Windows 能加密保存密钥，其他系统请用环境变量")
    current = load(data_dir)
    current.update({k: v for k, v in updates.items() if k in NAMES and v})
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / (FILE + ".tmp")
    tmp.write_bytes(_call("CryptProtectData", json.dumps(current).encode()))
    tmp.replace(data_dir / FILE)
