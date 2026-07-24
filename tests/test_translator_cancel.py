#!/usr/bin/env python3
"""
TranslatorEngine 取消信号与缺失译文检测测试

验证 R2-BUG-008（取消后客户端重建）、R2-BUG-009（取消抛 TranslationCancelled）、
R2-BUG-010（缺失译文检测）的修复行为。

测试策略：
- 使用 FakeApi 替换真实 HTTP 客户端，避免网络依赖
- 直接调用 `_translate_batch` 验证单批次行为
- 通过 `_translate` 验证多批次编排与取消传播
- 不依赖 Tkinter，可纯命令行运行
"""

import pytest

from src.core.translation_result import (
    TranslationCancelled,
    TranslationRequestError,
    TranslationStatus,
)
from src.core.translator import TranslatorEngine

# ── Fakes ─────────────────────────────────────────────


class FakeApi:
    """最小化的 API 替身，记录调用次数并按预设返回流式响应。"""

    def __init__(
        self, response_text: str = "", raise_exc: Exception = None, return_none: bool = False
    ):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.return_none = return_none
        self.call_count = 0
        self.cancelled = False
        self.prompts = []

    def translate_stream(self, prompt, callback):
        self.call_count += 1
        self.prompts.append(prompt)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.return_none:
            return None
        # 模拟流式回调：将完整响应一次性回传
        if callback and self.response_text:
            callback(self.response_text)
        return self.response_text

    def reset_cancel(self):
        self.cancelled = False

    def cancel_requests(self):
        self.cancelled = True


class SequenceApi:
    """按调用顺序返回不同响应的 API 替身。"""

    def __init__(self, responses):
        # responses: List[(response_text, return_none, raise_exc)]
        self.responses = list(responses)
        self.call_count = 0
        self.cancelled = False

    def translate_stream(self, prompt, callback):
        if self.call_count >= len(self.responses):
            raise AssertionError("SequenceApi 调用次数超出预设响应数")
        response_text, return_none, raise_exc = self.responses[self.call_count]
        self.call_count += 1
        if raise_exc is not None:
            raise raise_exc
        if return_none:
            return None
        if callback and response_text:
            callback(response_text)
        return response_text

    def reset_cancel(self):
        self.cancelled = False

    def cancel_requests(self):
        self.cancelled = True


class FakeConfigManager:
    """提供 translator 所需的最小配置接口。"""

    def __init__(
        self,
        batch_lines: int = 20,
        model_name: str = "gpt-4",
        small_model_mode: bool = False,
        global_concurrency: int = 4,
    ):
        self._app_config = {
            "target_language": "中文",
            "translation_prompt": "请翻译以下文本",
            "batch_lines": batch_lines,
            "small_model_mode": small_model_mode,
            "queue_max_in_flight_requests": global_concurrency,
            "queue_hard_request_cap": global_concurrency,
        }
        self._api_config = {"model_name": model_name}

    def get_app_config(self):
        return self._app_config

    def get_api_config(self):
        return self._api_config

    def get_glossary_prompt(self):
        return ""


def _make_engine(
    batch_lines: int = 20,
    api=None,
    *,
    small_model_mode: bool = False,
    global_concurrency: int = 4,
) -> TranslatorEngine:
    cm = FakeConfigManager(
        batch_lines=batch_lines,
        small_model_mode=small_model_mode,
        global_concurrency=global_concurrency,
    )
    engine = TranslatorEngine(cm)
    if api is not None:
        engine.api = api  # 预注入 fake api，绕过 _init_api
    return engine


def _no_op_progress(progress, data):
    """空操作进度回调"""
    pass


# ── _translate_batch 单元测试 ────────────────────────


