# Linux GTK 专项包矩阵（设计草案）

> **状态**：草案，未实现。本文定义"Qt 通用版 + GTK 专项版"两种 Linux 包共存的策略、
> 构建矩阵、命名规范，以及**程序内更新如何选包**。实现前请先确认文末的「待定决策」。

## 1. 为什么是两种包

Linux 没有系统级 WebView 统一层，pywebview 必须自带一个后端，两条路各有硬约束：

| | **Qt 通用版** | **GTK 专项版** |
|---|---|---|
| 后端 | QtWebEngine（Chromium/Blink 内核） | WebKitGTK（WebKit 内核） |
| 引擎来源 | **打进包**（PySide6 整包） | **用系统的**（WebKitGTK/PyGObject） |
| 自包含 | ✅ 一份包跑所有发行版 | ❌ **绑构建系统** |
| 体积 | 下载 ~253 MB / 装完 ~677 MB | 系统侧 ~105 MB、包内很小 |
| GL 依赖 | ❌ **硬依赖 GL**（无 GL 环境崩） | ✅ **不需要**（Cairo 软件渲染） |
| 前端兼容 | ✅ 内核新且一致 | ⚠️ **随发行版**（老系统 WebKit 太老 → Vue3/Vite 跑不动） |
| 定位 | **通用兜底**：给谁都能跑 | **专项优化**：常用发行版上更轻、更快 |

**策略**：**两包共存**。Qt 是"通用兜底"，GTK 是"针对常用发行版的高性能选项"，后续按需扩充专项档。

## 2. GTK 包的依赖栈与"系统差异"

GTK 包能跑，依赖目标机上一整条栈。**差异就藏在每一层**：

```
glibc → GLib/GIO → GTK3 → WebKitGTK → (typelib + libgirepository) → PyGObject(gi)
```

### 🔴 致命差异（决定"必须按发行版出专项包"）

| 层 | 差异 | 后果 |
|---|---|---|
| **WebKitGTK 版本** | 各发行版悬殊：CentOS 8 ≈ **2.28**、Debian 11 ≈ 2.32、Ubuntu 22.04 ≈ 2.36↑、Debian 12 ≈ 2.40↑、Ubuntu 24.04 ≈ 2.44↑、Kali rolling = **2.52** | **前端引擎水平**。太老 → Vue3/Vite 白屏 |
| **PyGObject(gi)** | `gi` 的 `.so` 带 **Python ABI 标签**（`cpython-3XX-…so`） | 冻结包的 Python（3.12）**import 不了系统的 gi** → 必须**用包内 Python 重新编译 PyGObject**并收进包 |
| **typelib / libgirepository** | typelib 是编译产物，跟 `libgirepository` 版本绑定 | 包内 typelib 与目标机 libgirepository 错配 → `gi.overrides` 断言失败 |
| **GLib/GIO** | 版本跨度大 | 与系统其它组件（如 gvfs）混用时报 `undefined symbol: g_task_set_static_name` 这类错 |

> **结论**：GTK 包**必须"在目标发行版自己的环境里构建"**——`gi` 现编、`glib`/`girepository` 取自构建机，
> 天然对齐上面三条。**不能交叉编译**。

### 🟡 次要差异

| 层 | 差异 | 影响 |
|---|---|---|
| GTK3 | 全是 3.24.x | 通常无碍 |
| AppIndicator | `ayatanaappindicator3`（新）vs `appindicator3`（老） | 托盘；GNOME 还需扩展 |
| glibc | 2.28（CentOS 8）~ 2.43（新） | 决定产物能跑的**最低**系统 |

## 3. 矩阵设计

**一个"档" = 一个构建系统 + 一个架构**。每档在**该系统自己的 CI runner / 容器**里构建。

| 档 (flavor) | 目标系统 | 架构 | WebKit 门槛 | 状态 |
|---|---|---|---|---|
| `linux-gtk-ubuntu20.04` | Ubuntu 20.04 | x86_64 | 2.32↑ | **首批（已在 CI 矩阵）** |
| `linux-gtk-ubuntu22.04` | Ubuntu 22.04 | x86_64 | 2.36↑ | 待加 |
| `linux-gtk-ubuntu24.04` | Ubuntu 24.04 | x86_64 | 2.44↑ | 待加 |
| `linux-gtk-debian12` | Debian 12 | x86_64 | 2.40↑ | 待加 |
| `linux-gtk-rocky9` | RHEL 9 系 | x86_64 | 2.36↑ | 待加 |
| ~~`linux-gtk-centos8`~~ | CentOS 8 | — | **2.28 太老，不出** | 劝用 Qt 通用版 |

> **首批只出 Ubuntu 20.04**（试水）。跑通后再按需加档——**加一档 = CI 加一条矩阵项**，不动功能代码。

**加新档的成本**：CI 加一条矩阵项 + 一个容器镜像。**不改任何功能代码**。

## 4. 包名规范

```
Keyrealm-<版本>-linux-gtk-<构建该包的系统>-<架构>.zip
例：Keyrealm-1.4.4-linux-gtk-ubuntu24.04-x86_64.zip
```

