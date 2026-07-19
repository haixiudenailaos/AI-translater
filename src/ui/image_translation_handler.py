#!/usr/bin/env python3
"""
图片翻译处理器模块

接入 manga-image-translator 模块后的统一入口：
- 「本地模块翻译」按钮启动 Manga Provider。
- 「AI 图片翻译」按钮仅由用户显式选择并二次确认后调用火山图生图。
- 一次性选择 AI 不得改变后续默认值；下次点击主按钮仍运行 Manga Provider。
- Manga 失败时只显示失败原因、重试和设置入口，不显示自动切换 AI。
- 未配置火山引擎 Key 时不影响 Manga 默认模块。
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Callable

from ..domain.edition import EditionCapabilities, detect_edition_capabilities
from ..domain.errors import (
    ImageTranslationCancelled,
    ImageTranslationConfigError,
)
from ..domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
)
from ..domain.translation import OperationStatus
from ..utils.logger import get_logger
from .ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox

logger = get_logger(__name__)


class ImageTranslationHandler:
    """处理图片翻译相关功能的控制器"""

    _MANGA_AI_GUIDANCE = "本地 Manga 图片翻译暂时不可用，请先使用「AI 图片翻译」。"
    _MANGA_DISABLED_TEXT_MSG = (
        "当前为 Text Edition，未打包本地 Manga 推理依赖。\n\n"
        "如需使用本地模块图片翻译，请下载 Full Edition。"
    )

    def __init__(
        self,
        root: tk.Tk,
        config_manager,
        status_updater: Callable[[str], None],
        image_progress_updater: Callable[[str], None],
        get_mapping_dir: Callable,
        open_settings: Callable,
        busy_state_updater: Callable[[bool], None] | None = None,
        app_paths=None,
        font_path: str | None = None,
        edition_capabilities: EditionCapabilities | None = None,
    ):
        self.root = root
        self.config_manager = config_manager
        self.status_updater = status_updater
        self.image_progress_updater = image_progress_updater
        self.get_mapping_dir = get_mapping_dir
        self.open_settings = open_settings
        self.busy_state_updater = busy_state_updater or (lambda _busy: None)
        self._app_paths = app_paths
        self._font_path = font_path
        self._service = None  # 惰性创建 ImageTranslationService
        self._worker_thread: threading.Thread | None = None
        self._closed = False

        # P0-2：版本能力契约。Text 版本不导入 Manga 模块、不注册 Provider。
        # ``None`` 时从运行时检测获取（manga_translator 是否可导入）。
        self._edition_capabilities = (
            edition_capabilities
            if edition_capabilities is not None
            else detect_edition_capabilities()
        )

        # P1-1：UI 回调邮箱 + Tk 主线程事件泵
        # 工作线程只调用 ``_ui_mailbox.submit(func)``，不再直接调用 ``root.after()``。
        # 仅当 root 支持 Tk ``after`` 时启动 pump（测试替身可能不支持）。
        self._ui_mailbox = UICallbackMailbox()
        self._ui_pump: TkUICallbackPump | None = None
        if hasattr(root, "after"):
            self._ui_pump = TkUICallbackPump(root, self._ui_mailbox)
            self._ui_pump.start()

    # ── 版本能力 ──────────────────────────────────

    @property
    def edition_capabilities(self) -> EditionCapabilities:
        """当前版本能力契约（只读）。"""
        return self._edition_capabilities

    @property
    def manga_enabled(self) -> bool:
        """是否启用本地 Manga 图片翻译入口。"""
        return self._edition_capabilities.manga_enabled

    # ── Service 构建（惰性） ──────────────────────────────────

    def _get_service(self):
        """惰性创建并缓存 ImageTranslationService，按版本能力注册 Provider。"""
        if self._service is not None:
            return self._service

        from ..application.image_translation_service import ImageTranslationService
        from ..infrastructure.image_translation.registry import (
            ImageTranslationProviderRegistry,
        )

        registry = ImageTranslationProviderRegistry()

        # P0-2：仅在 Full 版本注册 Manga Provider。Text 版本禁止触碰 Manga 模块，
        # 防止运行时 ImportError（spec excludes 已移除 manga_translator）。
        if self._edition_capabilities.manga_enabled:
            manga_provider = self._create_manga_provider()
            if manga_provider is not None:
                registry.register(manga_provider)
        else:
            registry.manga_provider_available = False
            logger.info("Text Edition：跳过 Manga Provider 注册")

        # 注册火山 AI Provider（Text/Full 共用）
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
            resource_dir=(
                Path(self._app_paths.resource_dir) if self._app_paths is not None else None
            ),
            font_path=self._font_path,
            quality_preset=manga_cfg.get("quality_preset", "standard"),
            device=manga_cfg.get("device", "auto"),
            python_executable=manga_cfg.get("python_executable") or None,
        )

    # ── 本地模块入口 ──────────────────────────────────

    def start_image_translation(self):
        """运行本地 Manga 图片翻译模块。"""
        # P0-2：Text Edition 未打包 Manga 依赖，直接拒绝并提示。
        if not self._edition_capabilities.manga_enabled:
            messagebox.showwarning("图片翻译", self._MANGA_DISABLED_TEXT_MSG)
            return

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
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("图片翻译", "已有图片翻译任务正在运行，请等待完成")
            return

        # Manga 入口不检查火山 Key
        if (
            provider_id == ImageTranslationProviderId.AI_VOLCENGINE
            and not self.config_manager.get_volc_key()
        ):
            messagebox.showwarning("AI 图片翻译", "请先配置火山引擎 API Key")
            return

        # API 配置校验（Manga 的 external_llm 复用文本翻译 API 配置）
        if (
            provider_id == ImageTranslationProviderId.MANGA
            and not self.config_manager.is_api_configured()
        ):
            messagebox.showwarning(
                "配置警告",
                "请先配置文本翻译 API 设置（Manga 模块复用该配置翻译图片文字）",
            )
            self.open_settings()
            return

        label = (
            "Manga 图片翻译" if provider_id == ImageTranslationProviderId.MANGA else "AI 图片翻译"
        )
        self.status_updater(f"正在启动{label}...")
        self.image_progress_updater("翻译中...")
        getattr(self, "busy_state_updater", lambda _busy: None)(True)

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
            target_lang = self.config_manager.get_app_config().get("target_language", "中文")
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
                self._safe_after(lambda m=msg: self.status_updater(f"图片翻译前置校验失败: {m}"))
                self._safe_after(lambda: self.image_progress_updater("校验失败"))
                # Manga 失败只显示失败原因，不切换 AI
                self._safe_after(
                    lambda m=self._format_error_message(provider_id, "无法开始翻译：", msg): (
                        messagebox.showerror("图片翻译", m)
                    )
                )
                return

            def on_progress(progress: ImageTranslationProgress):
                self._safe_after(
                    lambda p=progress: self.status_updater(
                        f"图片处理: {p.current}/{p.total} - {p.stage}"
                    )
                )
                self._safe_after(
                    lambda p=progress: self.image_progress_updater(f"{p.current}/{p.total}")
                )

            result = service.translate(request, on_progress)

            label = (
                "Manga 图片翻译"
                if provider_id == ImageTranslationProviderId.MANGA
                else "AI 图片翻译"
            )

            if result.status == OperationStatus.CANCELLED:
                self._safe_after(lambda: self.status_updater(f"{label}已取消"))
                self._safe_after(lambda: self.image_progress_updater("已取消"))
            elif result.status == OperationStatus.FAILED:
                failed_msg = "; ".join(f"{k}: {v}" for k, v in result.failed_images.items())
                self._safe_after(lambda m=failed_msg: self.status_updater(f"{label}失败: {m}"))
                self._safe_after(lambda: self.image_progress_updater("失败"))
                self._safe_after(
                    lambda m=self._format_error_message(provider_id, "翻译失败：", failed_msg): (
                        messagebox.showerror("图片翻译", m)
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
                self._safe_after(lambda c=count: self.image_progress_updater(f"完成({c}张)"))
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
            self._safe_after(lambda m=str(exc): self.status_updater(f"图片翻译配置错误: {m}"))
            self._safe_after(lambda: self.image_progress_updater("出错"))
            self._safe_after(
                lambda m=self._format_error_message(provider_id, "配置错误：", str(exc)): (
                    messagebox.showerror("图片翻译", m)
                )
            )
        except Exception as exc:
            error_message = str(exc)
            logger.error(f"图片翻译出错: {error_message}", exc_info=True)
            self._safe_after(lambda msg=error_message: self.status_updater(f"图片翻译出错: {msg}"))
            self._safe_after(lambda: self.image_progress_updater("出错"))
            self._safe_after(
                lambda m=self._format_error_message(provider_id, "图片翻译出错：", error_message): (
                    messagebox.showerror("图片翻译", m)
                )
            )
        finally:
            # Provider 在多次任务间复用；应用关闭时再统一释放模型和客户端。
            self._worker_thread = None
            self._safe_after(lambda: getattr(self, "busy_state_updater", lambda _busy: None)(False))

    @classmethod
    def _format_error_message(
        cls,
        provider_id: ImageTranslationProviderId,
        heading: str,
        details: str,
    ) -> str:
        message = f"{heading}\n\n{details}"
        if provider_id == ImageTranslationProviderId.MANGA:
            return f"{message}\n\n{cls._MANGA_AI_GUIDANCE}"
        return f"{message}\n\n可在设置中检查配置后重试。"

    def _safe_after(self, func):
        """P1-1：通过 UI 回调邮箱提交回调，不在工作线程调用 Tk API。"""
        if getattr(self, "_closed", False):
            return
        self._ui_mailbox.submit(func)

    # ── 兼容入口：file_importer 调用，走 Manga ──────────────────────────────────

    def start_default_image_translation(self):
        """EPUB 导入后询问确认后调用的默认图片翻译入口（走 Manga）。

        供 file_importer.image_translation_starter 回调使用。
        """
        self.start_image_translation()

    def close(self) -> None:
        """取消当前任务并释放所有已初始化的图片翻译 Provider。"""
        self._closed = True
        # P1-1：关闭 UI 回调事件泵
        pump = getattr(self, "_ui_pump", None)
        self._ui_pump = None
        if pump is not None:
            pump.close()

        service = self._service
        self._service = None
        if service is None:
            return
        for provider_id in ImageTranslationProviderId:
            try:
                service.cancel(provider_id)
                service.close_provider(provider_id)
            except Exception:
                logger.warning("关闭图片翻译 Provider 失败: %s", provider_id)
