import tkinter as tk
import unittest
from tkinter import ttk
from unittest.mock import patch

from src.ui.settings_window import SettingsWindow


class _GeometryWindow:
    def __init__(self, screen_width, screen_height):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.geometry_value = None
        self.minsize_value = None

    def update_idletasks(self):
        pass

    def winfo_screenwidth(self):
        return self.screen_width

    def winfo_screenheight(self):
        return self.screen_height

    def geometry(self, value):
        self.geometry_value = value

    def minsize(self, width, height):
        self.minsize_value = (width, height)


class _Widget:
    def __init__(self, parent=None, **options):
        self.parent = parent
        self.options = options
        self.pack_options = None

    def pack(self, **options):
        self.pack_options = options


class _DialogWindow:
    def __init__(self):
        self.protocol_value = None
        self.destroyed = False

    def protocol(self, name, callback):
        self.protocol_value = (name, callback)

    def destroy(self):
        self.destroyed = True


class SettingsWindowLayoutTests(unittest.TestCase):
    def test_window_is_compact_and_centered(self):
        dialog = SettingsWindow.__new__(SettingsWindow)
        dialog.window = _GeometryWindow(1920, 1080)

        dialog.center_window()

        self.assertEqual(dialog.window.geometry_value, "600x560+660+260")
        # P2-3：大屏 minsize 等于默认 WINDOW_WIDTH/WINDOW_HEIGHT
        self.assertEqual(dialog.window.minsize_value, (600, 560))

    def test_window_keeps_a_margin_on_a_small_screen(self):
        dialog = SettingsWindow.__new__(SettingsWindow)
        dialog.window = _GeometryWindow(640, 480)

        dialog.center_window()

        self.assertEqual(dialog.window.geometry_value, "576x416+32+32")

    def test_minsize_clamped_to_workspace_on_small_screen(self):
        """P2-3：小屏上 minsize 服从工作区，不超过屏幕 - 边距。"""
        dialog = SettingsWindow.__new__(SettingsWindow)
        dialog.window = _GeometryWindow(640, 480)

        dialog.center_window()

        # 640 - 32*2 = 576；480 - 32*2 = 416
        self.assertEqual(dialog.window.minsize_value, (576, 416))

    def test_minsize_never_exceeds_default_on_large_screen(self):
        """P2-3：大屏上 minsize 等于默认 600x560，不会被放大。"""
        dialog = SettingsWindow.__new__(SettingsWindow)
        dialog.window = _GeometryWindow(3840, 2160)

        dialog.center_window()

        self.assertEqual(dialog.window.minsize_value, (600, 560))

    def test_confirm_button_saves_and_action_bar_is_reserved_at_bottom(self):
        dialog = SettingsWindow.__new__(SettingsWindow)
        dialog.window = _DialogWindow()
        actions = []
        dialog.test_connection = lambda: actions.append("test")
        dialog.save_settings = lambda: actions.append("save")
        dialog.create_api_tab = lambda _notebook: None
        dialog.create_translation_tab = lambda _notebook: None
        dialog.create_volc_tab = lambda _notebook: None

        frames = []
        buttons = []

        def create_frame(parent, **options):
            widget = _Widget(parent, **options)
            frames.append(widget)
            return widget

        def create_button(parent, **options):
            widget = _Widget(parent, **options)
            buttons.append(widget)
            return widget

        with (
            patch("src.ui.settings_window.ttk.Frame", side_effect=create_frame),
            patch("src.ui.settings_window.ttk.Notebook", side_effect=_Widget),
            patch("src.ui.settings_window.ttk.Button", side_effect=create_button),
        ):
            dialog.setup_ui()

        self.assertEqual(frames[0].pack_options["side"], tk.BOTTOM)
        self.assertEqual(
            [button.options["text"] for button in buttons],
            [
                "测试连接",
                "确定",
                "取消",
            ],
        )
        self.assertIs(dialog.confirm_button, buttons[1])

        buttons[1].options["command"]()
        self.assertEqual(actions, ["save"])


class ScrollableCanvasBindingTests(unittest.TestCase):
    """P2-3：可滚动 canvas 必须绑定滚轮、PageUp/PageDown 和 FocusIn。"""

    def _build_real_dialog(self):
        """构造一个真实 Tk root + Toplevel 的 SettingsWindow（仅测事件绑定）。"""
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("当前环境无显示，无法创建 Tk root")
        try:
            from src.config.config_manager import ConfigManager
            from src.domain.secret import StorageStatus

            class _StubPaths:
                config_dir = None
                log_dir = None

            class _StubSecretStore:
                def store(self, identifier, key):
                    return StorageStatus.PERSISTED

                def retrieve(self, identifier):
                    return ""

                def delete(self, identifier):
                    return True

            # 使用最小 config_manager stub，避免触碰真实用户目录
            cfg = ConfigManager.__new__(ConfigManager)
            cfg._secret_store = _StubSecretStore()

            def _get_app_config():
                return {}

            cfg.get_app_config = _get_app_config
            cfg.get_api_config = lambda: {}
            return root, cfg
        except Exception:
            root.destroy()
            raise

    def test_scrollable_tab_binds_wheel_pageup_focusin(self):
        """_create_scrollable_tab 绑定 MouseWheel/Prior/Next/FocusIn。"""
        root, cfg = self._build_real_dialog()
        try:
            dialog = SettingsWindow.__new__(SettingsWindow)
            dialog.window = tk.Toplevel(root)
            dialog.window.withdraw()  # 不显示，仅用于事件绑定

            # 用 ttk.Notebook 真实构造一个 tab
            notebook = ttk.Notebook(dialog.window)
            content = dialog._create_scrollable_tab(notebook, "测试")
            notebook.update()

            # canvas 是 content 的父级
            canvas = content.master

            # 滚轮绑定：canvas 和 content 都应能响应
            self.assertTrue(canvas.bind("<MouseWheel>"))
            self.assertTrue(content.bind("<MouseWheel>"))
            # 键盘翻页：canvas 绑定 Prior/Next
            self.assertTrue(canvas.bind("<Prior>"))
            self.assertTrue(canvas.bind("<Next>"))
            # FocusIn：content 绑定，用于自动滚入视区
            self.assertTrue(content.bind("<FocusIn>"))
            # canvas 可获得焦点（takefocus）
            self.assertEqual(str(canvas.cget("takefocus")), "1")
        finally:
            try:
                dialog.window.destroy()
            except Exception:
                pass
            root.destroy()


if __name__ == "__main__":
    unittest.main()
