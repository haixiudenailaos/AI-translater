#!/usr/bin/env python3
"""
火山引擎 AI 图片翻译 Provider

包装现有 src/core/image_translator.py 的 ImageTranslator，作为
VolcengineImageTranslationProvider 的内部实现保留。

行为约束：
- 只能由 provider_id="ai_volcengine" 的显式请求触发。
- 执行前校验火山 Key。
- 不再由视觉检测结果选择它，也不标记为推荐路径。
- Manga Provider 失败、无文字或缺少模型时均不得调用它。
"""

import uuid

from ...domain.errors import ImageTranslationCancelled, ImageTranslationConfigError
from ...domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from ...domain.translation import OperationStatus
from ...utils.logger import get_logger

logger = get_logger(__name__)


class VolcengineImageTranslationProvider:
    """火山引擎图生图 Provider（显式可选）"""

    provider_id: str = ImageTranslationProviderId.AI_VOLCENGINE.value

    def __init__(self, config_manager) -> None:
        self._config_manager = config_manager
        self._translator = None  # 惰性创建
        self._cancel_event = None
        import threading

        self._cancel_event = threading.Event()
        self._closed = False

    # ── 公共协议方法 ──────────────────────────────────

    def validate(self, request: ImageTranslationRequest) -> list[str]:
        """执行前校验。"""
        errors: list[str] = []

        volc_key = self._config_manager.get_volc_key()
        if not volc_key:
            errors.append("未配置火山引擎 API Key，无法使用 AI 图片翻译")

        import json
        from pathlib import Path

        mapping_dir = Path(request.mapping_dir)
        images_file = mapping_dir / "images.json"
        if not images_file.exists():
            errors.append("缺少 images.json，请确认已正确导入 EPUB")
        else:
            try:
                data = json.loads(images_file.read_text(encoding="utf-8"))
                if not data.get("image_mappings"):
                    errors.append("images.json 中没有图片数据")
            except Exception as exc:
                errors.append(f"images.json 解析失败: {exc}")

        return errors

    def translate(
        self,
        request: ImageTranslationRequest,
        on_progress=None,
    ) -> ImageTranslationResult:
        """执行火山图生图翻译。"""
        if self._closed:
            raise ImageTranslationConfigError("Provider 已关闭，无法执行翻译")

        run_id = uuid.uuid4().hex
        self._cancel_event.clear()

        volc_key = self._config_manager.get_volc_key()
        if not volc_key:
            raise ImageTranslationConfigError("未配置火山引擎 API Key")

        from ...core.image_translator import ImageTranslator

        translator = ImageTranslator(self._config_manager)
        self._translator = translator

        try:
            from pathlib import Path

            mapping_dir = str(Path(request.mapping_dir))

            # 选定图片覆盖
            image_mappings_override = None
            if request.selected_images:
                import json

                images_data = json.loads(
                    (Path(request.mapping_dir) / "images.json").read_text(encoding="utf-8")
                )
                all_mappings = images_data.get("image_mappings", {})
                selected = set(request.selected_images)
                image_mappings_override = {k: v for k, v in all_mappings.items() if k in selected}

            def progress_cb(success, total, current):
                if on_progress is not None:
                    try:
                        on_progress(
                            ImageTranslationProgress(
                                stage="AI 图片翻译",
                                current=success,
                                total=total,
                                image_path=current,
                            )
                        )
                    except Exception:
                        pass
                if self._cancel_event.is_set():
                    return

            result_map = translator.translate_images(
                mapping_dir,
                request.target_language,
                progress_cb,
                image_mappings_override=image_mappings_override,
                cancel_event=self._cancel_event,
            )
            failure_message = getattr(translator, "last_error", "")
        except ImageTranslationCancelled:
            # P1-8：取消时返回 CANCELLED 结果，不再继续处理
            logger.info("AI 图片翻译已被用户取消")
            return ImageTranslationResult(
                status=OperationStatus.CANCELLED,
                provider_id=ImageTranslationProviderId.AI_VOLCENGINE,
                run_id=run_id,
            )
        except Exception as exc:
            logger.error("AI 图片翻译失败: %s", exc, exc_info=True)
            return ImageTranslationResult(
                status=OperationStatus.FAILED,
                provider_id=ImageTranslationProviderId.AI_VOLCENGINE,
                run_id=run_id,
                failed_images={"_global": self._sanitize_error(str(exc))},
            )
        finally:
            try:
                translator.close()
            except Exception:
                pass
            self._translator = None

        status = OperationStatus.SUCCEEDED if result_map else OperationStatus.FAILED
        return ImageTranslationResult(
            status=status,
            result_map=result_map,
            failed_images=(
                {"_global": failure_message or "AI 图片翻译未产生结果"} if not result_map else {}
            ),
            provider_id=ImageTranslationProviderId.AI_VOLCENGINE,
            run_id=run_id,
        )

    def cancel(self) -> None:
        """取消进行中的翻译，幂等。"""
        self._cancel_event.set()

    def close(self) -> None:
        """释放资源，幂等。"""
        if self._closed:
            return
        self._closed = True
        self._cancel_event.set()
        translator = self._translator
        self._translator = None
        if translator is not None:
            try:
                translator.close()
            except Exception:
                pass

    def _sanitize_error(self, msg: str) -> str:
        text = str(msg)
        for token in ("api_key", "apikey", "Bearer ", "authorization"):
            if token.lower() in text.lower():
                return "[filtered: contains credential token]"
        if len(text) > 300:
            text = text[:300] + "...(truncated)"
        return text
