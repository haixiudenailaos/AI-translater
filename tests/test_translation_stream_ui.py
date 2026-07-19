#!/usr/bin/env python3
"""PERF：TranslationController 静态辅助方法测试。

旧的 ``TkUpdateCoalescer`` 测试已移除：合并器被事件邮箱
（``TranslationEventMailbox``）+ Tk 主线程事件泵（``TkTranslationEventPump``）
取代，详见 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §6
和 ``tests/test_translation_event_mailbox.py``。
"""

from src.application.translation_document import TranslationDocument
from src.ui.translation_controller import TranslationController


def test_only_earliest_unfinished_batch_controls_scrolling():
    """只有最靠前的未完成并发批次可以控制表格滚动。"""
    assert TranslationController._should_follow_stream(
        {
            "batch_start": 0,
            "display_batch_start": 0,
        }
    )
    assert not TranslationController._should_follow_stream(
        {
            "batch_start": 20,
            "display_batch_start": 0,
        }
    )


def test_stream_snapshot_prefers_cumulative_preview():
    """``preview_lines`` 优先于 ``stream_lines``，作为累积完整快照。"""
    batch_data = {
        "preview_lines": ["第一行", "正在生成"],
        "stream_lines": ["正在生成"],
        "stream_start_line": 1,
    }

    assert TranslationController._get_stream_snapshot(batch_data) == (
        ["第一行", "正在生成"],
        0,
    )


def test_stream_snapshot_falls_back_to_stream_lines():
    """无 ``preview_lines`` 时使用 ``stream_lines`` + ``stream_start_line``。"""
    batch_data = {
        "stream_lines": ["第二行", "第三行"],
        "stream_start_line": 1,
    }

    assert TranslationController._get_stream_snapshot(batch_data) == (
        ["第二行", "第三行"],
        1,
    )


def test_stream_snapshot_falls_back_to_current_text():
    """无快照字段时按换行拆分 ``current_text``。"""
    batch_data = {"current_text": "第一行\n第二行"}

    assert TranslationController._get_stream_snapshot(batch_data) == (
        ["第一行", "第二行"],
        0,
    )


# ── P0-4: _apply_to_document 过滤保护 ──────────────────────


def _make_controller_with_document(document):
    """通过 ``__new__`` 绕过 Tk 初始化，构造仅含 ``_document`` 的控制器。

    P0-4 的 ``_apply_to_document`` 只依赖 ``self._document``，因此可以
    跳过完整的 Tk 控件构造，单独验证过滤逻辑。
    """
    controller = TranslationController.__new__(TranslationController)
    controller._document = document
    return controller


def test_apply_to_document_returns_empty_for_empty_input():
    """P0-4：空 updates 输入返回空 dict。"""
    doc = TranslationDocument()
    doc.replace(["A"], [""])
    controller = _make_controller_with_document(doc)

    assert controller._apply_to_document({}) == {}


def test_apply_to_document_accepts_unprotected_rows():
    """P0-4：未被人工编辑保护的行被正常接受。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])
    controller = _make_controller_with_document(doc)

    accepted = controller._apply_to_document({0: "译A", 2: "译C"})

    assert accepted == {0: "译A", 2: "译C"}
    assert doc.target_lines() == ["译A", "", "译C"]


def test_apply_to_document_filters_manually_edited_rows():
    """P0-4：人工编辑过的行不被机器结果覆盖，且不出现在 accepted_updates。

    这是 P0-4 的核心保证：``update_target`` 返回 ``False`` 的行不会
    被传入表格适配器渲染，因此 UI 不会被旧 generation 的机器结果污染。
    """
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["旧人工B", "", ""])
    # 行 0 标记为人工编辑
    doc.update_target(0, "旧人工B", manually_edited=True)
    controller = _make_controller_with_document(doc)

    # 机器结果同时覆盖行 0（受保护）和行 1/2（未保护）
    accepted = controller._apply_to_document({0: "新机器A", 1: "新机器B", 2: "新机器C"})

    # 只有未保护的行被接受
    assert accepted == {1: "新机器B", 2: "新机器C"}
    # 受保护行保留人工译文
    assert doc.target_lines() == ["旧人工B", "新机器B", "新机器C"]
    assert doc.is_manually_edited(0) is True


def test_apply_to_document_all_filtered_returns_empty():
    """P0-4：所有行都被人工编辑保护时返回空 dict，适配器收到空批量。"""
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["人工A", "人工B"])
    doc.update_target(0, "人工A", manually_edited=True)
    doc.update_target(1, "人工B", manually_edited=True)
    controller = _make_controller_with_document(doc)

    accepted = controller._apply_to_document({0: "机器A", 1: "机器B"})

    assert accepted == {}
    assert doc.target_lines() == ["人工A", "人工B"]


def test_apply_to_document_without_document_passes_through():
    """P0-4：无 document 时所有更新都视为可渲染，保留旧行为。"""
    controller = TranslationController.__new__(TranslationController)
    controller._document = None

    updates = {0: "译A", 1: "译B"}
    accepted = controller._apply_to_document(updates)

    assert accepted == updates


def test_apply_to_document_clear_manual_flag_allows_overwrite():
    """P0-4：清除人工编辑标记后机器结果可以覆盖。

    重译场景必须显式调用 ``clear_manual_flag``，否则旧 generation
    的机器结果不会进入 accepted_updates。
    """
    doc = TranslationDocument()
    doc.replace(["A"], ["旧人工"])
    doc.update_target(0, "旧人工", manually_edited=True)
    controller = _make_controller_with_document(doc)

    # 清除标记前：拒绝
    assert controller._apply_to_document({0: "新机器"}) == {}

    # 清除标记后：接受
    doc.clear_manual_flag(0)
    accepted = controller._apply_to_document({0: "新机器"})
    assert accepted == {0: "新机器"}
    assert doc.target_lines() == ["新机器"]
