#!/usr/bin/env python3
"""PERF：TranslationController 静态辅助方法测试。

旧的 ``TkUpdateCoalescer`` 测试已移除：合并器被事件邮箱
（``TranslationEventMailbox``）+ Tk 主线程事件泵（``TkTranslationEventPump``）
取代，详见 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §6
和 ``tests/test_translation_event_mailbox.py``。
"""

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