**兼容保留**的两种：
```
Keyrealm-<版本>-linux-general-<架构>.zip     # Qt 通用版
Keyrealm-<版本>-windows-x64.zip
Keyrealm-<版本>-macos-arm64.zip
```

- `<构建该包的系统>`：小写、去空格，用 `ID`+`VERSION_ID` 拼（`ubuntu22.04` / `debian12` / `rocky9`）
- `<架构>`：`x86_64`（与通用版一致；arm64 预留）

## 5. 构建要点

1. **CI 矩阵**：新增 `ubuntu-22.04` / `ubuntu-24.04` / `debian-12` / `rocky-9` 等 runner（或 `container:`）。
2. **系统依赖**（apt/dnf）：`libgirepository1.0-dev libcairo2-dev pkg-config` +
   `gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0 gir1.2-ayatanaappindicator3-0.1`。
3. **Python 绑定**：`pip install "PyGObject>=3.42,<3.51"`（3.51+ 需 girepository-2.0，老系统没有）。
4. **spec**（Linux 段）：
   - 收集 `gi` / `gi.repository.{Gtk,Gdk,GLib,GObject,WebKit2,AyatanaAppIndicator3}` /
     `webview.platforms.gtk` / `pystray._appindicator`
   - `hooksconfig = {"gi": {"module-versions": {"Gtk":"3.0","Gdk":"3.0","WebKit2":"4.1"}}}` **钉 typelib 版本**
5. **烘档标识**：扩展 `scripts/build.py` 写的 `backend/_build_info.py`，加 `LINUX_FLAVOR = "linux-gtk-ubuntu24.04-x86_64"`（Qt 通用版写 `linux-general-x86_64`）。
6. **托盘**：Linux 走 `run_detached()`（已在 `a0da454` 完成）。

> 参考：社区 PR #1（HoshiGT, `ci/linux-release`）已给出 spec/CI 的可工作骨架，可借鉴其 `hooksconfig` 与依赖清单。

## 6. 程序内更新如何选包

**问题**：包的"档"绑定了构建系统，那 `ubuntu24.04` 用户手里的 `ubuntu22.04` 包，更新时该去拿哪一档？

**方案：三级匹配**（写进 `backend/updater.py`）：

1. **按运行系统精确匹配** —— 读 `/etc/os-release` 的 `ID` + `VERSION_ID` 拼 `linux-gtk-<id><ver>-<arch>`；有该资产就用它。
   → 让"手里的包档落后于系统档"的用户**自动切到当前系统的档**。
2. **回退到当前包的 flavor** —— 用 `_build_info.LINUX_FLAVOR`（同档升级）。覆盖"该系统档还没出"的情形（如 24.04 档尚未发布，继续拿 22.04 档的新版本）。
3. **回退到通用版** —— `linux-general-<arch>`（Qt，glibc 基线最低，最保险）。

**边界规则**：
- **glibc 向后兼容**：新系统能跑为老系统构建的包（24.04 的 2.39 ≥ 22.04 包要的 2.35）——**所以跨档能用**，但**有对应档时应切过去**（包内 gi/glib 与系统同代才最稳）。
- **切档必须可见**：更新提示里写明"将更新到 `ubuntu24.04` 档"，不静默换。
- **`platform_pkg()` 需要重写**：现在它硬编码返回 `linux-x64`（见 §7），与任一实际包名都不符，**必须修**。

## 7. 与现有代码的衔接（必做的修正）

- **`backend/updater.py: platform_pkg()`** 目前对 Linux 返回 `linux-x64`，而实际包名是
  `linux-general-x86_64`（Qt）—— **程序内更新在 Linux 上找不到包**。需改为：优先用
  `_build_info.LINUX_FLAVOR`，没有则按 §6 的匹配顺序推导。
- **`backend/updater.py`** 的资产探测需支持"按顺序试多个包名"（404 则下一档）。
- **README**「下载与安装」的 Linux 一节要列出"通用版 / 各专项档"及各自适用系统。

## 8. 已定决策

1. **GTK 包收 typelib**（PR 做法）：绑得更死、档切细 —— `gi` 现场编译 + spec 收 `gi.*` + `hooksconfig` 钉版本。
2. **切档自动**：按 §6 三级匹配 —— "有对应档就自动切，并在更新提示里明示"。
3. **首批只出 `linux-gtk-ubuntu20.04`**，跑通后再按需加档（见 §3）。
4. **CentOS 8 不出 GTK 档**（WebKit 2.28 太老），引导用 Qt 通用版。

## 9. 本次落地范围（与 PR #1 合并时）

- CI 矩阵加 `ubuntu-20.04` / `linux-gtk-ubuntu20.04-x86_64`（GTK 档），与既有 `linux-general-x86_64`（Qt 档）并存；
  依赖与 `--gui` 按 matrix 的 `gui` 字段分流。
- spec 的 `gi.*` 收集**与 `_gui == "gtk"` 挂钩**（Qt 档不拖系统 gi/WebKit 进包）。
- tray 的 Linux `run_detached` 沿用现有实现（`os.name != "nt"` 已覆盖 macOS+Linux），不重复引入 PR 的分支。
- **`backend/updater.py` 的 `platform_pkg()` 命名修正 + §6 三级选包：另立一项做**（1.4.5），本 PR 不动更新逻辑。