class TestTranslateBatchSuccess:
    """R2-BUG-010：成功路径验证"""

    def test_hyphen_line_markers_are_parsed_and_removed(self):
        """模型将下划线改成连字符时，行号协议仍能正确解析。"""
        response = "\n".join(f"[LINE-{index:03d}]译文{index}" for index in range(1, 14))
        api = FakeApi(response_text=response)
        engine = _make_engine(api=api)

        result = engine._translate_batch(
            [f"原文{index}" for index in range(1, 14)],
            _no_op_progress,
            0,
            13,
        )

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == [f"译文{index}" for index in range(1, 14)]
        assert result.lines[12] == "译文13"

    @pytest.mark.parametrize(
        "response_text",
        [
            "[LINE 001]你好\n[LINE:002]世界\n[LINE.003]再见",
            "  【LINE－001】你好\n【line：002】世界\n【LINE—003】再见",
            "［ LINE _ 001 ］你好\n［LINE-002］世界\n［LINE 003］再见",
            "- **[LINE-001]**你好\n* **[LINE_002]**世界\n> **[LINE:003]**再见",
        ],
    )
    def test_common_line_marker_variants_are_parsed(self, response_text):
        """容忍模型常见的空格、标点、全角字符及 Markdown 改写。"""
        api = FakeApi(response_text=response_text)
        engine = _make_engine(api=api)

        result = engine._translate_batch(["hello", "world", "goodbye"], _no_op_progress, 0, 3)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == ["你好", "世界", "再见"]

    def test_line_marker_inside_translation_is_not_removed(self):
        """只清理行首协议标记，不误删正文中的同形文本。"""
        api = FakeApi(response_text="说明 [LINE-013] 是一个示例")
        engine = _make_engine(api=api)

        result = engine._translate_batch(["example"], _no_op_progress, 0, 1)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == ["说明 [LINE-013] 是一个示例"]

    def test_stream_progress_only_contains_new_completed_lines(self):
        """PERF-001：流式事件不重建历史文本，只发送新增完整行。"""

        class ChunkedApi(FakeApi):
            def translate_stream(self, prompt, callback):
                self.call_count += 1
                chunks = [
                    "[LINE_001]甲\n[LINE_002]",
                    "乙\n[LINE_003]丙",
                ]
                for chunk in chunks:
                    callback(chunk)
                return "".join(chunks)

        api = ChunkedApi()
        engine = _make_engine(api=api)
        progress_events = []

        def progress_hook(_, data):
            if data.get("streaming"):
                progress_events.append(data)

        result = engine._translate_batch(["one", "two", "three"], progress_hook, 0, 3)

        assert result.status == TranslationStatus.SUCCEEDED
        assert [event["stream_lines"] for event in progress_events] == [["甲"], ["乙"]]
        assert [event["stream_start_line"] for event in progress_events] == [0, 1]
        assert [event["preview_lines"] for event in progress_events] == [
            ["甲"],
            ["甲", "乙", "丙"],
        ]
        assert all("current_text" not in event for event in progress_events)

    def test_stream_progress_hides_hyphen_line_markers(self):
        """流式预览与最终结果使用相同的宽容标记清理规则。"""

        class ChunkedApi(FakeApi):
            def translate_stream(self, prompt, callback):
                self.call_count += 1
                chunks = ["[LINE-001]甲\n[LINE-", "002]乙"]
                for chunk in chunks:
                    callback(chunk)
                return "".join(chunks)

        engine = _make_engine(api=ChunkedApi())
        progress_events = []

        def progress_hook(_, data):
            if data.get("streaming"):
                progress_events.append(data)

        result = engine._translate_batch(["one", "two"], progress_hook, 0, 2)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == ["甲", "乙"]
        assert progress_events[0]["stream_lines"] == ["甲"]
        assert progress_events[-1]["preview_lines"] == ["甲", "乙"]
        assert all("LINE" not in str(event["preview_lines"]) for event in progress_events)

    def test_stream_progress_ignores_blank_separator_lines(self):
        """模型在标记行之间插入空行时，流式预览仍按行号对齐。"""

        class ChunkedApi(FakeApi):
            def translate_stream(self, prompt, callback):
                self.call_count += 1
                chunks = [
                    "[LINE_001]甲\n\n[LINE_002]乙\n",
                    "\n[LINE_003]丙",
                ]
                for chunk in chunks:
                    callback(chunk)
                return "".join(chunks)

        engine = _make_engine(api=ChunkedApi())
        progress_events = []

        def progress_hook(_, data):
            if data.get("streaming"):
                progress_events.append(data)

        result = engine._translate_batch(["one", "two", "three"], progress_hook, 0, 3)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == ["甲", "乙", "丙"]
        assert progress_events[0]["stream_lines"] == ["甲", "乙"]
        assert progress_events[0]["preview_lines"] == ["甲", "乙"]
        assert progress_events[-1]["preview_lines"] == ["甲", "乙", "丙"]
        assert all("" not in event["preview_lines"] for event in progress_events)

    def test_stream_progress_waits_for_missing_marker_instead_of_shifting(self):
        """后续行先出现时不应被预览到缺失行的位置。"""

        class ChunkedApi(FakeApi):
            def translate_stream(self, prompt, callback):
                self.call_count += 1
                chunks = [
                    "[LINE_001]甲\n[LINE_003]丙\n",
                    "[LINE_002]乙",
                ]
                for chunk in chunks:
                    callback(chunk)
                return "[LINE_001]甲\n[LINE_002]乙\n[LINE_003]丙"

        engine = _make_engine(api=ChunkedApi())
        progress_events = []

        def progress_hook(_, data):
            if data.get("streaming"):
                progress_events.append(data)

        result = engine._translate_batch(["one", "two", "three"], progress_hook, 0, 3)

        assert result.status == TranslationStatus.SUCCEEDED
        assert progress_events[0]["preview_lines"] == ["甲"]
        assert progress_events[-1]["preview_lines"] == ["甲", "乙", "丙"]

    def test_all_lines_translated_returns_succeeded(self):
        """所有非空原文都有非空译文时返回 SUCCEEDED"""
        api = FakeApi(response_text="[LINE_001]你好\n[LINE_002]世界\n[LINE_003]再见")
        engine = _make_engine(api=api)

        batch_lines = ["hello", "world", "goodbye"]
        result = engine._translate_batch(batch_lines, _no_op_progress, 0, 3)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == ["你好", "世界", "再见"]
        assert result.failed_indices == []

    def test_empty_source_line_not_counted_as_failed(self):
        """原文为空的行不计为失败，即使译文也为空"""
        api = FakeApi(response_text="[LINE_001]你好\n[LINE_002]\n[LINE_003]再见")
        engine = _make_engine(api=api)

        batch_lines = ["hello", "", "goodbye"]
        result = engine._translate_batch(batch_lines, _no_op_progress, 0, 3)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.failed_indices == []

    def test_empty_batch_returns_succeeded(self):
        """空批次立即返回 SUCCEEDED，不调用 API"""
        api = FakeApi()
        engine = _make_engine(api=api)

        result = engine._translate_batch([], _no_op_progress, 0, 0)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == []
        assert api.call_count == 0


