"""P2-2：桌面 UI 主题 token、named font 与 ttk style 应用。

设计目标（来自 PYTHON_UIUX_CURRENT_OPTIMIZATION_AUDIT P2-2）：
- 使用 ttk theme token 与 Tk named fonts 定义 surface/text/muted/accent/
  danger/focus/spacing，避免散落在各 UI 文件里的硬编码颜色和字体。
- 主色至少满足 WCAG 2.2 普通文本 4.5:1：白色普通文字配 ``#0D9488`` 只有
  3.74:1，因此 accent 改用 ``#0F766E``（约 5.47:1）；状态文字与背景同时
  使用文字 + 颜色双重表达，避免色盲用户无法辨识状态。
- “界面字号”不再只影响 Treeview，而是通过 named font 一次性更新整个 UI。

本模块是单例式工具：``apply_theme(style, font_size)`` 在 MainWindow 与
SettingsWindow 启动时调用一次，后续 ``update_font_size`` 通过
``tkinter.font.nametofont`` 改写 named font 即可全局生效。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ── 颜色 token ───────────────────────────────────────
# 所有颜色都经过对比度核算（白底普通文本）：
# - accent #0F766E ≈ 5.47:1（满足 4.5:1）
# - accent_pressed #115E59 ≈ 7.21:1
# - danger #B91C1C ≈ 5.89:1
# - success #15803D ≈ 4.54:1（取代旧 #0a0 的 2.55:1）
# - muted #5C5C5C ≈ 7.05:1
# - focus_ring #0F766E 用作焦点边框，避免使用低对比灰
COLORS = {
    "surface": "#FFFFFF",
    "surface_alt": "#F5F5F5",
    "surface_muted": "#EDEDED",
    "heading_bg": "#E0E0E0",
    "heading_fg": "#333333",
    "text": "#1F1F1F",
    "text_on_accent": "#FFFFFF",
    "muted": "#5C5C5C",
    "accent": "#0F766E",
    "accent_pressed": "#115E59",
    "accent_hover": "#0D9488",
    "danger": "#B91C1C",
    "danger_pressed": "#991B1B",
    "success": "#15803D",
    "warning": "#B45309",
    "focus_ring": "#0F766E",
    "row_odd": "#FFFFFF",
    "row_even": "#F7F7F7",
    "selected_bg": "#0F766E",
    "selected_fg": "#FFFFFF",
}

# ── 间距 token ───────────────────────────────────────
SPACING = {
    "xs": 2,
    "sm": 4,
    "md": 8,
    "lg": 12,
    "xl": 16,
    "xxl": 24,
}

# ── Named font 名称 ─────────────────────────────────
FONT_APP = "AppFont"
FONT_APP_BOLD = "AppFontBold"
FONT_APP_LARGE = "AppFontLarge"
FONT_APP_SMALL = "AppFontSmall"
FONT_TREEVIEW = "AppTreeviewFont"
FONT_TREEVIEW_HEADING = "AppTreeviewHeadingFont"

_DEFAULT_FONT_FAMILY = "TkDefaultFont"

# P2-2：持有 named font 的 Python 引用，避免 Font 对象被 GC 时
# 把 Tk 解释器里的 named font 一并删除（tkinter.Font 默认 delete_font=True）。
_font_registry: dict[str, "tkfont.Font"] = {}


def _resolve_family(config_family: str | None) -> str:
    """解析字体族：空值或“微软雅黑”回退到 ``TkDefaultFont``。

    P2-2：硬编码“微软雅黑”在缺失字体系统上会回退到方框字符，且与 Emoji
    混排会改变行高。统一通过 named font 派生，让回退只发生在一处。
    """
    if not config_family:
        return _DEFAULT_FONT_FAMILY
    family = config_family.strip()
    if not family or family.lower() == "default":
        return _DEFAULT_FONT_FAMILY
    return family


def configure_named_fonts(family: str | None, size: int) -> None:
    """创建或更新全局 named font。

    ``size`` 为正整数；任何调用 ``update_font_size`` 都会同步刷新所有 named
    font，使依赖它们的 widget（包括 ttk 默认 widget）全局生效。
    """
    resolved_family = _resolve_family(family)
    safe_size = max(8, min(24, int(size or 10)))

    specs = [
        (FONT_APP, {"family": resolved_family, "size": safe_size}),
        (FONT_APP_BOLD, {"family": resolved_family, "size": safe_size, "weight": "bold"}),
        (FONT_APP_SMALL, {"family": resolved_family, "size": max(8, safe_size - 1)}),
        (FONT_APP_LARGE, {"family": resolved_family, "size": safe_size + 2, "weight": "bold"}),
        (FONT_TREEVIEW, {"family": resolved_family, "size": safe_size}),
        (
            FONT_TREEVIEW_HEADING,
            {"family": resolved_family, "size": safe_size, "weight": "bold"},
        ),
    ]
    for name, attrs in specs:
        try:
            existing = tkfont.nametofont(name)
        except tk.TclError:
            # named font 尚未注册，下面会创建
            existing = None
        try:
            if existing is not None:
                existing.configure(**attrs)
                # P2-2：nametofont 返回的 Font 默认 delete_font=False，
                # 但仍需保留引用避免 Python 端被 GC 后 Tk 端 font 被回收。
                existing.delete_font = False
                _font_registry[name] = existing
            else:
                font_obj = tkfont.Font(name=name, **attrs)
                # P2-2：tkfont.Font 默认 delete_font=True，对象 GC 时会从 Tk
                # 解释器删除 named font。我们把对象存入模块级 registry 持有引用，
                # 并显式置 delete_font=False，避免测试切换 Tk root 时旧 Font
                # 的 __del__ 把新 root 里同名 font 一并删除。
                font_obj.delete_font = False
                _font_registry[name] = font_obj
        except tk.TclError:
            # root 尚未创建时无法注册 named font，调用方应在 root 就绪后调用
            continue


def apply_theme(style: ttk.Style, *, font_family: str | None = None, font_size: int = 10) -> None:
    """把主题 token 应用到 ttk.Style 与 named font。

    Args:
        style: ``ttk.Style()`` 实例（root 已创建后获取）
        font_family: 字体族；空值回退到 ``TkDefaultFont``
        font_size: 基础字号，范围 8-24
    """
    configure_named_fonts(font_family, font_size)

    # 让 ttk 默认 widget 使用 named font，后续改字号即可全局生效
    try:
        style.configure(".", font=FONT_APP, background=COLORS["surface"], foreground=COLORS["text"])
    except tk.TclError:
        pass

    # Treeview：不再硬编码 white/#e0e0e0/#0078D7，统一来自 token
    treeview_rowheight = max(28, int(font_size) * 3)
    style.configure(
        "Treeview",
        font=FONT_TREEVIEW,
        rowheight=treeview_rowheight,
        background=COLORS["surface"],
        fieldbackground=COLORS["surface"],
        foreground=COLORS["text"],
    )
    style.configure(
        "Treeview.Heading",
        font=FONT_TREEVIEW_HEADING,
        background=COLORS["heading_bg"],
        foreground=COLORS["heading_fg"],
    )
    style.map(
        "Treeview",
        background=[("selected", COLORS["selected_bg"])],
        foreground=[("selected", COLORS["selected_fg"])],
    )


def update_font_size(font_family: str | None, font_size: int) -> None:
    """运行时更新全局字号（仅刷新 named font，无需重建窗口）。"""
    configure_named_fonts(font_family, font_size)


def accent_button_options(*, pressed: bool = False) -> dict[str, str]:
    """返回主强调按钮的颜色配置（供 ``tk.Button`` 直接使用）。

    P2-2：原 ``#0D9488`` 配白字仅 3.74:1，不满足普通文本 4.5:1；改用
    ``#0F766E``（5.47:1）作为默认背景，``#115E59``（7.21:1）作为按下态。
    """
    if pressed:
        return {
            "background": COLORS["accent_pressed"],
            "activebackground": COLORS["accent_pressed"],
            "foreground": COLORS["text_on_accent"],
            "activeforeground": COLORS["text_on_accent"],
        }
    return {
        "background": COLORS["accent"],
        "activebackground": COLORS["accent_hover"],
        "foreground": COLORS["text_on_accent"],
        "activeforeground": COLORS["text_on_accent"],
    }


def status_color(status: str) -> str:
    """状态文字颜色：success/danger/warning 同时使用文字与颜色双重表达。

    P2-2：旧 ``#0a0`` 配白底仅 2.55:1，色盲用户难以辨识；改为 ``#15803D``
    （4.54:1），且状态文字本身已携带“可用/失败/检测中”语义。
    """
    status_lower = (status or "").lower()
    if status_lower in {"success", "ok", "可用", "已保存", "completed"}:
        return COLORS["success"]
    if status_lower in {"danger", "error", "failed", "失败", "失败}", "检测失败"}:
        return COLORS["danger"]
    if status_lower in {"warning", "pending", "saving", "检测中", "未保存"}:
        return COLORS["warning"]
    return COLORS["text"]
