"""判断插件 logo 在深色主题下**需不需要垫一层浅色底**（纯函数，只读文件）。

## 为什么需要它

插件 logo 是品牌标，来源不统一：Simple Icons 那批单色标**不写 fill**，按 SVG 默认
fill 渲染成**纯黑**；其余是彩色标。界面的深色主题底色近黑（黑曜主题卡片 #131316），
纯黑标在它上面对比度约 1.1:1 —— 等于隐形。

统一给**所有** logo 垫一块 88% 白底不行：彩色标也会被套上一圈白框。垫底必须**按 logo
区分**。

## 判据：可见度，不是亮度

取不透明像素 max(r,g,b)（HSV 的 V）的 p90，低于 `LIFT_CUTOFF` 才判「需要垫底」。

用 Rec.709 亮度会误判**饱和色**：纯蓝 #0033FF 的亮度只有 0.215，会被判成「糊」，
但它在近黑底上极其显眼。深色底上「看不看得见」取决于**离黑有多远**，而 V 正是这个量。

## 分布

50 个内置 logo 的 p90 两极分化：

    p90 ≤ 0.094  →  aws / github / anthropic / moonshot / openai（5 个）
    p90 ≥ 0.749  →  其余 45 个

中间是大段真空（0.094 ~ 0.749），阈值取 0.30 极稳，落在这段真空里的任何值结果都一样。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

#: max(r,g,b) 的 p90 低于此值 → 深色底上会糊，需要垫底
LIFT_CUTOFF = 0.30

#: 算「墨迹」的最低不透明度：低于它的是抗锯齿过渡带，不代表标的颜色
INK_ALPHA = 128

#: 位图按此尺寸采样即可（只有 16/22px 要显示，不需要原分辨率）
PROBE_SIZE = (96, 96)

#: 属性式颜色声明：fill="#fff" / stroke='rgb(1,2,3)'
_COLOR_RE = re.compile(
    r"""(?:fill|stroke)\s*=\s*["']\s*(#[0-9a-fA-F]{3,8}|rgb[a]?\([^)]*\)|[a-zA-Z]+)\s*["']"""
)

#: CSS 式颜色声明：`<style>` 里的 `.cls-1 { fill: #1c99ff }`，或行内 style="fill:#fff"。
#: 官方导出的 SVG 常把颜色放在这儿（如 W&B 的 WnB_dark-mode_icon.svg）—— 只认属性式会
#: 把整份文件当成「一个颜色都没声明」，于是按「SVG 默认纯黑」误判成需要垫底，深色主题下
#: 给蓝/黄的彩色标套上一圈白底。`(?<![-\w])` 挡掉 `stroke-width` / `fill-opacity` /
#: `--fill` 这类不含颜色的同前缀属性。
_COLOR_CSS_RE = re.compile(
    r"""(?<![-\w])(?:fill|stroke)\s*:\s*(#[0-9a-fA-F]{3,8}|rgb[a]?\([^)]*\)|[a-zA-Z]+)"""
)

#: 明确表示「不画」的取值，不算墨迹
_SKIP_COLORS = {"none", "transparent", "inherit", "context-fill"}

_NAMED_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "silver": (192, 192, 192),
}


def _parse_color(value: str) -> Optional[tuple[int, int, int]]:
    """把 #rgb / #rgba / #rrggbb / #rrggbbaa / rgb() / 颜色名解析成 RGB。"""
    if value.startswith("#"):
        h = value[1:]
        if len(h) in (3, 4):  # #rgb / #rgba —— 每位重复一次展开
            try:
                return tuple(int(c * 2, 16) for c in h[:3])  # type: ignore[return-value]
            except ValueError:
                return None
        if len(h) in (6, 8):
            try:
                return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
            except ValueError:
                return None
        return None
    if value.startswith("rgb"):
        nums = re.findall(r"[\d.]+", value)
        if len(nums) >= 3:
            try:
                return (int(float(nums[0])), int(float(nums[1])), int(float(nums[2])))
            except ValueError:
                return None
        return None
    return _NAMED_COLORS.get(value)


def _svg_ink(path: Path) -> list[float]:
    """SVG 的墨迹样本 = 每个 fill/stroke 声明颜色的 max(r,g,b)。

    属性式（`fill="#fff"`）与 CSS 式（`<style>{fill: #1c99ff}` / `style="fill:#fff"`）
    都收。两处都拿不到颜色时返回空表，由调用方按「默认纯黑」处理。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    ink: list[float] = []
    for raw in _COLOR_RE.findall(text) + _COLOR_CSS_RE.findall(text):
        value = raw.strip().lower()
        if value == "currentcolor":
            # <img> 里没有可继承的 CSS 颜色，currentColor 会解析成黑
            ink.append(0.0)
            continue
        if value in _SKIP_COLORS:
            continue
        rgb = _parse_color(value)
        if rgb is not None:
            ink.append(max(rgb) / 255.0)
    return ink


def _raster_ink(path: Path) -> list[float]:
    """位图的墨迹样本 = 每个不透明像素的 max(r,g,b)。"""
    from PIL import Image  # 懒导入：不分析位图就不付 Pillow 的导入成本

    with Image.open(path) as im:
        probe = im.convert("RGBA")
        probe.thumbnail(PROBE_SIZE)
        raw = probe.tobytes()  # RGBA，4 字节/像素
    return [
        max(raw[i], raw[i + 1], raw[i + 2]) / 255.0
        for i in range(0, len(raw), 4)
        if raw[i + 3] >= INK_ALPHA
    ]


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.9 * (len(ordered) - 1)))]


def needs_lift(path: Path) -> bool:
    """该 logo 在深色主题下是否需要垫一层浅色底。

    解析不了（文件损坏 / Pillow 缺失）时返回 True —— **垫了只是不好看，不垫就直接看不见**。
    """
    try:
        ink = _svg_ink(path) if path.suffix.lower() == ".svg" else _raster_ink(path)
    except Exception:
        # 文件损坏，或 CLI 构建未打包 Pillow（位图分析不可用）：按「需要垫底」处理。
        return True
    if not ink:
        # SVG 一个颜色都不声明 → 按 SVG 默认 fill 渲染成纯黑，正是最该垫底的一类；
        # 位图没有不透明像素 → 本来就是空图，垫不垫都一样。
        return path.suffix.lower() == ".svg"
    return _p90(ink) < LIFT_CUTOFF
