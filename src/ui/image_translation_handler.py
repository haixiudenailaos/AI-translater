#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片翻译处理器模块

接入 manga-image-translator 模块后的统一入口：
- 主按钮「图片翻译」直接启动 Manga 默认模块，不再先弹出选择对话框。
- 独立「AI 图片翻译...」入口，仅由用户显式选择并二次确认后才调用火山图生图。
- 一次性选择 AI 不得改变后续默认值；下次点击主按钮仍运行 Manga Provider。
- Manga 失败时只显示失败原因、重试和设置入口，不显示自动切换 AI。
- 未配置火山引擎 Key 时不影响 Manga 默认模块。
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Callable, Optional

from ..domain.errors import (
    ImageTranslationCancelled,
    ImageTranslationConfigError,
)
from ..domain.image_translation import (
    ImageTranslationProviderId,
    ImageTranslationProgress,
)
from ..domain.translation import OperationStatus
from ..utils.logger import get_logger

logger = get_logger(__name__)


class ImageTranslationHandler:
    """处理图片翻译相关功能的控制器"""

    def __init__(
        self,
        root: tk.Tk,
        config_manager,
        status_updater: Callable[[str], None],
        image_progress_updater: Callable[[str], None],
        get_mapping_dir: Callable,
        open_settings: Callable,
        app_paths=None,
        font_path: Optional[str] = None,
    ):
        self.root = root
        self.config_manager = config_manager
        self.status_updater = status_updater
        self.image_progress_updater = image_progress_updater
        self.get_mapping_dir = get_mapping_dir
        self.open_settings = open_settings
        self._app_paths = app_paths
        self._font_path = font_path
        self._service = None  # 惰性创建 ImageTranslationService
        self._worker_thread: Optional[threading.Thread] = None

    # ── Service 构建（惰性） ──────────────────────────────────

    def _get_service(self):
        """惰性创建并缓存 ImageTranslationService，注册 Manga 和 AI Provider。"""
        if self._service is not None:
            return self._service

        from ..application.image_translation_service import ImageTranslationService
        from ..infrastructure.image_translation.registry import (
            ImageTranslationProviderRegistry,
        )

        registry = ImageTranslationProviderRegistry()

        # 注册 Manga 默认 Provider（惰性导入，未安装引擎时 validate 会报错）
        manga_provider = self._create_manga_provider()
        if manga_provider is not None:
            registry.register(manga_provider)

        # 注册火山 AI Provider
        from ..infrastructure.image_translation.volcengine_provider import (
            VolcengineImageTranslationProvider,
        )
        registry.register(VolcengineImageTranslationProvider(self.config_manager))

        self._service = ImageTranslationService(self.config_manager, registry)
        return self._service

    def _create_manga_provider(self):
        """创建 Manga Provider，配置模型目录和质量预设。"""
        from ..infrastructure.image_translation.manga_provider import (
            MangaImageTranslationProvider,
        )

        img_config = self.config_manager.get_image_translation_config()
        manga_cfg = img_config.get("manga", {})

        # 模型目录：AppPaths 用户数据目录下的 models/manga
        model_dir = None
        if self._app_paths is not None:
            model_dir = Path(self._app_paths.data_dir) / "models" / "manga"
            model_dir.mkdir(parents=True, exist_ok=True)
        elif manga_cfg.get("model_dir"):
            model_dir = Path(manga_cfg["model_dir"])

        return MangaImageTranslationProvider(
            self.config_manager,
            model_dir=model_dir,
            font_path=self._font_path,
            quality_preset=manga_cfg.get("quality_preset", "standard"),
            device=manga_cfg.get("device", "auto"),
        )

    # ── 主入口：直接启动 Manga ──────────────────────────────────

    def start_image_translation(self):
        """主按钮入口：直接运行 Manga 默认模块，不再弹出选择对话框。"""
        current_mapping_dir = self.get_mapping_dir()
        if not current_mapping_dir:
            messagebox.showwarning("图片翻译", "请先导入EPUB文件")
            return

        # 检查 images.json 是否存在且有图片
        images_file = current_mapping_dir / "images.json"
        if not images_file.exists():
            messagebox.showwarning("图片翻译", "当前EPUB没有图片数据")
            return

        # 迁移旧格式 Base64 图片到二进制资源文件
        from ..infrastructure.image_asset_store import migrate_legacy_images
        try:
            migrate_legacy_images(current_mapping_dir)
        except Exception:
            pass

        import json
        try:
            images_data = json.loads(images_file.read_text(encoding="utf-8"))
        except Exception:
            messagebox.showwarning("图片翻译", "images.json 解析失败")
            return
        if not images_data.get("image_mappings"):
            messagebox.showwarning("图片翻译", "当前EPUB没有图片")
            return

        # 直接启动 Manga（不检查火山 Key）
        self._start_translation(ImageTranslationProviderId.MANGA)

    # ── AI 显式入口 ──────────────────────────────────

    def start_ai_image_translation(self):
        """独立 AI 图片翻译入口：用户显式选择并二次确认后才调用火山图生图。"""
        current_mapping_dir = self.get_mapping_dir()
        if not current_mapping_dir:
            messagebox.showwarning("AI 图片翻译", "请先导入EPUB文件")
            return

        # 校验火山 Key
        if not self.config_manager.get_volc_key():
            messagebox.showwarning(
                "AI 图片翻译",
                "未配置火山引擎 API Key，请在设置中配置后再使用 AI 图片翻译。",
            )
            self.open_settings()
            return

        # 二次确认：费用提示
        confirmed = messagebox.askyesno(
            "AI 图片翻译确认",
            "AI 图片翻译将调用火山图生图服务并可能产生 API 费用，\n"
            "且会对图片进行生成式修改。\n\n"
            "确认继续吗？\n\n"
            "（本次选择不会改变默认图片翻译模块，下次点击主按钮仍运行 Manga）",
            icon="warning",
        )
        if not confirmed:
            return

        # 一次性选择 AI 不写回 default_provider
        self._start_translation(ImageTranslationProviderId.AI_VOLCENGINE)

    # ── 通用翻译启动 ──────────────────────────────────

    def _start_translation(self, provider_id: ImageTranslationProviderId):
        """启动指定 Provider 的图片翻译工作线程。"""
        # Manga 入口不检查火山 Key
        if provider_id == ImageTranslationProviderId.AI_VOLCENGINE:
            if not self.config_manager.get_volc_key():
                messagebox.showwarning("AI 图片翻译", "请先配置火山引擎 API Key")
                return

        # API 配置校验（Manga 的 external_llm 复用文本翻译 API 配置）
        if provider_id == ImageTranslationProviderId.MANGA:
            if not self.config_manager.is_api_configured():
                messagebox.showwarning(
                    "配置警告",
                    "请先配置文本翻译 API 设置（Manga 模块复用该配置翻译图片文字）",
                )
                self.open_settings()
                return

        label = "Manga 图片翻译" if provider_id == ImageTranslationProviderId.MANGA else "AI 图片翻译"
        self.status_updater(f"正在启动{label}...")
        self.image_progress_updater("翻译中...")

        self._worker_thread = threading.Thread(
            target=self._translation_worker,
            args=(provider_id,),
            daemon=True,
        )
        self._worker_thread.start()

    def _translation_worker(self, provider_id: ImageTranslationProviderId):
        """图片翻译工作线程，通过 ImageTranslationService 调用 Provider。"""
        service = None
        try:
            service = self._get_service()
            target_lang = self.config_manager.get_app_config().get(
                "target_language", "中文"
            )
            current_mapping_dir = self.get_mapping_dir()

            request = service.make_request(
                mapping_dir=current_mapping_dir,
                target_language=target_lang,
                provider_id=provider_id,
            )

            # 执行前校验
            errors = service.validate_request(request)
            if errors:
                msg = "\n".join(errors)
                self._safe_after(
                    lambda m=msg: self.status_updater(f"图片翻译前置校验失败: {m}")
                )
                self._safe_after(lambda: self.image_progress_updater("校验失败"))
                # Manga 失败只显示失败原因，不切换 AI
                self._safe_after(
                    lambda m=msg: messagebox.showerror("图片翻译", f"无法开始翻译：\n\n{m}")
                )
                return

            def on_progress(progress: ImageTranslationProgress):
                self._safe_after(
                    lambda p=progress: self.status_updater(
                        f"图片处理: {p.current}/{p.total} - {p.stage}"
                    )
                )
                self._safe_after(
                    lambda p=progress: self.image_progress_updater(
                        f"{p.current}/{p.total}"
                    )
                )

            result = service.translate(request, on_progress)

            label = (
                "Manga 图片翻译" if provider_id == ImageTranslationProviderId.MANGA
                else "AI 图片翻译"
            )

            if result.status == OperationStatus.CANCELLED:
                self._safe_after(lambda: self.status_updater(f"{label}已取消"))
                self._safe_after(lambda: self.image_progress_updater("已取消"))
            elif result.status == OperationStatus.FAILED:
                failed_msg = "; ".join(
                    f"{k}: {v}" for k, v in result.failed_images.items()
                )
                self._safe_after(
                    lambda m=failed_msg: self.status_updater(f"{label}失败: {m}")
                )
                self._safe_after(lambda: self.image_progress_updater("失败"))
                self._safe_after(
                    lambda m=failed_msg: messagebox.showerror(
                        "图片翻译",
                        f"翻译失败：\n\n{m}\n\n"
                        "可在设置中检查配置后重试。",
                    )
                )
            else:
                count = result.succeeded_count
                skipped = len(result.skipped_images)
                self._safe_after(
                    lambda c=count: self.status_updater(
                        f"{label}完成: {c} 张已翻译"
                        + (f"，{skipped} 张无需翻译" if skipped else "")
                    )
                )
                self._safe_after(
                    lambda c=count: self.image_progress_updater(f"完成({c}张)")
                )
                self._safe_after(
                    lambda c=count, s=skipped: messagebox.showinfo(
                        "图片翻译",
                        f"已完成 {c} 张图片的翻译。"
                        + (f"\n{s} 张图片无需翻译。" if skipped else "")
                        + "\n导出EPUB时将自动使用翻译后的图片。",
                    )
                )

        except ImageTranslationCancelled:
            self._safe_after(lambda: self.status_updater("图片翻译已取消"))
            self._safe_after(lambda: self.image_progress_updater("已取消"))
        except ImageTranslationConfigError as exc:
            logger.error("图片翻译配置错误: %s", exc)
            self._safe_after(
                lambda m=str(exc): self.status_updater(f"图片翻译配置错误: {m}")
            )
            self._safe_after(lambda: self.image_progress_updater("出错"))
            self._safe_after(
                lambda m=str(exc): messagebox.showerror("图片翻译", f"配置错误：\n\n{m}")
            )
        except Exception as exc:
            error_message = str(exc)
            logger.error(f"图片翻译出错: {error_message}", exc_info=True)
            self._safe_after(
                lambda msg=error_message: self.status_updater(f"图片翻译出错: {msg}")
            )
            self._safe_after(lambda: self.image_progress_updater("出错"))
        finally:
            # 关闭 Provider 生命周期，释放资源
            if service is not None:
                try:
                    service.close_provider(provider_id)
                except Exception:
                    pass

    def _safe_after(self, func):
        """安全地在主线程调度回调。"""
        try:
            self.root.after(0, func)
        except Exception:
            pass

    # ── 兼容入口：file_importer 调用，走 Manga ──────────────────────────────────

    def start_default_image_translation(self):
        """EPUB 导入后询问确认后调用的默认图片翻译入口（走 Manga）。

        供 file_importer.image_translation_starter 回调使用。
        """
        self.start_image_translation()