class TestTranslateBatchPartial:
    """R2-BUG-010：缺失译文检测"""

    def test_missing_middle_line_returns_partial(self):
        """中间行译文缺失时返回 PARTIAL + 正确 failed_indices"""
        api = FakeApi(response_text="[LINE_001]你好\n[LINE_003]再见")
        engine = _make_engine(api=api)

        batch_lines = ["hello", "world", "goodbye"]
        result = engine._translate_batch(batch_lines, _no_op_progress, 0, 3)

        assert result.status == TranslationStatus.PARTIAL
        assert result.failed_indices == [1]
        # 已获得的译文仍保留，未覆盖
        assert result.lines[0] == "你好"
        assert result.lines[1] == ""
        assert result.lines[2] == "再见"
        assert "1 行译文缺失" in (result.error_message or "")

    def test_multiple_missing_lines(self):
        """多行缺失全部计入 failed_indices"""
        api = FakeApi(response_text="[LINE_002]世界")
        engine = _make_engine(api=api)

        batch_lines = ["hello", "world", "foo", "bar"]
        result = engine._translate_batch(batch_lines, _no_op_progress, 0, 4)

        assert result.status == TranslationStatus.PARTIAL
        assert result.failed_indices == [0, 2, 3]

    def test_whitespace_only_translation_counts_as_missing(self):
        """仅含空白的译文视为缺失"""
        api = FakeApi(response_text="[LINE_001]你好\n[LINE_002]   \n[LINE_003]再见")
        engine = _make_engine(api=api)

        batch_lines = ["hello", "world", "goodbye"]
        result = engine._translate_batch(batch_lines, _no_op_progress, 0, 3)

        assert result.status == TranslationStatus.PARTIAL
        assert result.failed_indices == [1]


class TestTranslateBatchCancel:
    """R2-BUG-009：取消信号传播"""

    def test_stopped_with_empty_response_raises_translation_cancelled(self):
        """is_stopped=True 且响应为空时抛 TranslationCancelled，不再返回伪造空译文"""
        api = FakeApi(response_text="")
        engine = _make_engine(api=api)
        engine.is_stopped = True

        with pytest.raises(TranslationCancelled) as exc_info:
            engine._translate_batch(["hello", "world"], _no_op_progress, 0, 2)

        # 异常应携带 partial_lines 占位（与原文行数对齐）
        assert exc_info.value.partial_lines == ["", ""]

    def test_stopped_with_exception_raises_translation_cancelled(self):
        """is_stopped=True 且 API 抛异常时抛 TranslationCancelled（而非 RequestError）"""
        api = FakeApi(raise_exc=RuntimeError("network down"))
        engine = _make_engine(api=api)
        engine.is_stopped = True

        with pytest.raises(TranslationCancelled):
            engine._translate_batch(["hello"], _no_op_progress, 0, 1)

    def test_not_stopped_with_empty_response_does_not_retry_in_engine(self):
        """空响应由 API 层重试，翻译层只发起一次逻辑调用。"""
        api = FakeApi(response_text="")
        engine = _make_engine(api=api)
        # is_stopped 保持 False

        with pytest.raises(TranslationRequestError) as exc_info:
            engine._translate_batch(["hello"], _no_op_progress, 0, 1)

        assert api.call_count == 1
        assert exc_info.value.failed_indices == [0]


