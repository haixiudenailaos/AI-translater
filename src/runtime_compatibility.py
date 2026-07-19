"""Runtime checks shared by source and frozen application entry points."""

from __future__ import annotations

import sys
from typing import TextIO

MINIMUM_PYTHON = (3, 10)


def is_supported_python(version: tuple[int, int] | None = None) -> bool:
    """Return whether the interpreter meets the minimum version requirement."""
    current = version if version is not None else sys.version_info[:2]
    return current >= MINIMUM_PYTHON


def unsupported_python_message(version: tuple[int, int] | None = None) -> str:
    """Build a concise actionable message without importing Tk."""
    current = version if version is not None else sys.version_info[:2]
    return (
        f"当前 Python {current[0]}.{current[1]} 不受支持。"
        "轻小说翻译器 1.6 支持 Python 3.10 及以上版本。"
    )


def ensure_supported_python(
    version: tuple[int, int] | None = None,
    *,
    stderr: TextIO | None = None,
) -> None:
    """Fail before GUI/import-heavy initialization on unsupported Python."""
    if is_supported_python(version):
        return
    output = stderr if stderr is not None else sys.stderr
    print(unsupported_python_message(version), file=output)
    raise SystemExit(1)
