import tkinter as tk
import unittest
from unittest.mock import patch

from src.ui.settings_window import SettingsWindow


class _GeometryWindow:
    def __init__(self, screen_width, screen_height):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.geometry_value = None

    def update_idletasks(self):
        pass

    def winfo_screenwidth(self):
        return self.screen_width

    def winfo_screenheight(self):
        return self.screen_height

    def geometry(self, value):
        self.geometry_value = value


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

    def test_window_keeps_a_margin_on_a_small_screen(self):
        dialog = SettingsWindow.__new__(SettingsWindow)
        dialog.window = _GeometryWindow(640, 480)

        dialog.center_window()

        self.assertEqual(dialog.window.geometry_value, "576x416+32+32")

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


if __name__ == "__main__":
    unittest.main()