class TestTranslateBatchRequestError:
    """重试耗尽抛出 TranslationRequestError"""

    def test_exception_is_not_retried_by_engine(self):
        """API 异常由 API 层重试，翻译层不再叠加重试。"""
        api = FakeApi(raise_exc=RuntimeError("network error"))
        engine = _make_engine(api=api)

        with pytest.raises(TranslationRequestError) as exc_info:
            engine._translate_batch(["hello", "world"], _no_op_progress, 0, 2)

        assert api.call_count == 1
        assert exc_info.value.failed_indices == [0, 1]


# ── _translate 整合测试 ──────────────────────────────


class TestTranslateFlow:
    """多批次编排与取消传播"""

    def test_full_flow_success(self, monkeypatch):
        """完整翻译流程：所有批次成功 → SUCCEEDED"""
        # batch_lines=2，4 行原文 → 2 个批次
        api = FakeApi(response_text="[LINE_001]你好\n[LINE_002]世界")
        engine = _make_engine(batch_lines=2, api=api)

        completed = []
        engine._translate(
            "hello\nworld\nfoo\nbar",
            _no_op_progress,
            lambda r: completed.append(r),
        )

        assert len(completed) == 1
        result = completed[0]
        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == ["你好", "世界", "你好", "世界"]
        assert result.failed_indices == []

    def test_small_model_mode_sends_exactly_one_source_line_per_request(self):
        api = FakeApi(response_text="逐行译文")
        engine = _make_engine(batch_lines=20, api=api, small_model_mode=True)

        completed = []
        engine._translate(
            "first\nsecond\nthird",
            _no_op_progress,
            lambda result: completed.append(result),
        )

        assert api.call_count == 3
        assert completed[0].status == TranslationStatus.SUCCEEDED
        assert completed[0].lines == ["逐行译文", "逐行译文", "逐行译文"]
        assert all("[LINE_002]" not in prompt for prompt in api.prompts)

    def test_small_model_stream_rejection_stops_after_first_failed_request(self):
        api = SequenceApi(
            [
                (
                    "",
                    False,
                    TranslationRequestError("流式翻译异常: <ConnectionTerminated error_code:9>"),
                )
            ]
        )
        engine = _make_engine(
            api=api,
            small_model_mode=True,
            global_concurrency=1,
        )
        completed = []

        engine._translate(
            "first\nsecond\nthird",
            _no_op_progress,
            completed.append,
        )

        assert api.call_count == 1
        assert api.cancelled is True
        assert completed[0].status == TranslationStatus.FAILED
        assert set(completed[0].failed_indices) == {0, 1, 2}
        assert "并发批次过大" in (completed[0].error_message or "")
        assert "服务商拒绝" in (completed[0].error_message or "")

    def test_small_model_generic_stream_error_is_not_reported_as_provider_rejection(self):
        api = SequenceApi([("", False, TranslationRequestError("流式翻译异常: EmptyStreamError"))])
        engine = _make_engine(
            api=api,
            small_model_mode=True,
            global_concurrency=1,
        )
        completed = []

        engine._translate(
            "first\nsecond",
            _no_op_progress,
            completed.append,
        )

        error_message = completed[0].error_message or ""
        assert completed[0].status == TranslationStatus.FAILED
        assert "EmptyStreamError" in error_message
        assert "并发批次过大" not in error_message
        assert "服务商拒绝" not in error_message

    def test_cancel_between_batches_preserves_successful(self, monkeypatch):
        """第一批完成后取消：CANCELLED 状态，已确认译文保留，未翻译为空

        R2-BUG-009 协作点：取消不发送成功批次回调，不覆盖已确认译文。
        """
        api = FakeApi(response_text="[LINE_001]你好\n[LINE_002]世界")
        engine = _make_engine(batch_lines=2, api=api)

        def progress_hook(progress, data):
            # 第一批完成（非流式、batch_start=0）后立即取消
            if not data.get("streaming") and data.get("batch_start") == 0:
                engine.is_stopped = True

        completed = []
        engine._translate(
            "hello\nworld\nfoo\nbar",
            progress_hook,
            lambda r: completed.append(r),
        )

        assert len(completed) == 1
        result = completed[0]
        assert result.status == TranslationStatus.CANCELLED
        # 第一批译文保留
        assert result.lines[0] == "你好"
        assert result.lines[1] == "世界"
        # 第二批未被翻译（取消不覆盖）
        assert result.lines[2] == ""
        assert result.lines[3] == ""
        # API 只被调用一次（第二批未进入 _translate_batch）
        assert api.call_count == 1

    def test_partial_flow_with_missing_translation(self, monkeypatch):
        """一批成功一批有缺失译文 → PARTIAL + 全局 failed_indices"""
        # 第一批成功，第二批缺失 [LINE_002]
        api = SequenceApi(
            [
                ("[LINE_001]你好\n[LINE_002]世界", False, None),
                ("[LINE_001]foo译文", False, None),
            ]
        )
        engine = _make_engine(batch_lines=2, api=api)

        completed = []
        engine._translate(
            "hello\nworld\nfoo\nbar",
            _no_op_progress,
            lambda r: completed.append(r),
        )

        assert len(completed) == 1
        result = completed[0]
        assert result.status == TranslationStatus.PARTIAL
        # 第二批的第 1 行（全局索引 3）对应原文 "bar" 缺失译文
        assert 3 in result.failed_indices
        # 第一批译文保留
        assert result.lines[0] == "你好"
        assert result.lines[1] == "世界"

    def test_failed_batch_does_not_block_subsequent(self, monkeypatch):
        """某批次彻底失败时记录失败行，继续处理后续批次 → PARTIAL"""
        # 第一批异常，第二批成功；翻译层不重复第一批。
        api = SequenceApi(
            [
                ("", False, RuntimeError("network down")),
                ("[LINE_001]foo译文\n[LINE_002]bar译文", False, None),  # 第二批成功
            ]
        )
        engine = _make_engine(batch_lines=2, api=api)

        completed = []
        engine._translate(
            "hello\nworld\nfoo\nbar",
            _no_op_progress,
            lambda r: completed.append(r),
        )

        assert len(completed) == 1
        result = completed[0]
        assert result.status == TranslationStatus.PARTIAL
        # 第一批两行失败（全局索引 0、1）
        assert 0 in result.failed_indices
        assert 1 in result.failed_indices
        # 第二批成功
        assert result.lines[2] == "foo译文"
        assert result.lines[3] == "bar译文"

    def test_all_batches_fail_returns_failed(self, monkeypatch):
        """所有批次都失败 → FAILED"""
        # 两批各调用一次，重试已经收敛到 API 层。
        responses = [("", False, RuntimeError("err"))] * 2
        api = SequenceApi(responses)
        engine = _make_engine(batch_lines=2, api=api)

        completed = []
        engine._translate(
            "hello\nworld\nfoo\nbar",
            _no_op_progress,
            lambda r: completed.append(r),
        )

        assert len(completed) == 1
        result = completed[0]
        assert result.status == TranslationStatus.FAILED
        assert set(result.failed_indices) == {0, 1, 2, 3}


