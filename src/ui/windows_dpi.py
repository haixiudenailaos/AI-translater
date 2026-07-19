"""Windows DPI awareness setup for native pointer/window coordinates."""

from __future__ import annotations

import ctypes
import sys


def enable_per_monitor_dpi_awareness() -> bool:
    """Enable the best available Windows DPI mode before Tk creates a root.

    Non-Windows platforms deliberately do nothing. Windows first tries the
    Per-Monitor V2 context, then falls back to the older shcore/user32 APIs.
    """
    if sys.platform != "win32":
        return False

    per_monitor_v2 = ctypes.c_void_p(-4)
    try:
        user32 = ctypes.windll.user32
        if user32.SetProcessDpiAwarenessContext(per_monitor_v2):
            return True
    except (AttributeError, OSError):
        pass

    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except (AttributeError, OSError):
        pass

    try:
        user32 = ctypes.windll.user32
        if user32.SetProcessDPIAware():
            return True
        set_thread_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
        return bool(set_thread_context and set_thread_context(per_monitor_v2))
    except (AttributeError, OSError):
        return False
