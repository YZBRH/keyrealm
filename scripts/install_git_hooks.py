#!/usr/bin/env python3
"""把仓库自带的 git 钩子挂上（写 `core.hooksPath`，**仅本仓库**，不动全局配置）。

    .venv/bin/python scripts/install_git_hooks.py

为什么用 `core.hooksPath` 而不是往 `.git/hooks/` 里拷文件：
`.git/` 不进版本库，拷贝出去的钩子改了就没人知道；指向版本库里的
`scripts/git-hooks/` 后，钩子跟着代码一起演进，别人 `pull` 完重跑一次安装即可。

当前只有一条钩子 `pre-commit`：提交前对齐开发模式 Mock 的生成产物
（`frontend/src/api/mockPlugins.js` / `pluginIcons.js`，见 `docs/FRONTEND.md`）。
"""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = ROOT / "scripts" / "git-hooks"
REL_HOOKS = "scripts/git-hooks"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def main() -> int:
    if not (ROOT / ".git").exists():
        print("当前目录不是 git 仓库，已中止。", file=sys.stderr)
        return 1
    if not HOOKS_DIR.is_dir():
        print(f"找不到钩子目录：{HOOKS_DIR}", file=sys.stderr)
        return 1

    # 钩子必须可执行（Windows 上 git 会忽略该位，Linux/macOS 上必须有）
    for hook in sorted(HOOKS_DIR.iterdir()):
        if hook.is_file():
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    current = _git("config", "--get", "core.hooksPath").stdout.strip()
    if current == REL_HOOKS:
        print(f"钩子已就位（core.hooksPath = {REL_HOOKS}），无需改动。")
    elif current:
        # 别默默覆盖别人设过的值——可能是有意为之（如 husky / pre-commit 框架）
        print(
            f"检测到 core.hooksPath 已指向 {current!r}，**未做改动**。\n"
            f"要改用本仓库的钩子，请先确认不再需要它，再执行：\n"
            f"    git config core.hooksPath {REL_HOOKS}",
            file=sys.stderr,
        )
        return 1
    else:
        # 不带 --global：只写本仓库的 .git/config，不影响其它项目
        result = _git("config", "core.hooksPath", REL_HOOKS)
        if result.returncode != 0:
            print(f"设置 core.hooksPath 失败：{result.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"已启用本仓库的 git 钩子（core.hooksPath = {REL_HOOKS}）。")

    hooks = "、".join(h.name for h in sorted(HOOKS_DIR.iterdir()) if h.is_file())
    print(f"生效的钩子：{hooks}")
    print("（仅对本仓库生效；`git commit --no-verify` 可临时跳过。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
