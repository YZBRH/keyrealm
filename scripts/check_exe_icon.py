"""校验已构建的可执行程序里，PE 图标资源是否真的注册了 assets/icon.ico 的每一帧。

为什么需要它
------------
「资源管理器显示默认图标」有两大类原因，现象一模一样但修法完全不同：

  A. 图标未进 exe（spec 里 icon 为空 / 路径不存在 / 构建用了旧产物）
     → 必须重新构建。
  B. 图标进 exe 了，但 Windows 图标缓存还拿着旧记录
     → 重建无用，要刷缓存（ie4uinit.exe -show，或把 exe 换个目录再看）。

分不清这两者就会在「反复重建但图标依旧」和「明明重建了却去刷缓存」之间来回折腾。

为什么不直接按字节搜
--------------------
本项目的 spec 同时把整个 `assets/` 目录当**数据文件**打进包里（运行时读它设窗口图标），
所以 `assets/icon.ico` 的字节本来就存在于 exe 中。按字节搜到命中，无法区分
「PE 图标资源注册了」和「只是被打包了一份数据文件」——真出问题时会给错结论。
因此这里**解析 PE 的资源目录**：Windows 只看 RT_GROUP_ICON（类型 14）与 RT_ICON
（类型 3），只有它们存在，资源管理器才会显示自定义图标。

用法
----
    python scripts/check_exe_icon.py                       # 默认查 release/ 下的产物
    python scripts/check_exe_icon.py path/to/app.exe

退出码：0 = 图标资源已注册且与 ico 一致；1 = 未注册或帧不一致；2 = 文件/参数问题。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "assets" / "icon.ico"

# 自动探测：Windows 下是 .exe，Linux/macOS 构建产物无扩展名
_CANDIDATES = [
    ROOT / "release" / "Keyrealm.exe",
    ROOT / "release" / "Keyrealm",
]

_RT_ICON = 3
_RT_GROUP_ICON = 14


# ── 读 ICO ────────────────────────────────────────────────────────────────

def read_ico_entries(path: Path) -> list[dict]:
    """解析 ICO，返回每个条目的尺寸与载荷。"""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if (reserved, kind) != (0, 1):
        raise ValueError(f"{path} 不是合法的 ICO（reserved={reserved}, type={kind}）")

    entries: list[dict] = []
    off = 6
    for _ in range(count):
        w, h, _colors, _res, _planes, bpp, size, offset = struct.unpack(
            "<BBBBHHII", data[off:off + 16]
        )
        off += 16
        payload = data[offset:offset + size]
        entries.append({
            "size": w or 256,
            "bpp": bpp,
            "bytes": size,
            "payload": payload,
            "format": "PNG" if payload[:8] == b"\x89PNG\r\n\x1a\n" else "BMP",
        })
    return entries


# ── 最小 PE 解析（只为拿到资源目录）────────────────────────────────────────

def _parse_pe(data: bytes) -> tuple[int, list[tuple[int, int, int]]]:
    """返回 (节表, [(VirtualAddress, VirtualSize, PointerToRawData)])。"""
    if data[:2] != b"MZ":
        raise ValueError("不是 PE 文件（缺少 MZ 头）")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("不是 PE 文件（缺少 PE\\0\\0 签名）")

    machine, nsec, _tds, _psym, _nsym, opt_size, _chars = struct.unpack_from(
        "<HHIIIHH", data, pe_off + 4
    )
    opt_off = pe_off + 24
    magic = struct.unpack_from("<H", data, opt_off)[0]
    # 数据目录起始位置：PE32 在可选头偏移 96，PE32+ 在 112
    dd_off = opt_off + (96 if magic == 0x10B else 112)

    sec_off = opt_off + opt_size
    sections = []
    for i in range(nsec):
        base = sec_off + i * 40
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, base + 8)
        sections.append((va, max(vsize, raw_size), raw_ptr))
    return dd_off, sections


def _rva_to_off(sections, rva: int) -> int:
    for va, size, raw in sections:
        if va <= rva < va + size:
            return rva - va + raw
    raise ValueError(f"RVA 0x{rva:x} 不落在任何节内")


def _walk(data, sections, res_off: int, dir_off: int, path: list, out: list) -> None:
    """递归遍历资源目录树，收集叶子节点（含三级 path: 类型/名称/语言）。"""
    _c, _t, _mj, _mn, n_named, n_id = struct.unpack_from("<IIHHHH", data, res_off + dir_off)
    for i in range(n_named + n_id):
        name, off = struct.unpack_from("<II", data, res_off + dir_off + 16 + i * 8)
        sub = off & 0x7FFFFFFF
        if off & 0x80000000:
            _walk(data, sections, res_off, sub, path + [name], out)
        else:
            data_rva, size = struct.unpack_from("<II", data, res_off + sub)
            start = _rva_to_off(sections, data_rva)
            out.append({"path": path + [name], "data": data[start:start + size]})


def read_pe_icons(exe: Path) -> tuple[list[dict], dict[int, bytes]]:
    """解析 exe 的 PE 资源目录，返回 (RT_GROUP_ICON 声明的帧, {图标ID: RT_ICON 数据})。"""
    data = exe.read_bytes()
    dd_off, sections = _parse_pe(data)
    res_rva, res_size = struct.unpack_from("<II", data, dd_off + 8 * 2)  # 索引 2 = 资源表
    if not res_rva or not res_size:
        raise ValueError("exe 没有资源表（未嵌入任何图标）")

    res_off = _rva_to_off(sections, res_rva)
    leaves: list[dict] = []
    _walk(data, sections, res_off, 0, [], leaves)

    icons = {p["path"][1]: p["data"] for p in leaves if p["path"][0] == _RT_ICON}

    groups: list[dict] = []
    for leaf in (p for p in leaves if p["path"][0] == _RT_GROUP_ICON):
        blob = leaf["data"]
        if len(blob) < 6:
            continue
        count = struct.unpack_from("<H", blob, 4)[0]
        for i in range(count):
            b_w, b_h, _cc, _r, _pl, bpp, nbytes, nid = struct.unpack_from(
                "<BBBBHHIH", blob, 6 + i * 14
            )
            groups.append({
                "size": b_w or 256,
                "bpp": bpp,
                "bytes": nbytes,
                "id": nid,
                "data": icons.get(nid, b""),
            })
    return groups, icons


# ── 比对与输出 ────────────────────────────────────────────────────────────

def check(exe: Path) -> int:
    if not ICO.exists():
        print(f"✗ 找不到 {ICO}，先运行 python scripts/make_icon.py", file=sys.stderr)
        return 2
    if not exe.exists():
        print(f"✗ 找不到可执行文件：{exe}", file=sys.stderr)
        print("  先在 Windows 上运行 python scripts/build.py --frontend", file=sys.stderr)
        return 2

    want = read_ico_entries(ICO)
    print(f"图标文件：{ICO}（{len(want)} 帧，{ICO.stat().st_size} 字节）")
    print(f"可执行文件：{exe}（{exe.stat().st_size} 字节）")
    print()

    try:
        groups, _icons = read_pe_icons(exe)
    except ValueError as e:
        print(f"✗ 读取 PE 图标资源失败：{e}")
        print()
        if str(e).startswith("不是 PE 文件"):
            print("结论：这个文件不是 Windows 可执行程序（PE），路径可能选错了。")
            print("      传入 release/Keyrealm.exe，或先运行 python scripts/build.py --frontend。")
            return 2
        print("结论：exe 里没有 PE 图标资源 → 这次构建没带上图标。逐项排查：")
        print("        1) 构建日志里是否为「[spec] 警告：未找到 assets/icon.ico」；")
        print("        2) 是否在项目根目录执行构建（spec 按 SPECPATH 解析 assets/）；")
        print("        3) exe 的修改时间是否晚于 assets/icon.ico（否则看的是旧产物）。")
        return 1

    if not groups:
        print("✗ exe 有资源表，但没有 RT_GROUP_ICON —— 图标未被注册。")
        return 1

    print(f"PE 资源中 RT_GROUP_ICON 声明 {len(groups)} 帧：")
    # 按 (尺寸, 字节数) 与 ico 逐帧比对：顺序不敏感，但集合必须一致
    want_set = {(e["size"], e["bytes"]) for e in want}
    got_set = {(g["size"], g["bytes"]) for g in groups}

    for g in sorted(groups, key=lambda x: x["size"]):
        payload = g["data"]
        kind = "PNG" if payload[:8] == b"\x89PNG\r\n\x1a\n" else ("BMP" if payload else "缺失")
        hit = (g["size"], g["bytes"]) in want_set and bool(payload)
        print(f"  {'✓' if hit else '✗'} {g['size']:>3}x{g['size']:<3} {kind:<4} "
              f"{g['bytes']:>6} 字节（图标ID={g['id']}）")

    print()
    missing = sorted(want_set - got_set)
    extra = sorted(got_set - want_set)
    if missing:
        print(f"✗ ico 中有 {len(missing)} 帧未注册进 exe：{missing}")
    if extra:
        print(f"⚠ exe 中有 {len(extra)} 帧不在当前 ico 里（可能是旧构建残留）：{extra}")

    if not missing and not extra:
        print("结论：图标资源已正确注册，且与 assets/icon.ico 完全一致。")
        print("      资源管理器仍显示默认图标 → 是 Windows 图标缓存问题，重建没用。依次尝试：")
        print("        1) 把 exe 复制到另一个目录再看（新路径不带缓存，最省事）")
        print("        2) ie4uinit.exe -show 然后重开资源管理器")
        return 0

    print("结论：图标资源与 ico 不一致 → 重新构建（python scripts/build.py --frontend）。")
    return 1


def main() -> int:
    args = sys.argv[1:]
    if args:
        return check(Path(args[0]).expanduser().resolve())
    for cand in _CANDIDATES:
        if cand.exists():
            return check(cand)
    print("✗ release/ 下没有找到构建产物", file=sys.stderr)
    print("  用法：python scripts/check_exe_icon.py <可执行文件路径>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
