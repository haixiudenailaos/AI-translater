#!/usr/bin/env python3
"""
内存 Fake Provider（测试安全网）

用于在不接入真实 ML 引擎时验证 Service 的成功、部分成功、失败和取消语义。
"""

import threading
from typing import Callable

from ...domain.errors import ImageTranslationCancelled
from ...domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from ...domain.translation import OperationStatus


class FakeImageTranslationProvider:
    """可控的内存 Provider，用于测试。

    通过配置控制返回结果，便于覆盖各种状态语义。
    """

    def __init__(
        self,
        provider_id: str = ImageTranslationProviderId.MANGA.value,
        *,
        validation_errors: list[str] | None = None,
        result_map: dict | None = None,
        skipped_images: list | None = None,
        failed_images: dict | None = None,
        status: OperationStatus = OperationStatus.SUCCEEDED,
        raise_on_translate: Exception | None = None,
        cancel_raises: bool = False,
    ) -> None:
        self._provider_id = provider_id
        self._validation_errors = validation_errors or []
        self._result_map = result_map or {}
        self._skipped_images = skipped_images or []
        self._failed_images = failed_images or {}
        self._status = status
        self._raise_on_translate = raise_on_translate
        self._cancel_raises = cancel_raises

        self.cancel_called = False
        self.close_called = False
        self.translate_called = False
        self.translate_call_count = 0
        self.last_request: ImageTranslationRequest | None = None
        self._cancel_event = threading.Event()

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def validate(self, request: ImageTranslationRequest) -> list[str]:
        return list(self._validation_errors)

    def translate(
        self,
        request: ImageTranslationRequest,
        on_progress: Callable[[ImageTranslationProgress], None] | None = None,
    ) -> ImageTranslationResult:
        self.translate_called = True
        self.translate_call_count += 1
        self.last_request = request

        if self._cancel_event.is_set():
            raise ImageTranslationCancelled()

        if self._raise_on_translate is not None:
            raise self._raise_on_translate

        if on_progress is not None:
            total = len(self._result_map) + len(self._skipped_images) + len(self._failed_images)
            for i in range(total):
                if self._cancel_event.is_set():
                    raise ImageTranslationCancelled()
                try:
                    on_progress(ImageTranslationProgress(stage="test", current=i, total=total))
                except Exception:
                    pass

        return ImageTranslationResult(
            status=self._status,
            result_map=dict(self._result_map),
            skipped_images=list(self._skipped_images),
            failed_images=dict(self._failed_images),
            provider_id=ImageTranslationProviderId(self._provider_id),
        )

    def cancel(self) -> None:
        self.cancel_called = True
        self._cancel_event.set()
        if self._cancel_raises:
            raise RuntimeError("cancel error (test)")

    def close(self, *, timeout_seconds: float | None = None) -> None:
        self.close_called = True

    def reset(self) -> None:
        """重置调用记录（测试用）。"""
        self.cancel_called = False
        self.close_called = False
        self.translate_called = False
        self.translate_call_count = 0
        self.last_request = None
        self._cancel_event.clear()
