#!/usr/bin/env python3
"""P2-2：主题 token、named font 与对比度核算测试。

验证：
1. 主色 accent 与白色前景对比度 ≥ 4.5:1（WCAG 2.2 普通文本）。
2. 状态色 success/danger/warning 与白色背景对比度 ≥ 4.5:1。
3. ``status_color`` 同时通过文字与颜色双重表达状态。
4. ``configure_named_fonts`` 在 Tk root 就绪后能注册并更新 named font。
5. ``apply_theme`` 把 ttk.Treeview 的背景/选中色改为 token。
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import pytest

from src.ui.theme import (
    COLORS,
    FONT_APP,
    FONT_TREEVIEW,
    accent_button_options,
    apply_theme,
    configure_named_fonts,
    status_color,
)


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.x 相对亮度。"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = (int(hex_color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))

    def _channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.x 对比度。"""
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


class TestContrastTokens:
    """P2-2：颜色 token 对比度核算。"""

    @pytest.mark.parametrize(
        "fg_token,bg_token",
        [
            ("text_on_accent", "accent"),
            ("text_on_accent", "accent_pressed"),
            ("text", "surface"),
            ("success", "surface"),
            ("danger", "surface"),
            ("warning", "surface"),
            ("muted", "surface"),
            ("selected_fg", "selected_bg"),
        ],
    )
    def test_token_pair_meets_wcag_aa(self, fg_token, bg_token):
        """前景/背景 token 对比度 ≥ 4.5:1。"""
        fg = COLORS[fg_token]
        bg = COLORS[bg_token]
        ratio = _contrast_ratio(fg, bg)
        assert ratio >= 4.5, (
            f"{fg_token}({fg}) on {bg_token}({bg}) 对比度仅 {ratio:.2f}:1，"
            f"未达到 WCAG AA 4.5:1"
        )

    def test_old_low_contrast_green_replaced(self):
        """旧 #0a0 (2.55:1) 不应再出现在 success token。"""
        assert COLORS["success"].lower() != "#0a0"
        assert COLORS["success"].lower() != "#0a0000"
        # 新 success 必须满足对比度
        ratio = _contrast_ratio(COLORS["success"], COLORS["surface"])
        assert ratio >= 4.5, f"success 对比度 {ratio:.2f}:1 不达标"

    def test_old_low_contrast_accent_replaced(self):
        """旧 #0D9488 (3.74:1) 不应再作为 accent 默认背景。"""
        assert COLORS["accent"].lower() != "#0d9488"
        ratio = _contrast_ratio(COLORS["text_on_accent"], COLORS["accent"])
        assert ratio >= 4.5, f"accent 对比度 {ratio:.2f}:1 不达标"


class TestStatusColor:
    """P2-2：状态颜色同时使用文字 + 颜色双重表达。"""

    @pytest.mark.parametrize(
        "status,expected_token",
        [
            ("ok", "success"),
            ("可用", "success"),
            ("completed", "success"),
            ("error", "danger"),
            ("失败", "danger"),
            ("检测失败", "danger"),
            ("saving", "warning"),
            ("未保存", "warning"),
            ("检测中", "warning"),
        ],
    )
    def test_status_color_returns_distinct_token(self, status, expected_token):
        """每种状态返回对应 token，颜色不作为唯一表达。"""
        assert status_color(status) == COLORS[expected_token]


class TestAccentButtonOptions:
    """P2-2：强调按钮颜色配置。"""

    def test_default_uses_accent_token(self):
        opts = accent_button_options()
        assert opts["background"] == COLORS["accent"]
        assert opts["foreground"] == COLORS["text_on_accent"]

    def test_pressed_uses_darker_accent(self):
        opts = accent_button_options(pressed=True)
        assert opts["background"] == COLORS["accent_pressed"]
        # pressed 必须比 default 更暗（对比度更高）
        default_ratio = _contrast_ratio(COLORS["text_on_accent"], COLORS["accent"])
        pressed_ratio = _contrast_ratio(COLORS["text_on_accent"], COLORS["accent_pressed"])
        assert pressed_ratio >= default_ratio


@pytest.fixture()
def tk_root():
    """创建临时 Tk root 供 named font 测试使用。"""
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("当前环境无显示，无法创建 Tk root")
    try:
        yield root
    finally:
        root.destroy()


class TestNamedFonts:
    """P2-2：named font 注册与全局更新。"""

    def test_configure_named_fonts_registers_app_font(self, tk_root):
        """configure_named_fonts 创建 AppFont named font。"""
        configure_named_fonts("TkDefaultFont", 11)
        font_obj = tkfont.nametofont(FONT_APP)
        assert font_obj is not None
        assert font_obj.cget("size") == 11

    def test_update_font_size_propagates_to_named_fonts(self, tk_root):
        """改字号后 named font 同步更新，依赖它的 widget 全局生效。"""
        configure_named_fonts("TkDefaultFont", 10)
        before = tkfont.nametofont(FONT_APP).cget("size")

        configure_named_fonts("TkDefaultFont", 14)
        after = tkfont.nametofont(FONT_APP).cget("size")

        assert after == 14
        assert after != before

    def test_clamp_font_size_to_safe_range(self, tk_root):
        """字号被限制在 8-24，避免极端值破坏布局。"""
        configure_named_fonts("TkDefaultFont", 1)
        assert tkfont.nametofont(FONT_APP).cget("size") == 8

        configure_named_fonts("TkDefaultFont", 100)
        assert tkfont.nametofont(FONT_APP).cget("size") == 24


class TestApplyTheme:
    """P2-2：apply_theme 把 ttk.Style 改为 token。"""

    def test_treeview_uses_token_colors(self, tk_root):
        style = ttk.Style()
        apply_theme(style, font_family="TkDefaultFont", font_size=10)

        bg = style.lookup("Treeview", "background")
        assert bg == COLORS["surface"]

        selected_bg = style.map("Treeview", "background")
        # ttk 实现可能返回 list of tuples
        flat = []
        for entry in selected_bg:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                flat.append(entry[1])
            else:
                flat.append(entry)
        assert COLORS["selected_bg"] in flat, f"选中态背景应使用 token，实际: {selected_bg}"
