#!/usr/bin/env python3
"""
Manga 图片翻译 Provider

将本项目模型适配到 manga-image-translator 的 MangaTranslator。

设计要点：
- 惰性导入：torch / manga_translator 在调用 translate 时才导入，
  模块导入本身不破坏无 ML 环境的应用启动。
- 复用本项目 OpenAI 兼容文本翻译配置构造 external_llm TranslatorConfig，
  禁止写入 os.environ 避免多任务密钥污染。
- 同一任务复用一个 MangaTranslator 实例，按 EPUB 图片顺序处理。
- 无文字图片记为 skipped，不生成替换映射。
- 单图失败不破坏整个任务，记入 failed_images。
- 取消令牌由 Service 持有；进度 hook 检测取消后抛出 TranslationInterrupt，
  适配器转换为 ImageTranslationCancelled。
- close() 幂等：停止后台 cleanup task、卸载模型、关闭 LLM 客户端、关闭 event loop。
"""

import hashlib
import io
import threading
import uuid
from pathlib import Path

from ...domain.errors import ImageTranslationCancelled, ImageTranslationConfigError
from ...domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from ...domain.translation import OperationStatus
from ...utils.logger import get_logger
from ..image_asset_store import load_image_bytes
from .engine_loader import configure_engine_import_path, diagnose_manga_engine
from .language_codes import stage_label, to_manga_lang
from .runtime import MangaRuntime

logger = get_logger(__name__)

# 防解压炸弹：最大像素数（约 1.2 亿像素）
_MAX_PIXELS = 128_000_000
_OUTPUT_SUBDIR = "translated_images"
_MANGA_SUBDIR = "manga"

# 进度阶段映射（外部状态 -> 本项目 stage 文案）
_STAGE_MAP = {
    "detection": "检测文字",
    "ocr": "识别文字",
    "translating": "翻译文字",
    "mask-generation": "生成去字区域",
    "inpainting": "修复原图",
    "rendering": "渲染译文",
}


