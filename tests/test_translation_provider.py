#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 3（API 生命周期）测试

验证：
1. TranslationProvider 协议定义正确
2. TranslatorEngineAdapter 满足 TranslationProvider 协议
3. 适配器正确转换回调式接口为同步返回
4. cancel / close 幂等且委托到 engine
5. UiScheduler 协议可被简单实现满足
"""

import pytest
from unittest.mock import MagicMock, patch

from src.application.ports import TranslationProvider, UiScheduler
from src.application.translator_provider import TranslatorEngineAdapter
from src.domain.translation import (
    OperationStatus,
    TranslationOptions,
    TranslationProgress,
    TranslationResult,
)
from src.core.translation_result import BatchTranslationResult, TranslationStatus


# ── 协议符合性测试 ──────────────────────────────────────


class TestProtocolConformance:
    """验证适配器满足 TranslationProvider 协议"""

    def test_adapter_is_translation_provider(self):
        """TranslatorEngineAdapter 实例满足 TranslationProvider 协议"""
        engine = MagicMock()
        adapter = TranslatorEngineAdapter(engine)
        assert isinstance(adapter, TranslationProvider)

    def test_adapter_has_translate_batch(self):
        """适配器有 translate_batch 方法"""
        assert hasattr(TranslatorEngineAdapter, "translate_batch")

    def test_adapter_has_cancel(self):
        """适配器有 cancel 方法"""
        assert hasattr(TranslatorEngineAdapter, "cancel")

    def test_adapter_has_close(self):
        """适配器有 close 方法"""
        assert hasattr(TranslatorEngineAdapter, "close")

    def test_ui_scheduler_protocol_accepts_simple_impl(self):
        """UiScheduler 协议可被简单实现满足"""
        class TkScheduler:
            def __init__(self, root):
                self._root = root

            def submit(self, callback):
                self._root.after(0, callback)

        root = MagicMock()
        scheduler = TkScheduler(root)
        assert isinstance(scheduler, UiScheduler)


# ── 适配器翻译测试 ──────────────────────────────────────


class TestAdapterTranslateBatch:
    """验证适配器正确转换回调式接口为同步返回"""

    def test_successful_translation_returns_succeeded(self):
        """成功翻译返回 SUCCEEDED 状态"""
        engine = MagicMock()
        # 模拟 translate_fast_mode 同步调用 complete_callback
        def fake_translate(content, progress_cb, complete_cb):
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=["你好", "世界"],
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        result = adapter.translate_batch(["hello", "world"], options)

        assert result.status == OperationStatus.SUCCEEDED
        assert result.lines == ("你好", "世界")
        assert result.failed_indices == ()
        assert result.error_message is None

    def test_partial_translation_returns_partial(self):
        """部分翻译返回 PARTIAL 状态和失败索引"""
        engine = MagicMock()
        def fake_translate(content, progress_cb, complete_cb):
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.PARTIAL,
                lines=["你好", ""],
                failed_indices=[1],
                error_message="1 行译文缺失",
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        result = adapter.translate_batch(["hello", "world"], options)

        assert result.status == OperationStatus.PARTIAL
        assert result.lines == ("你好", "")
        assert result.failed_indices == (1,)
        assert result.error_message == "1 行译文缺失"

    def test_cancelled_translation_returns_cancelled(self):
        """取消翻译返回 CANCELLED 状态"""
        engine = MagicMock()
        def fake_translate(content, progress_cb, complete_cb):
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.CANCELLED,
                lines=["你好", ""],
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        result = adapter.translate_batch(["hello", "world"], options)

        assert result.status == OperationStatus.CANCELLED
        assert result.is_cancelled

    def test_failed_translation_returns_failed(self):
        """失败翻译返回 FAILED 状态"""
        engine = MagicMock()
        def fake_translate(content, progress_cb, complete_cb):
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.FAILED,
                lines=["", ""],
                failed_indices=[0, 1],
                error_message="连接超时",
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        result = adapter.translate_batch(["hello", "world"], options)

        assert result.status == OperationStatus.FAILED
        assert result.is_failed
        assert result.failed_indices == (0, 1)
        assert result.error_message == "连接超时"

    def test_no_result_returns_failed(self):
        """translate_fast_mode 未调用 complete_callback 时返回 FAILED"""
        engine = MagicMock()
        # 不调用 complete_callback
        engine.translate_fast_mode.side_effect = lambda c, p, cb: None

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        result = adapter.translate_batch(["hello"], options)

        assert result.status == OperationStatus.FAILED
        assert result.lines == ("",)
        assert result.failed_indices == (0,)
        assert "未返回结果" in result.error_message

    def test_empty_lines_returns_succeeded_with_empty_tuple(self):
        """空行列表返回 SUCCEEDED 和空 tuple"""
        engine = MagicMock()
        def fake_translate(content, progress_cb, complete_cb):
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=[],
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        result = adapter.translate_batch([], options)

        assert result.status == OperationStatus.SUCCEEDED
        assert result.lines == ()


# ── 进度回调测试 ────────────────────────────────────────


class TestProgressCallback:
    """验证适配器正确转换进度回调"""

    def test_progress_callback_invoked(self):
        """on_progress 被正确调用"""
        engine = MagicMock()
        progress_events = []

        def fake_translate(content, progress_cb, complete_cb):
            # 模拟一个批次完成
            progress_cb(50.0, {
                "batch_start": 0,
                "translated_lines": ["你好"],
                "streaming": False,
            })
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=["你好", "世界"],
            ))

        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        adapter.translate_batch(["hello", "world"], options,
                                on_progress=lambda e: progress_events.append(e))

        assert len(progress_events) == 1
        event = progress_events[0]
        assert isinstance(event, TranslationProgress)
        assert event.batch_start == 0
        assert event.preview_lines == ("你好",)
        assert event.total == 2

    def test_progress_callback_none_does_not_crash(self):
        """on_progress=None 时不崩溃"""
        engine = MagicMock()
        def fake_translate(content, progress_cb, complete_cb):
            progress_cb(50.0, {"batch_start": 0, "translated_lines": ["你好"], "streaming": False})
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=["你好"],
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        # 不传 on_progress，不应崩溃
        result = adapter.translate_batch(["hello"], options)
        assert result.status == OperationStatus.SUCCEEDED

    def test_progress_callback_exception_does_not_crash(self):
        """on_progress 抛异常时不影响翻译"""
        engine = MagicMock()
        def fake_translate(content, progress_cb, complete_cb):
            progress_cb(50.0, {"batch_start": 0, "translated_lines": ["你好"], "streaming": False})
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=["你好"],
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")

        def bad_callback(event):
            raise RuntimeError("回调内部错误")

        result = adapter.translate_batch(["hello"], options, on_progress=bad_callback)
        assert result.status == OperationStatus.SUCCEEDED

    def test_streaming_progress_ignored(self):
        """流式进度回调（streaming=True）不产生 preview_lines"""
        engine = MagicMock()
        progress_events = []

        def fake_translate(content, progress_cb, complete_cb):
            progress_cb(25.0, {
                "batch_start": 0,
                "streaming": True,
                "current_text": "你好",
                "expected_lines": 2,
            })
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=["你好", "世界"],
            ))

        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        adapter.translate_batch(["hello", "world"], options,
                                on_progress=lambda e: progress_events.append(e))

        assert len(progress_events) == 1
        event = progress_events[0]
        assert event.preview_lines == ()  # 流式时不产生 preview_lines


# ── 生命周期测试 ────────────────────────────────────────


class TestLifecycle:
    """验证 cancel / close 委托到 engine"""

    def test_cancel_calls_engine_stop(self):
        """cancel 委托到 engine.stop"""
        engine = MagicMock()
        adapter = TranslatorEngineAdapter(engine)
        adapter.cancel()
        engine.stop.assert_called_once()

    def test_close_calls_engine_close(self):
        """close 委托到 engine.close"""
        engine = MagicMock()
        adapter = TranslatorEngineAdapter(engine)
        adapter.close()
        engine.close.assert_called_once()

    def test_cancel_idempotent(self):
        """cancel 多次调用安全"""
        engine = MagicMock()
        adapter = TranslatorEngineAdapter(engine)
        adapter.cancel()
        adapter.cancel()
        adapter.cancel()
        assert engine.stop.call_count == 3  # engine.stop 本身应幂等

    def test_close_idempotent(self):
        """close 多次调用安全"""
        engine = MagicMock()
        adapter = TranslatorEngineAdapter(engine)
        adapter.close()
        adapter.close()
        adapter.close()
        assert engine.close.call_count == 3  # engine.close 本身应幂等

    def test_reset_called_before_translate(self):
        """翻译前调用 engine.reset 重置状态"""
        engine = MagicMock()
        def fake_translate(content, progress_cb, complete_cb):
            complete_cb(BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=["你好"],
            ))
        engine.translate_fast_mode.side_effect = fake_translate

        adapter = TranslatorEngineAdapter(engine)
        options = TranslationOptions(target_language="中文", model_name="test")
        adapter.translate_batch(["hello"], options)

        engine.reset.assert_called_once()
        # reset 在 translate_fast_mode 之前调用（通过 mock_calls 顺序检查）
        call_names = [c[0] for c in engine.mock_calls]
        assert "reset" in call_names
        assert "translate_fast_mode" in call_names
        assert call_names.index("reset") < call_names.index("translate_fast_mode")
