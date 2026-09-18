"""生成应用图标（PNG + ICO），只用 Python 标准库，不依赖 Pillow。

用法:
    py -3.12 tools/make_icons.py
输出:
    app/web/icon-192.png, icon-512.png, icon-32.png
    assets/notice-hub.ico          （Windows 桌面快捷方式用）
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"
ASSETS = ROOT / "assets"

BG = (31, 111, 235)      # #1f6feb
BELL = (255, 255, 255)
SS = 3                   # 超采样倍数，用来做抗锯齿


# ---------------- 图形 ----------------
def _in_rounded_square(x: float, y: float, radius: float = 0.22) -> bool:
    """圆角矩形：把点夹到内部矩形，距离圆心不超过半径即在形状内。"""
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return False
    lo, hi = radius, 1.0 - radius
    cx = min(max(x, lo), hi)
    cy = min(max(y, lo), hi)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _in_bell(x: float, y: float) -> bool:
    # 顶部圆顶（椭圆上半部）
    if 0.26 <= y <= 0.565:
        ex = (x - 0.5) / 0.212
        ey = (y - 0.52) / 0.262
        if ex * ex + ey * ey <= 1.0:
            return True
    # 喇叭口
    if 0.545 <= y <= 0.675:
        half = 0.212 + 0.095 * ((y - 0.545) / 0.13)
        if abs(x - 0.5) <= half:
            return True
    # 底座横条
    if 0.675 <= y <= 0.716:
        if abs(x - 0.5) <= 0.307:
            return True
    # 铃舌
    if (x - 0.5) ** 2 + (y - 0.792) ** 2 <= 0.060 ** 2:
        return True
    return False


def _pixel(x: float, y: float) -> tuple[int, int, int, int]:
    if not _in_rounded_square(x, y):
        return (0, 0, 0, 0)
    if _in_bell(x, y):
        return (*BELL, 255)
    return (*BG, 255)


def render_rgba(size: int) -> list[bytes]:
    """返回逐行 RGBA 字节。每个像素做 SS x SS 超采样。"""
    rows: list[bytes] = []
    step = 1.0 / (size * SS)
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = (px * SS + sx + 0.5) * step
                    y = (py * SS + sy + 0.5) * step
                    pr, pg, pb, pa = _pixel(x, y)
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            n = SS * SS
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                # 亮度按 alpha 加权平均，alpha 取平均
                row += bytes((round(r / a), round(g / a), round(b / a), round(a / n)))
        rows.append(bytes(row))
    return rows


# ---------------- PNG ----------------
def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def encode_png(size: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)   # 每行滤波类型 0
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


# ---------------- ICO ----------------
def encode_ico(images: list[tuple[int, bytes]]) -> bytes:
    """images: [(size, png_bytes)]。直接内嵌 PNG（Vista+ 支持）。"""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + entries + blobs


def main() -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)

    cache: dict[int, bytes] = {}

    def png_for(size: int) -> bytes:
        if size not in cache:
            cache[size] = encode_png(size, render_rgba(size))
        return cache[size]

    for size in (32, 192, 512):
        path = WEB / f"icon-{size}.png"
        path.write_bytes(png_for(size))
        print(f"  写出 {path.relative_to(ROOT)}  ({path.stat().st_size} 字节)")

    ico_sizes = (16, 24, 32, 48, 64, 128, 256)
    ico_path = ASSETS / "notice-hub.ico"
    ico_path.write_bytes(encode_ico([(s, png_for(s)) for s in ico_sizes]))
    print(f"  写出 {ico_path.relative_to(ROOT)}  ({ico_path.stat().st_size} 字节)")

    # 顺手存一张大图，方便你自己看效果
    preview = ASSETS / "icon-preview-256.png"
    preview.write_bytes(png_for(256))
    print(f"  写出 {preview.relative_to(ROOT)}")
    print("图标生成完成。")


if __name__ == "__main__":
    main()
