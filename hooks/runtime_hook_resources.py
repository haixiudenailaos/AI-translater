#!/usr/bin/env python3
"""
PyInstaller运行时钩子 - 资源路径修复
BUG-001：路径解析已统一到 src/app_paths.py，此钩子仅保留最小兼容标记。
"""

import sys
from pathlib import Path


def setup_resource_paths() -> Path:
    """Return the bundled resource root without mutating process globals.

    ``AppPaths`` is the single owner of resource discovery. The previous hook
    wrote an unused environment variable at import time and printed to stdout,
    which could corrupt JSON subprocess protocols and made startup order
    observable. Keep this helper for compatibility, but make it side-effect
    free and fail loudly when a frozen bundle is malformed.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


# Do not execute setup at import time. PyInstaller only needs AppPaths, and the
# worker JSON protocol must never receive hook diagnostics on stdout.
