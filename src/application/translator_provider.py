#!/usr/bin/env python3
"""
TranslatorEngine 适配器

将现有的 TranslatorEngine（回调式接口）适配为 TranslationProvider 协议
（同步返回 TranslationResult）。这样应用层可以依赖协议而非具体实现，
便于测试时注入替身。

阶段 3（API 生命周期）交付。后续 TranslatorEngine 可逐步缩减为纯应用服务，
此适配器最终会消失。
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, TypeGuard

from ..domain.translation import (
    OperationStatus,
    TranslationOptions,
    TranslationProgress,
    TranslationResult,
)
from ..utils.logger import get_logger
from .ports import TranslationProvider

logger = get_logger(__name__)


class LegacyBatchResult(Protocol):
    """The result shape exposed by the callback-based translation engine."""

    status: OperationStatus
    lines: Sequence[str]
    failed_indices: Sequence[int]
    error_message: str | None


LegacyProgressCallback = Callable[[float, Mapping[str, object] | None], None]
LegacyCompleteCallback = Callable[[LegacyBatchResult], None]


class LegacyTranslatorEngine(Protocol):
    """Minimal legacy-engine API isolated behind this transitional adapter."""

    def reset(self) -> None:
        """Reset cancellation and transient translation state."""

    def translate_fast_mode(
        self,
        content: str,
        progress_callback: LegacyProgressCallback,
        complete_callback: LegacyCompleteCallback,
    ) -> None:
        """Translate synchronously and invoke the completion callback before returning."""

    def stop(self) -> None:
        """Request cancellation for the active translation."""

    def close(self) -> None:
        """Release resources owned by the engine."""


class TranslatorEngineAdapter(TranslationProvider):
    """把 TranslatorEngine 适配为 TranslationProvider 协议

    现有 TranslatorEngine.translate_fast_mode 使用回调式接口：
        translate_fast_mode(content, progress_callback, complete_callback)

    此适配器将其包装为同步接口：
        translate_batch(lines, options, on_progress) -> TranslationResult

    translate_fast_mode 本身是同步阻塞的（在调用线程中执行所有批次），
    complete_callback 在其返回前被调用，因此适配器无需额外线程等待。
    """

    def __init__(self, engine: LegacyTranslatorEngine) -> None:
        """初始化适配器

        Args:
            engine: TranslatorEngine 实例
        """
        self._engine = engine

    def translate_batch(
        self,
        lines: Sequence[str],
        options: TranslationOptions,
        on_progress: Callable[[TranslationProgress], None] | None = None,
    ) -> TranslationResult:
        """翻译一批原文行

        将 lines 合并为 content，调用 engine.translate_fast_mode，
        通过 complete_callback 收集结果后返回。

        Args:
            lines: 原文行序列
            options: 翻译选项（target_language 等）
            on_progress: 进度回调（可选）

        Returns:
            TranslationResult
        """
        content = "\n".join(lines)
        result_holder: dict[str, LegacyBatchResult] = {}

        def progress_callback(progress: float, batch_data: Mapping[str, object] | None) -> None:
            if on_progress is None:
                return
            # 将旧式字典回调转换为 TranslationProgress
            batch_start = _coerce_progress_index(
                batch_data.get("batch_start") if batch_data else None
            )
            translated_lines = (
                _coerce_preview_lines(batch_data.get("translated_lines"))
                if batch_data and not batch_data.get("streaming", False)
                else ()
            )
            total = len(lines)
            if translated_lines:
                completed = batch_start + len(translated_lines)
            else:
                completed = int(progress * total / 100) if total > 0 else 0
            event = TranslationProgress(
                completed=completed,
                total=total,
                batch_start=batch_start,
                preview_lines=translated_lines,
            )
            try:
                on_progress(event)
            except Exception as e:
                logger.warning("进度回调异常: %s", e)

        def complete_callback(batch_result: LegacyBatchResult) -> None:
            result_holder["result"] = batch_result

        # 重置引擎状态并执行翻译（同步阻塞，返回时 complete_callback 已被调用）
        self._engine.reset()
        self._engine.translate_fast_mode(content, progress_callback, complete_callback)

        batch_result = result_holder.get("result")
        if batch_result is None:
            return TranslationResult(
                status=OperationStatus.FAILED,
                lines=tuple([""] * len(lines)),
                failed_indices=tuple(range(len(lines))),
                error_message="翻译未返回结果（未知错误）",
            )

        # BatchTranslationResult.lines 是 list，转换为 tuple
        return TranslationResult(
            status=batch_result.status,
            lines=tuple(batch_result.lines),
            failed_indices=tuple(batch_result.failed_indices),
            error_message=batch_result.error_message,
        )

    def cancel(self) -> None:
        """取消翻译，幂等可多次调用"""
        self._engine.stop()

    def close(self) -> None:
        """释放资源，幂等可多次调用"""
        self._engine.close()


def _coerce_progress_index(value: object) -> int:
    """Read legacy progress indexes defensively at the adapter boundary."""
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _coerce_preview_lines(value: object) -> tuple[str, ...]:
    """Narrow legacy callback payloads to immutable preview lines."""
    if not _is_non_text_sequence(value):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _is_non_text_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)
