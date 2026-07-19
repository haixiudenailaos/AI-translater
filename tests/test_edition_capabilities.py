#!/usr/bin/env python3
"""P0-2：版本能力契约单元测试。

覆盖 ``PYTHON_UIUX_CURRENT_OPTIMIZATION_AUDIT.md`` P0-2 验收标准：
- ``EditionCapabilities.full()`` 启用 Manga
- ``EditionCapabilities.text()`` 禁用 Manga
- ``detect_edition_capabilities`` 惰性检测并缓存
- Text 版本检测到 ``manga_translator`` 不可导入时返回 Text 能力
- Full 版本检测到 ``manga_translator`` 可导入时返回 Full 能力
- 缓存清除后重新检测
- 不可变性：``frozen=True, slots=True``
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from src.domain.edition import (
    Edition,
    EditionCapabilities,
    detect_edition_capabilities,
    reset_detection_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_detection():
    """每个测试前后清除检测缓存，避免跨测试污染。"""
    reset_detection_for_tests()
    yield
    reset_detection_for_tests()


# ── 工厂方法 ─────────────────────────────────────────────


def test_full_factory_enables_manga():
    """P0-2：full() 工厂返回启用 Manga 的能力。"""
    caps = EditionCapabilities.full()

    assert caps.edition is Edition.FULL
    assert caps.manga_enabled is True
    assert caps.is_full is True
    assert caps.is_text is False


def test_text_factory_disables_manga():
    """P0-2：text() 工厂返回禁用 Manga 的能力。"""
    caps = EditionCapabilities.text()

    assert caps.edition is Edition.TEXT
    assert caps.manga_enabled is False
    assert caps.is_text is True
    assert caps.is_full is False


# ── 不可变性 ─────────────────────────────────────────────


def test_capabilities_is_frozen():
    """P0-2：能力对象不可变，避免运行时被意外篡改。"""
    caps = EditionCapabilities.full()

    with pytest.raises(Exception):
        caps.manga_enabled = False  # type: ignore[misc]


def test_capabilities_has_slots():
    """P0-2：能力对象使用 slots，不能添加新属性。

    Python 3.10 的 frozen+slots dataclass 在设置未声明属性时抛
    ``TypeError``，3.11+ 改为 ``AttributeError``；两者都表示 slot
    保护生效。
    """
    caps = EditionCapabilities.full()

    with pytest.raises((AttributeError, TypeError)):
        caps.new_field = "value"  # type: ignore[attr-defined]


# ── detect_edition_capabilities ──────────────────────────


def test_detect_returns_text_when_manga_translator_is_not_discoverable(monkeypatch):
    """开发环境探测缺失依赖时返回 Text，且不导入该包。"""
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)

    caps = detect_edition_capabilities()

    assert caps.edition is Edition.TEXT
    assert caps.manga_enabled is False


def test_detect_returns_full_when_manga_translator_is_discoverable(monkeypatch):
    """开发环境仅通过模块规格判断 Full，不执行模块代码。"""
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    caps = detect_edition_capabilities()

    assert caps.edition is Edition.FULL
    assert caps.manga_enabled is True


def test_detect_caches_result(monkeypatch):
    """P0-2：检测结果被缓存，第二次调用不再触发规格探测。"""
    call_count = {"n": 0}

    def _counting_find_spec(_name):
        call_count["n"] += 1
        return None

    monkeypatch.setattr(importlib.util, "find_spec", _counting_find_spec)

    first = detect_edition_capabilities()
    second = detect_edition_capabilities()

    assert first is second
    # 第二次调用应命中缓存，不再触发规格探测
    assert call_count["n"] == 1


def test_reset_detection_clears_cache(monkeypatch):
    """P0-2：reset_detection_for_tests 清除缓存后重新检测。"""
    # 第一次：Text（manga_translator 不可发现）
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    first = detect_edition_capabilities()
    assert first.is_text

    # 清除缓存后改为可发现，重新检测应为 Full
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    reset_detection_for_tests()
    second = detect_edition_capabilities()

    assert second.is_full
    assert first is not second


def test_environment_marker_overrides_development_probe(monkeypatch):
    monkeypatch.setenv("AI_TRANSLATER_EDITION", "text")
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: pytest.fail("marker must avoid discovery"),
    )

    assert detect_edition_capabilities().is_text
