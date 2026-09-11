"""Keep Tcl lifetime isolated from tests that create background worker threads."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def test_diagnostic_shortcut_with_and_without_caps_lock():
    script = textwrap.dedent(
        """
        import io
        import logging
        import tkinter as tk
        from tkinter import ttk
        from types import SimpleNamespace
        from main import TranslatorApp

        try:
            root = tk.Tk()
        except tk.TclError:
            raise SystemExit(77)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger("main").addHandler(handler)
        app = TranslatorApp.__new__(TranslatorApp)
        app.root = root
        messages = []
        app.main_window = SimpleNamespace(update_status=messages.append)
        app._bind_stall_diagnostic()
        try:
            for widget_class in (tk.Entry, tk.Text, ttk.Entry, ttk.Treeview):
                widget = widget_class(root)
                widget.pack()
                root.update()
                widget.focus_force()
                root.update()
                for keysym in ("D", "d"):
                    before = len(messages)
                    widget.event_generate("<KeyPress>", keysym=keysym, state=0x5)
                    root.update()
                    assert len(messages) == before + 1, (widget_class, keysym)
                widget.destroy()
            assert output.getvalue().count("in dump_stacks") == 8
        finally:
            root.destroy()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("A display is required for real Tk key dispatch")
    assert result.returncode == 0, result.stderr
