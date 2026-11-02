"""画 launcher.exe 的图标（深蓝圆底上一只青色小鲸），只用标准库，输出 assets/launcher.ico。

运行：python scripts/make_icon.py
"""
import struct
import zlib
from pathlib import Path

import math

NAVY = (15, 26, 48)
TEAL = (95, 227, 208)
BELLY = (196, 246, 238)
SIZES = [256, 64, 48, 32, 24, 16]

STALK = [(0.60, 0.53), (0.78, 0.37), (0.83, 0.40), (0.69, 0.62)]  # 往上翘的尾巴
FLUKES = [(0.755, 0.335, 0.08, 0.032, -32), (0.865, 0.335, 0.08, 0.032, 32)]  # 两片横着的尾鳍
SPOUT = [(0.36, 0.37, 0.022), (0.36, 0.315, 0.022), (0.305, 0.27, 0.021), (0.415, 0.27, 0.021)]  # 头顶喷的水


def in_ellipse(x, y, cx, cy, a, b, deg=0.0):
    t = math.radians(deg)
    dx, dy = x - cx, y - cy
    u, v = dx * math.cos(t) + dy * math.sin(t), -dx * math.sin(t) + dy * math.cos(t)
    return (u / a) ** 2 + (v / b) ** 2 <= 1


def inside_polygon(x, y, pts):
    hit = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def shade(x, y):
    """(x, y) 在 0–1 之间，返回 (r, g, b, a)。"""
    d2 = (x - 0.5) ** 2 + (y - 0.5) ** 2
    if d2 > 0.48 ** 2:
        return (0, 0, 0, 0)
    body = in_ellipse(x, y, 0.42, 0.585, 0.27, 0.165)
    if body:
        if (x - 0.27) ** 2 + (y - 0.555) ** 2 <= 0.021 ** 2:  # 眼睛
            return (*NAVY, 255)
        if 0.17 < x < 0.37 and abs(y - (0.635 + 0.25 * (x - 0.27) ** 2)) < 0.009:  # 嘴
            return (*NAVY, 255)
        if y > 0.655 + 0.6 * (x - 0.40) ** 2:  # 浅色肚皮
            return (*BELLY, 255)
        return (*TEAL, 255)
    if (inside_polygon(x, y, STALK) or any(in_ellipse(x, y, *f) for f in FLUKES)
            or any((x - cx) ** 2 + (y - cy) ** 2 <= r * r for cx, cy, r in SPOUT)):
        return (*TEAL, 255)
    if d2 > 0.435 ** 2:  # 外圈一道淡青色的边
        return tuple(round(n * 0.4 + t * 0.6) for n, t in zip(NAVY, TEAL)) + (255,)
    return (*NAVY, 255)


def render(n, ss=4):
    rows = []
    for py in range(n):
        row = []
        for px in range(n):
            acc = [0.0, 0.0, 0.0, 0.0]
            for i in range(ss):
                for j in range(ss):
                    r, g, b, a = shade((px + (j + 0.5) / ss) / n, (py + (i + 0.5) / ss) / n)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            a = acc[3] / (ss * ss)
            row.append((0, 0, 0, 0) if acc[3] == 0 else
                       (round(acc[0] / acc[3]), round(acc[1] / acc[3]), round(acc[2] / acc[3]), round(a)))
        rows.append(row)
    return rows


def png(rows):
    n = len(rows)
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib(rows):
    """小尺寸用老式 BMP 格式存，最稳。"""
    n = len(rows)
    pixels = b"".join(bytes((b, g, r, a)) for row in reversed(rows) for r, g, b, a in row)
    mask = b"\x00" * (((n + 31) // 32) * 4 * n)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    return header + pixels + mask


def main():
    images = [(n, png(render(n)) if n == 256 else dib(render(n))) for n in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for n, data in images:
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    out += b"".join(data for _, data in images)
    path = Path(__file__).resolve().parent.parent / "assets" / "launcher.ico"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(out)
    (path.parent / "launcher-256.png").write_bytes(images[0][1])  # 方便预览
    print(f"写好了：{path}（{len(out) // 1024} KB）")


if __name__ == "__main__":
    main()
