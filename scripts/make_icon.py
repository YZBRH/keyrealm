#!/usr/bin/env python3
"""生成 Keyrealm 的应用图标资源（确定性，可重复运行）。

母版为 design/icon-source.png（1024×1024 RGBA，圆角方形、白边已抠除），
各尺寸产物均由母版重采样得到。

产物：
  assets/icon.ico        多尺寸 ICO（16/24/32/48/64/128/256），供 PyInstaller、Windows exe 使用
                         —— 小尺寸存 BMP(DIB)、256 存 PNG，见 _write_ico 注释
  assets/icon.png        256×256 PNG，供托盘（pystray）等按路径加载
  frontend/public/favicon.png  32×32 PNG，供前端页面 favicon
  frontend/public/favicon.svg  内嵌位图的 SVG favicon（现代浏览器优先）

用法：python scripts/make_icon.py
"""
from __future__ import annotations

import base64
import io
import struct
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
PUBLIC = ROOT / "frontend" / "public"
SOURCE = ROOT / "design" / "icon-source.png"


def _load_master() -> Image.Image:
    """读取并校验正方形 RGBA 母版。"""
    if not SOURCE.is_file():
        raise SystemExit(f"缺少图标母版：{SOURCE.relative_to(ROOT)}")
    master = Image.open(SOURCE).convert("RGBA")
    if master.width != master.height:
        raise SystemExit(f"图标母版必须为正方形，当前 {master.width}×{master.height}")
    return master


def _svg(master: Image.Image) -> str:
    """内嵌位图的 SVG favicon：128px 位图缩放到 64×64，兼顾高分屏清晰度。"""
    buf = io.BytesIO()
    master.resize((128, 128), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <image width="64" height="64" href="data:image/png;base64,{b64}"/>
</svg>
"""


def _and_mask(frame: Image.Image, n: int) -> bytes:
    """生成 ICO 的 AND 掩码（1bpp，1=透明，自底向上，每行 4 字节对齐）。

    32 位深下 Windows 以 alpha 通道为准，但该字段按 ICONIMAGE 规范必须存在，
    且供不使用 alpha 的旧 GDI 渲染路径回退使用（否则透明圆角会渲染成黑角）。
    """
    stride = ((n + 31) // 32) * 4
    alpha = frame.convert("RGBA").getchannel("A").tobytes()
    mask = bytearray(stride * n)
    for y in range(n):
        src = y * n
        dst = (n - 1 - y) * stride  # 掩码自底向上
        for x in range(n):
            if alpha[src + x] < 128:
                mask[dst + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(mask)


def _write_ico(path: Path, master: Image.Image, sizes: list[int]) -> None:
    """手工组装多尺寸 ICO：小尺寸存 BMP(DIB)，256 存 PNG。

    为什么不直接用 Pillow 的 `save(format="ICO")`：Pillow 默认把**每一帧**都写成
    PNG 压缩条目。而 Windows 只对 256×256 保证支持 PNG 条目——16~128 这些小尺寸
    用 PNG 时，任务栏与资源管理器常无法解码该条目而回退成默认图标（PNG-in-ICO
    是 Vista 起为「256 大图省体积」引入的扩展）。这里按惯例混合：≤128 走
    BITMAPINFOHEADER + 自底向上 BGRA 的 DIB（普遍兼容），256 保留 PNG（官方支持且体积小）。
    """
    blobs: list[tuple[int, bytes]] = []  # (尺寸, 载荷)
    for n in sizes:
        frame = master.resize((n, n), Image.LANCZOS)
        buf = io.BytesIO()
        if n >= 256:
            frame.save(buf, format="PNG", optimize=True)
            blobs.append((n, buf.getvalue()))
        else:
            frame.save(buf, format="DIB")  # 无 14 字节 BITMAPFILEHEADER
            dib = bytearray(buf.getvalue())
            # ICO 内的 DIB 须把 biHeight（偏移 8）记为 2×：XOR 图 + AND 掩码各占一份
            struct.pack_into("<i", dib, 8, n * 2)
            blobs.append((n, bytes(dib) + _and_mask(frame, n)))

    offset = 6 + 16 * len(blobs)  # 载荷起始位置：ICONDIR(6) + 条目表(16×N)
    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    data = bytearray()
    for n, payload in blobs:
        out += struct.pack(
            "<BBBBHHII",
            n if n < 256 else 0,  # bWidth（256 记 0）
            n if n < 256 else 0,  # bHeight
            0,                    # bColorCount（真彩为 0）
            0,                    # bReserved
            1,                    # wPlanes
            32,                   # wBitCount
            len(payload),         # dwBytesInRes
            offset + len(data),   # dwImageOffset
        )
        data += payload
    path.write_bytes(bytes(out) + bytes(data))


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)

    master = _load_master()

    # 多尺寸 ICO：从全分辨率母版逐尺寸重采样（小尺寸 DIB、256 PNG）
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    ico_path = ASSETS / "icon.ico"
    _write_ico(ico_path, master, ico_sizes)

    # 256 PNG（托盘等按路径加载）
    png_path = ASSETS / "icon.png"
    master.resize((256, 256), Image.LANCZOS).save(png_path, format="PNG")

    # 前端 favicon
    fav_png = PUBLIC / "favicon.png"
    master.resize((32, 32), Image.LANCZOS).save(fav_png, format="PNG")
    fav_svg = PUBLIC / "favicon.svg"
    fav_svg.write_text(_svg(master), encoding="utf-8")

    print("已生成图标资源：")
    for p in (ico_path, png_path, fav_png, fav_svg):
        print(f"  {p.relative_to(ROOT).as_posix()}  ({p.stat().st_size} 字节)")

    # 自检：确认 ICO 条目格式符合预期（小尺寸 DIB、256 PNG），防止回归
    for n, fmt in _ico_entries(ico_path):
        print(f"    {n:>3}×{n:<3} {fmt}")
        if n >= 256:
            assert fmt == "PNG", f"{n}×{n} 应为 PNG，实为 {fmt}"
        else:
            assert fmt == "BMP", f"{n}×{n} 应为 BMP(DIB)，实为 {fmt}"


def _ico_entries(path: Path) -> list[tuple[int, str]]:
    """解析 ICO 条目，返回 [(尺寸, 'BMP'|'PNG')]，用于自检。"""
    blob = path.read_bytes()
    _, _, count = struct.unpack_from("<HHH", blob, 0)
    png_magic = b"\x89PNG\r\n\x1a\n"
    out = []
    for i in range(count):
        w, _, _, _, _, _, _, offset = struct.unpack_from("<BBBBHHII", blob, 6 + i * 16)
        out.append((w or 256, "PNG" if blob[offset:offset + 8] == png_magic else "BMP"))
    return out


if __name__ == "__main__":
    main()
