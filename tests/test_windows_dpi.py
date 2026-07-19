import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import main
from src.ui import windows_dpi


class WindowsDpiTests(unittest.TestCase):
    def test_non_windows_is_a_noop(self):
        with (
            patch.object(windows_dpi.sys, "platform", "linux"),
            patch.object(windows_dpi.ctypes, "windll", create=True) as windll,
        ):
            self.assertFalse(windows_dpi.enable_per_monitor_dpi_awareness())

        windll.assert_not_called()

    def test_prefers_per_monitor_v2_without_mouse_adjustments(self):
        set_context = Mock(return_value=True)
        windll = SimpleNamespace(
            user32=SimpleNamespace(SetProcessDpiAwarenessContext=set_context),
        )

        with (
            patch.object(windows_dpi.sys, "platform", "win32"),
            patch.object(windows_dpi.ctypes, "windll", windll, create=True),
        ):
            self.assertTrue(windows_dpi.enable_per_monitor_dpi_awareness())

        self.assertEqual(set_context.call_count, 1)
        context = set_context.call_args.args[0]
        self.assertIsInstance(context, ctypes.c_void_p)
        self.assertEqual(context.value, ctypes.c_void_p(-4).value)

    def test_falls_back_for_older_windows(self):
        set_context = Mock(return_value=False)
        set_awareness = Mock(return_value=0)
        set_aware = Mock(return_value=True)
        windll = SimpleNamespace(
            user32=SimpleNamespace(
                SetProcessDpiAwarenessContext=set_context,
                SetProcessDPIAware=set_aware,
            ),
            shcore=SimpleNamespace(SetProcessDpiAwareness=set_awareness),
        )

        with (
            patch.object(windows_dpi.sys, "platform", "win32"),
            patch.object(windows_dpi.ctypes, "windll", windll, create=True),
        ):
            self.assertTrue(windows_dpi.enable_per_monitor_dpi_awareness())

        self.assertEqual(set_awareness.call_args, call(2))
        set_aware.assert_not_called()

    def test_sets_thread_context_when_manifest_already_fixed_process_mode(self):
        set_context = Mock(return_value=False)
        set_awareness = Mock(return_value=5)
        set_aware = Mock(return_value=False)
        set_thread_context = Mock(return_value=123)
        windll = SimpleNamespace(
            user32=SimpleNamespace(
                SetProcessDpiAwarenessContext=set_context,
                SetProcessDPIAware=set_aware,
                SetThreadDpiAwarenessContext=set_thread_context,
            ),
            shcore=SimpleNamespace(SetProcessDpiAwareness=set_awareness),
        )

        with (
            patch.object(windows_dpi.sys, "platform", "win32"),
            patch.object(windows_dpi.ctypes, "windll", windll, create=True),
        ):
            self.assertTrue(windows_dpi.enable_per_monitor_dpi_awareness())

        context = set_thread_context.call_args.args[0]
        self.assertEqual(context.value, ctypes.c_void_p(-4).value)

    def test_dpi_awareness_is_set_before_tk_root_creation(self):
        calls = []

        with (
            patch.object(main, "enable_per_monitor_dpi_awareness", lambda: calls.append("dpi")),
            patch.object(main.tk, "Tk", side_effect=lambda: calls.append("tk") or object()),
            patch.object(main.TranslatorApp, "setup_app"),
        ):
            main.TranslatorApp()

        self.assertEqual(calls, ["dpi", "tk"])


if __name__ == "__main__":
    unittest.main()
