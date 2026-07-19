from __future__ import annotations

from io import StringIO

import pytest

from src.runtime_compatibility import (
    ensure_supported_python,
    is_supported_python,
    unsupported_python_message,
)


def test_python_310_and_later_releases_are_supported():
    assert is_supported_python((3, 10))
    assert is_supported_python((3, 11))
    assert is_supported_python((3, 12))
    assert is_supported_python((3, 13))
    assert is_supported_python((4, 0))
    assert not is_supported_python((3, 9))


def test_unsupported_python_fails_with_actionable_chinese_message():
    output = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        ensure_supported_python((3, 9), stderr=output)

    assert exc_info.value.code == 1
    assert "Python 3.9" in output.getvalue()
    assert "Python 3.10 及以上版本" in unsupported_python_message((3, 9))