# ── R2-BUG-008：取消后客户端重建 ─────────────────────


class TestCancelRecreatesClient:
    """R2-BUG-008：取消后 base_api 客户端状态正确重建

    这里直接测试 BaseAPI 的 _get_client / cancel_requests 行为，
    确保 translator.stop() 调用 api.cancel_requests() 后，
    下次 _get_client() 不会复用已关闭的客户端。
    """

    def test_cancel_then_get_client_recreates(self):
        """取消后 _get_client 返回新的客户端实例"""
        from src.api.base_api import BaseAPI

        api = BaseAPI(
            {
                "base_url": "http://localhost",
                "api_key": "test-key",
                "model_name": "gpt-4",
                "enable_cache": False,
                "enable_batch": False,
            }
        )

        try:
            client_before = api._get_client()
            assert client_before is not None
            assert not client_before.is_closed

            # 取消：应关闭并置空
            api.cancel_requests()
            assert api._current_client is None
            assert client_before.is_closed

            # 再次获取：应创建新客户端
            client_after = api._get_client()
            assert client_after is not None
            assert client_after is not client_before
            assert not client_after.is_closed
        finally:
            api.close()

    def test_get_client_after_close_recreates(self):
        """显式 close() 后 _get_client 也能重建"""
        from src.api.base_api import BaseAPI

        api = BaseAPI(
            {
                "base_url": "http://localhost",
                "api_key": "test-key",
                "model_name": "gpt-4",
                "enable_cache": False,
                "enable_batch": False,
            }
        )

        try:
            client1 = api._get_client()
            api.close()
            assert api._current_client is None
            assert client1.is_closed

            client2 = api._get_client()
            assert client2 is not client1
            assert not client2.is_closed
        finally:
            api.close()
