#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领域层（domain）单元测试

验证阶段 2 重构引入的领域模型：
- OperationStatus 状态枚举
- TranslationOptions / TranslationProgress / TranslationResult 不可变 dataclass
- 领域异常携带正确属性
- core/translation_result.py 兼容层 re-export 行为正确

领域层不依赖任何项目外模块，可独立运行。
"""

import pytest

from src.domain.translation import (
    OperationStatus,
    TranslationOptions,
    TranslationProgress,
    TranslationResult,
)
from src.domain.errors import (
    TranslationRequestError,
    TranslationCancelled,
    EpubFingerprintMismatchError,
    SegmentMappingError,
)


# ── OperationStatus ──────────────────────────────────

class TestOperationStatus:
    def test_status_values_are_strings(self):
        """状态枚举值为字符串，可直接序列化"""
        assert OperationStatus.SUCCEEDED == "succeeded"
        assert OperationStatus.PARTIAL == "partial"
        assert OperationStatus.CANCELLED == "cancelled"
        assert OperationStatus.FAILED == "failed"

    def test_status_distinct(self):
        """四个状态互不相等"""
        statuses = {
            OperationStatus.SUCCEEDED,
            OperationStatus.PARTIAL,
            OperationStatus.CANCELLED,
            OperationStatus.FAILED,
        }
        assert len(statuses) == 4

    def test_status_is_str_enum(self):
        """OperationStatus 是 str Enum，可与字符串比较"""
        assert OperationStatus.SUCCEEDED == "succeeded"
        assert isinstance(OperationStatus.SUCCEEDED, str)


# ── TranslationOptions ───────────────────────────────

class TestTranslationOptions:
    def test_defaults(self):
        """默认值符合预期"""
        opts = TranslationOptions(target_language="中文", model_name="gpt-4")
        assert opts.batch_size == 20
        assert opts.max_retries == 5
        assert opts.temperature == 0.3
        assert opts.max_tokens == 2048

    def test_immutable(self):
        """不可变 dataclass：赋值抛 FrozenInstanceError"""
        opts = TranslationOptions(target_language="中文", model_name="gpt-4")
        with pytest.raises(Exception):
            opts.target_language = "English"

    def test_custom_values(self):
        """可自定义所有字段"""
        opts = TranslationOptions(
            target_language="English",
            model_name="deepseek-chat",
            batch_size=10,
            max_retries=3,
            temperature=0.7,
            max_tokens=4096,
        )
        assert opts.target_language == "English"
        assert opts.model_name == "deepseek-chat"
        assert opts.batch_size == 10
        assert opts.max_retries == 3
        assert opts.temperature == 0.7
        assert opts.max_tokens == 4096


# ── TranslationProgress ──────────────────────────────

class TestTranslationProgress:
    def test_defaults(self):
        """preview_lines 默认为空 tuple"""
        prog = TranslationProgress(completed=5, total=20, batch_start=0)
        assert prog.preview_lines == ()

    def test_immutable(self):
        """不可变"""
        prog = TranslationProgress(completed=5, total=20, batch_start=0)
        with pytest.raises(Exception):
            prog.completed = 10

    def test_with_preview(self):
        """可携带预览译文"""
        prog = TranslationProgress(
            completed=2,
            total=4,
            batch_start=0,
            preview_lines=("你好", "世界"),
        )
        assert prog.preview_lines == ("你好", "世界")
        assert len(prog.preview_lines) == 2

    def test_preview_is_tuple_not_list(self):
        """preview_lines 必须是 tuple 而非 list（不可变）"""
        prog = TranslationProgress(
            completed=1, total=2, batch_start=0,
            preview_lines=("a",),
        )
        assert isinstance(prog.preview_lines, tuple)


# ── TranslationResult ────────────────────────────────

class TestTranslationResult:
    def test_succeeded_result(self):
        """成功结果属性正确"""
        result = TranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=("你好", "世界"),
        )
        assert result.is_success
        assert not result.is_cancelled
        assert not result.is_partial
        assert not result.is_failed
        assert result.failed_indices == ()
        assert result.error_message is None
        assert result.completed_count == 2

    def test_partial_result(self):
        """部分成功结果：failed_indices 非空"""
        result = TranslationResult(
            status=OperationStatus.PARTIAL,
            lines=("你好", "", "再见"),
            failed_indices=(1,),
            error_message="1 行译文缺失",
        )
        assert result.is_partial
        assert not result.is_success
        assert result.completed_count == 2  # 3 - 1 = 2

    def test_cancelled_result(self):
        """取消结果"""
        result = TranslationResult(
            status=OperationStatus.CANCELLED,
            lines=("你好", "", "", ""),
            failed_indices=(1, 2, 3),
        )
        assert result.is_cancelled
        assert result.completed_count == 1

    def test_failed_result(self):
        """失败结果"""
        result = TranslationResult(
            status=OperationStatus.FAILED,
            lines=("", ""),
            failed_indices=(0, 1),
            error_message="网络错误",
        )
        assert result.is_failed
        assert result.completed_count == 0
        assert result.error_message == "网络错误"

    def test_immutable(self):
        """不可变"""
        result = TranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=("a",),
        )
        with pytest.raises(Exception):
            result.status = OperationStatus.FAILED

    def test_lines_is_tuple(self):
        """lines 必须是 tuple 而非 list（不可变）"""
        result = TranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=("a", "b"),
        )
        assert isinstance(result.lines, tuple)
        assert isinstance(result.failed_indices, tuple)


# ── 领域异常 ─────────────────────────────────────────

class TestDomainErrors:
    def test_translation_request_error_carries_failed_indices(self):
        """TranslationRequestError 携带 failed_indices"""
        exc = TranslationRequestError(
            "翻译失败", failed_indices=[0, 2], status_code=500,
        )
        assert exc.failed_indices == [0, 2]
        assert exc.status_code == 500
        assert str(exc) == "翻译失败"

    def test_translation_request_error_defaults(self):
        """默认 failed_indices 为空列表"""
        exc = TranslationRequestError("err")
        assert exc.failed_indices == []
        assert exc.status_code is None

    def test_translation_cancelled_carries_partial_lines(self):
        """TranslationCancelled 携带 partial_lines"""
        exc = TranslationCancelled(partial_lines=["你好", ""])
        assert exc.partial_lines == ["你好", ""]
        assert "取消" in str(exc)

    def test_translation_cancelled_defaults(self):
        """默认 partial_lines 为空列表"""
        exc = TranslationCancelled()
        assert exc.partial_lines == []

    def test_epub_fingerprint_mismatch_carries_fingerprints(self):
        """EpubFingerprintMismatchError 携带期望和实际指纹"""
        exc = EpubFingerprintMismatchError(
            expected="abc123", actual="def456",
        )
        assert exc.expected == "abc123"
        assert exc.actual == "def456"

    def test_segment_mapping_error_carries_reason(self):
        """SegmentMappingError 携带 segment_id 和 reason"""
        exc = SegmentMappingError(
            "原文已变化", segment_id="ch1_p2", reason="source_changed",
        )
        assert exc.segment_id == "ch1_p2"
        assert exc.reason == "source_changed"


# ── 兼容层 re-export ─────────────────────────────────

class TestCompatibilityShim:
    """验证 core/translation_result.py 兼容层正确 re-export 领域类型"""

    def test_translation_status_is_operation_status(self):
        """TranslationStatus 是 OperationStatus 的别名"""
        from src.core.translation_result import (
            TranslationStatus,
            OperationStatus as DomainOperationStatus,
        )
        assert TranslationStatus is DomainOperationStatus
        assert TranslationStatus.SUCCEEDED is OperationStatus.SUCCEEDED

    def test_batch_translation_result_uses_list(self):
        """BatchTranslationResult 使用 list（兼容 translator.py）"""
        from src.core.translation_result import BatchTranslationResult
        result = BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=["a", "b"],
            failed_indices=[0],
        )
        assert isinstance(result.lines, list)
        assert isinstance(result.failed_indices, list)

    def test_errors_reexported(self):
        """异常类从兼容层正确 re-export"""
        from src.core.translation_result import (
            TranslationRequestError as CompatRequestError,
            TranslationCancelled as CompatCancelled,
        )
        assert CompatRequestError is TranslationRequestError
        assert CompatCancelled is TranslationCancelled

    def test_batch_result_status_accepts_operation_status(self):
        """BatchTranslationResult.status 接受 OperationStatus（含别名）"""
        from src.core.translation_result import (
            BatchTranslationResult,
            TranslationStatus,
        )
        result = BatchTranslationResult(
            status=TranslationStatus.SUCCEEDED,
            lines=[],
        )
        assert result.is_success
        assert result.status == OperationStatus.SUCCEEDED