class _LocalMangaImageTranslationProvider:
    """运行在 Python 3.11 worker 内的 Manga 实现。"""

    provider_id: str = ImageTranslationProviderId.MANGA.value

    def __init__(
        self,
        config_manager,
        *,
        model_dir: Path | None = None,
        resource_dir: Path | None = None,
        font_path: Path | None = None,
        quality_preset: str = "standard",
        device: str = "auto",
    ) -> None:
        """
        Args:
            config_manager: ConfigManager，用于获取 API 配置和火山 Key。
            model_dir: 模型缓存目录，必须位于 AppPaths 管理的用户数据目录，
                       不能写到源码目录、CWD 或 _MEIPASS。
            font_path: 随程序分发且覆盖目标语言字符的字体路径。
            quality_preset: 质量预设（standard/high_quality/low_memory）。
            device: 设备（auto/cpu/cuda/mps/xpu）。
        """
        self._config_manager = config_manager
        self._model_dir = Path(model_dir) if model_dir else None
        self._resource_dir = Path(resource_dir) if resource_dir else None
        self._font_path = str(font_path) if font_path else None
        self._quality_preset = quality_preset
        self._device = device

        self._runtime = MangaRuntime()
        self._engine = None  # MangaTranslator 实例，惰性创建
        self._cancel_event = threading.Event()
        self._closed = False
        self._engine_lock = threading.Lock()
        # P1-3：稳定的 progress hook 桥接。
        # 旧实现每次 translate() 都向缓存的 engine 追加新的 progress_hook，
        # 没有 finally 移除路径——N 次任务后 engine._progress_hooks 会有 N 个
        # 闭包，旧闭包仍引用前一次任务的 result_map/on_progress，造成：
        # 1) 重复回调  2) 闭包内存泄漏  3) 旧闭包向旧 UI 状态写更新。
        # 改为：engine 上只安装一个固定的桥接 hook，translate() 仅替换
        # ``_current_progress_hook`` 指针；finally 中清空指针，避免悬挂引用。
        self._current_progress_hook: callable | None = None

    # ── 公共协议方法 ──────────────────────────────────

    def validate(self, request: ImageTranslationRequest) -> list[str]:
        """执行前校验，返回错误消息列表。"""
        errors: list[str] = []

        # 语言映射校验
        manga_lang = to_manga_lang(request.target_language)
        if manga_lang is None:
            errors.append(
                f"不支持的目标语言: {request.target_language}，请在设置中选择已支持的语言"
            )

        # images.json 校验
        mapping_dir = Path(request.mapping_dir)
        images_file = mapping_dir / "images.json"
        if not images_file.exists():
            errors.append("缺少 images.json，请确认已正确导入 EPUB")
        else:
            import json

            try:
                data = json.loads(images_file.read_text(encoding="utf-8"))
                if not data.get("image_mappings"):
                    errors.append("images.json 中没有图片数据")
            except Exception as exc:
                errors.append(f"images.json 解析失败: {exc}")

        # 引擎与全局 Python 环境校验（不加载模型权重）
        errors.extend(diagnose_manga_engine(self._resource_dir))

        # 文本翻译 API 配置校验（external_llm 需要 base_url 和 model）
        api_config = self._config_manager.get_api_config()
        if not api_config.get("base_url"):
            errors.append("缺少文本翻译 API base_url 配置")
        if not api_config.get("api_key"):
            errors.append("缺少文本翻译 API Key 配置")
        if not api_config.get("model_name"):
            errors.append("缺少文本翻译模型名称配置")

        return errors

    def translate(
        self,
        request: ImageTranslationRequest,
        on_progress=None,
    ) -> ImageTranslationResult:
        """执行 Manga 图片翻译。"""
        if self._closed:
            raise ImageTranslationConfigError("Provider 已关闭，无法执行翻译")

        run_id = uuid.uuid4().hex
        self._cancel_event.clear()

        # 惰性导入重依赖
        try:
            from PIL import Image, ImageOps  # noqa: F401
        except ImportError as exc:
            raise ImageTranslationConfigError("Pillow 未安装，无法处理图片: " + str(exc))

        manga_lang = to_manga_lang(request.target_language)
        if manga_lang is None:
            raise ImageTranslationConfigError(f"不支持的目标语言: {request.target_language}")

        # 读取 images.json
        import json

        mapping_dir = Path(request.mapping_dir)
        images_file = mapping_dir / "images.json"
        try:
            images_data = json.loads(images_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ImageTranslationConfigError(f"读取 images.json 失败: {exc}")

        image_mappings = images_data.get("image_mappings", {})
        if not image_mappings:
            return ImageTranslationResult(
                status=OperationStatus.SUCCEEDED,
                provider_id=ImageTranslationProviderId.MANGA,
                run_id=run_id,
            )

        # 过滤选定图片
        if request.selected_images:
            selected = set(request.selected_images)
            image_mappings = {k: v for k, v in image_mappings.items() if k in selected}

        total = len(image_mappings)
        result_map: dict[str, str] = {}
        skipped_images: list[str] = []
        failed_images: dict[str, str] = {}

        # 输出目录
        output_dir = mapping_dir / _OUTPUT_SUBDIR / _MANGA_SUBDIR
        output_dir.mkdir(parents=True, exist_ok=True)

        # 构造引擎和配置
        engine = self._get_or_create_engine()
        config = self._build_config(manga_lang)

        # P1-3：稳定的 progress hook 桥接。
        # 不再每次 translate() 都 add_progress_hook——engine 已在首次创建时
        # 注册了 _bridge_progress_hook，它从 ``_current_progress_hook`` 读取
        # 当前任务的可调用对象。这里把本任务的 hook 设置进去，并在 finally 中清空。
        def progress_hook(state: str, finished: bool) -> None:
            label = stage_label(state)
            if on_progress is not None:
                idx = len(result_map) + len(skipped_images) + len(failed_images)
                try:
                    on_progress(
                        ImageTranslationProgress(
                            stage=label,
                            current=idx,
                            total=total,
                        )
                    )
                except Exception:
                    pass
            if self._cancel_event.is_set():
                # 抛出外部中断异常，由 translate 协程内捕获
                raise _TranslationInterruptProxy()

        self._current_progress_hook = progress_hook
        try:
            for idx, (image_path, image_info) in enumerate(image_mappings.items()):
                if self._cancel_event.is_set():
                    break

                if on_progress is not None:
                    try:
                        on_progress(
                            ImageTranslationProgress(
                                stage="准备中",
                                current=idx,
                                total=total,
                                image_path=image_path,
                            )
                        )
                    except Exception:
                        pass

                try:
                    pil_image = self._load_image(mapping_dir, image_info)
                    if pil_image is None:
                        failed_images[image_path] = "图片加载失败或损坏"
                        continue

                    ctx = self._run_translate(engine, pil_image, config)

                    # 无文字 -> skipped
                    if not getattr(ctx, "text_regions", None):
                        skipped_images.append(image_path)
                        logger.info("图片无文字，跳过: %s", image_path)
                        continue

                    result_image = getattr(ctx, "result", None)
                    if result_image is None:
                        failed_images[image_path] = "翻译未产生结果图片"
                        continue

                    # 保存结果
                    rel_path = self._save_result(result_image, output_dir, image_path, image_info)
                    if rel_path:
                        result_map[image_path] = rel_path
                    else:
                        failed_images[image_path] = "结果图片保存失败"

                except ImageTranslationCancelled:
                    break
                except Exception as exc:
                    # 单图失败不破坏整个任务
                    err = self._sanitize_error(str(exc))
                    failed_images[image_path] = err
                    logger.error("翻译图片失败: %s: %s", image_path, exc, exc_info=True)
        finally:
            # P1-3：清空当前 hook 指针，避免悬挂闭包引用本任务的 result_map/on_progress。
            # engine 上的桥接 hook 仍保留，下次 translate() 只需设置新指针。
            self._current_progress_hook = None

        # 确定最终状态
        if self._cancel_event.is_set():
            status = OperationStatus.CANCELLED
        elif not result_map and (failed_images or not skipped_images):
            status = OperationStatus.FAILED
        elif result_map and (failed_images or skipped_images):
            status = OperationStatus.PARTIAL
        else:
            status = OperationStatus.SUCCEEDED

        return ImageTranslationResult(
            status=status,
            result_map=result_map,
            skipped_images=skipped_images,
            failed_images=failed_images,
            provider_id=ImageTranslationProviderId.MANGA,
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
        try:
            self._unload_engine()
        except Exception as exc:
            logger.warning("卸载 Manga 引擎失败: %s", exc)
        try:
            self._runtime.shutdown()
        except Exception as exc:
            logger.warning("关闭运行时失败: %s", exc)

    # ── 引擎构造 ──────────────────────────────────

    def _get_or_create_engine(self):
        """惰性创建 MangaTranslator 实例。

        P1-3：首次创建时只注册一个桥接 progress hook，它从
        ``self._current_progress_hook`` 读取当前任务的可调用对象。
        避免 N 次 translate() 后 engine._progress_hooks 累积 N 个闭包。
        """
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            if configure_engine_import_path(self._resource_dir) is None:
                raise ImageTranslationConfigError("未找到 manga_translator 源码")
            from manga_translator.manga_translator import MangaTranslator

            params = self._build_engine_params()
            engine = MangaTranslator(params)
            # P1-3：稳定的桥接 hook，只安装一次
            provider = self  # 闭包捕获 provider 引用，读取当前 hook 指针

            def _bridge_progress_hook(state: str, finished: bool) -> None:
                hook = provider._current_progress_hook
                if hook is None:
                    return
                # 委托到当前任务的 hook，异常由任务侧的 try/except 处理
                hook(state, finished)

            engine.add_progress_hook(_bridge_progress_hook)
            self._engine = engine
            return engine

    def _build_engine_params(self) -> dict:
        """构造 MangaTranslator 初始化参数。"""
        device = self._resolve_device()
        params = {
            "kernel_size": 3,
            "input": [],
            "use_gpu": device != "cpu",
            "ignore_errors": False,
            "batch_size": 1,
            "models_ttl": 0,
            "verbose": False,
            "model_dir": str(self._model_dir) if self._model_dir else None,
            "font_path": self._font_path,
        }
        return params

    def _resolve_device(self) -> str:
        """解析设备（auto 时探测可用加速器）。"""
        device = self._device or "auto"
        if device == "auto":
            try:
                import torch  # noqa: F401

                if hasattr(torch, "cuda") and torch.cuda.is_available():
                    return "cuda"
                if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    return "mps"
                if hasattr(torch, "xpu") and torch.xpu.is_available():
                    return "xpu"
            except Exception:
                pass
            return "cpu"
        return device

    def _build_config(self, manga_lang: str):
        """构造外部 Config，复用本项目 API 配置构造 external_llm。"""
        from manga_translator.config import (
            Config,
            Detector,
            Inpainter,
            Ocr,
            Renderer,
            Translator,
            TranslatorConfig,
        )

        api_config = self._config_manager.get_api_config()
        translator_config = TranslatorConfig(
            translator=Translator.external_llm,
            target_lang=manga_lang,
            external_llm_base_url=api_config.get("base_url", ""),
            external_llm_api_key=api_config.get("api_key", ""),
            external_llm_model=api_config.get("model_name", ""),
        )

        # 质量预设
        detection_size, inpainting_size = self._preset_sizes()

        config = Config()
        config.detector.detector = Detector.default
        config.detector.detection_size = detection_size
        config.ocr.ocr = Ocr.ocr48px
        config.inpainter.inpainter = Inpainter.lama_large
        config.inpainter.inpainting_size = inpainting_size
        config.render.renderer = Renderer.default
        config.translator = translator_config
        config.kernel_size = 3
        return config

    def _preset_sizes(self) -> tuple[int, int]:
        """根据质量预设返回 (detection_size, inpainting_size)。"""
        preset = self._quality_preset or "standard"
        if preset == "high_quality":
            return (2048, 2048)
        if preset == "low_memory":
            return (1024, 1024)
        # standard（默认）
        return (1536, 1536)

    # ── 图片加载与保存 ──────────────────────────────────

    def _load_image(self, mapping_dir: Path, image_info: dict):
        """加载图片为 PIL.Image，统一 EXIF 方向，限制最大像素。"""
        from PIL import Image, ImageOps

        data = load_image_bytes(mapping_dir, image_info)
        if data is None:
            return None
        try:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            # 防解压炸弹
            if img.width * img.height > _MAX_PIXELS:
                logger.warning("图片像素过大，拒绝处理: %dx%d", img.width, img.height)
                return None
            # 统一为 RGB（Manga 引擎要求）
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            return img
        except Exception as exc:
            logger.warning("图片解码失败: %s", exc)
            return None

    def _save_result(
        self,
        result_image,
        output_dir: Path,
        image_path: str,
        image_info: dict,
    ) -> str | None:
        """保存结果图片到 translated_images/manga/<source_hash>.png。

        文件名基于原始 EPUB 路径和内容摘要，避免同名图片互相覆盖。
        只有文件成功原子落盘后才返回相对路径。
        """
        from ..atomic_file import write_bytes_atomic

        # 基于原始路径生成唯一文件名，避免同名图片互相覆盖
        path_hash = hashlib.sha256(image_path.encode("utf-8")).hexdigest()[:12]
        original_path = image_info.get("original_path", image_path)
        path_hash2 = hashlib.sha256(original_path.encode("utf-8")).hexdigest()[:8]
        filename = f"{Path(image_path).stem}_{path_hash}_{path_hash2}.png"
        save_path = output_dir / filename

        try:
            buf = io.BytesIO()
            result_image.save(buf, format="PNG")
            write_bytes_atomic(save_path, buf.getvalue())
        except Exception as exc:
            logger.error("保存结果图片失败: %s: %s", filename, exc)
            return None

        # 返回相对于 mapping_dir 的路径（导出器据此定位文件）
        mapping_dir = output_dir.parent.parent  # output_dir = mapping_dir/translated_images/manga
        try:
            rel = save_path.relative_to(mapping_dir)
            return str(rel).replace("\\", "/")
        except ValueError:
            return str(save_path).replace("\\", "/")

    # ── 协程执行与取消 ──────────────────────────────────

    def _run_translate(self, engine, pil_image, config):
        """在后台 loop 中运行 engine.translate()。"""
        try:
            return self._runtime.run(engine.translate(pil_image, config))
        except _TranslationInterruptProxy:
            raise ImageTranslationCancelled()
        except Exception as exc:
            # 透传外部 TranslationInterrupt
            if "TranslationInterrupt" in type(exc).__name__:
                raise ImageTranslationCancelled()
            raise

    def _unload_engine(self) -> None:
        """卸载已加载模型，关闭引擎。"""
        engine = self._engine
        self._engine = None
        if engine is None:
            return
        # 外部引擎没有完整公开的 close()，在后台 loop 中卸载模型
        try:
            from manga_translator.detection import unload as unload_detection
            from manga_translator.inpainting import unload as unload_inpainting
            from manga_translator.ocr import unload as unload_ocr
            from manga_translator.translators import unload as unload_translation

            async def _cleanup():
                try:
                    await unload_detection(engine.device)
                except Exception:
                    pass
                try:
                    await unload_ocr(engine.device)
                except Exception:
                    pass
                try:
                    await unload_inpainting(engine.device)
                except Exception:
                    pass
                try:
                    await unload_translation()
                except Exception:
                    pass

            self._runtime.run(_cleanup())
        except Exception as exc:
            logger.warning("卸载模型失败: %s", exc)

    def _sanitize_error(self, msg: str) -> str:
        """脱敏错误信息。"""
        text = str(msg)
        for token in ("api_key", "apikey", "Bearer ", "authorization"):
            if token.lower() in text.lower():
                return "[filtered: contains credential token]"
        if len(text) > 300:
            text = text[:300] + "...(truncated)"
        return text


class _TranslationInterruptProxy(Exception):
    """取消代理异常，用于在进度 hook 中中断翻译。"""

    pass


class MangaImageTranslationProvider:
    """Python 3.13-side Provider proxy backed by a Python 3.11 worker."""

    provider_id: str = ImageTranslationProviderId.MANGA.value

    def __init__(
        self,
        config_manager,
        *,
        model_dir: Path | None = None,
        resource_dir: Path | None = None,
        font_path: Path | None = None,
        quality_preset: str = "standard",
        device: str = "auto",
        python_executable: str | None = None,
    ) -> None:
        from .manga_worker_client import MangaWorkerClient

        self._client = MangaWorkerClient(
            config_manager,
            model_dir=model_dir,
            resource_dir=resource_dir,
            font_path=font_path,
            quality_preset=quality_preset,
            device=device,
            python_executable=python_executable,
        )

    def validate(self, request: ImageTranslationRequest) -> list[str]:
        return self._client.validate(request)

    def translate(self, request: ImageTranslationRequest, on_progress=None):
        return self._client.translate(request, on_progress)

    def cancel(self) -> None:
        self._client.cancel()

    def close(self) -> None:
        self._client.close()
