"""窗口尺寸恢复与跟踪。"""

from __future__ import annotations

import tkinter as tk
from typing import Any


class WindowGeometryTracker:
    """恢复窗口尺寸，并记住用户最后调整的普通窗口尺寸。"""

    def __init__(
        self,
        window,
        config_manager,
        window_name: str,
        *,
        default_size: tuple[int, int],
        minimum_size: tuple[int, int],
        screen_margin: int = 32,
        center: bool = False,
    ) -> None:
        self.window = window
        self.config_manager = config_manager
        self.window_name = window_name
        self.default_size = default_size
        self.minimum_size = minimum_size
        self.screen_margin = screen_margin
        self.center = center
        self._last_normal_size = default_size
        self._maximized = False
        self._bound = False

    def restore(self) -> None:
        """按当前屏幕范围恢复已保存尺寸，损坏配置自动回退到默认值。"""
        self.window.update_idletasks()
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        available_width = max(320, screen_width - self.screen_margin * 2)
        available_height = max(240, screen_height - self.screen_margin * 2)

        minimum_width = min(self.minimum_size[0], available_width)
        minimum_height = min(self.minimum_size[1], available_height)
        self.window.minsize(minimum_width, minimum_height)

        state = self._load_state()
        requested_width = state.get("width", self.default_size[0])
        requested_height = state.get("height", self.default_size[1])
        width = min(max(requested_width, minimum_width), available_width)
        height = min(max(requested_height, minimum_height), available_height)
        self._last_normal_size = (width, height)
        self._maximized = state.get("maximized") is True

        if self.center:
            x = max(0, (screen_width - width) // 2)
            y = max(0, (screen_height - height) // 2)
            self.window.geometry(f"{width}x{height}+{x}+{y}")
        else:
            self.window.geometry(f"{width}x{height}")

        if self._maximized:
            try:
                self.window.after_idle(self._restore_maximized)
            except (tk.TclError, AttributeError):
                self._maximized = False

    def bind(self) -> None:
        """开始跟踪用户调整；重复调用不会重复绑定。"""
        if self._bound:
            return
        self.window.bind("<Configure>", self._on_configure, add="+")
        self._bound = True

    def save(self, *, persist: bool = False) -> bool:
        """把最后的普通尺寸和最大化状态写回配置管理器。"""
        update = getattr(self.config_manager, "update_window_state", None)
        if not callable(update):
            return False
        width, height = self._last_normal_size
        return bool(
            update(
                self.window_name,
                {
                    "width": width,
                    "height": height,
                    "maximized": self._maximized,
                },
                persist=persist,
            )
        )

    def _load_state(self) -> dict[str, Any]:
        getter = getattr(self.config_manager, "get_window_state", None)
        if not callable(getter):
            return {}
        state = getter(self.window_name)
        if not isinstance(state, dict):
            return {}
        width = state.get("width")
        height = state.get("height")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
        ):
            return {}
        return {
            "width": width,
            "height": height,
            "maximized": state.get("maximized") is True,
        }

    def _restore_maximized(self) -> None:
        try:
            self.window.state("zoomed")
            self._maximized = True
        except tk.TclError:
            self._maximized = False

    def _on_configure(self, event) -> None:
        if event.widget is not self.window:
            return
        try:
            state = self.window.state()
        except tk.TclError:
            return

        self._maximized = state == "zoomed"
        if state != "normal":
            return

        width = int(getattr(event, "width", 0) or 0)
        height = int(getattr(event, "height", 0) or 0)
        if width > 1 and height > 1:
            self._last_normal_size = (width, height)
