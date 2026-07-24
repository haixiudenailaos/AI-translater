#!/usr/bin/env python3
"""V1.5-style AI image translation workflow.

The primary flow uses the configured vision model to find images containing
foreign text, then sends only those images to Volcengine image-to-image
translation. Users can also explicitly translate every image.
"""

from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from ..domain.errors import ImageTranslationCancelled
from ..infrastructure.image_asset_store import migrate_legacy_images
from ..infrastructure.mapping_repository import resolve_mapping_file
from ..utils.logger import get_logger
from .ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox

logger = get_logger(__name__)

_CLOSE_TIMEOUT_SECONDS = 5.0
_MODE_DIALOG_MIN_WIDTH = 460
_MODE_DIALOG_MIN_HEIGHT = 320
_MODE_DIALOG_SCREEN_MARGIN = 32
_MODE_DIALOG_DESCRIPTION_WIDTH = 412


class ImageTranslationHandler:
    """Coordinate the V1.5 vision-detection and image-generation flow."""

    def __init__(
        self,
        root: tk.Tk,
        config_manager,
        status_updater: Callable[[str], None],
        image_progress_updater: Callable[[str], None],
        get_mapping_dir: Callable[[], Path | None],
        open_settings: Callable[[], None],
        busy_state_updater: Callable[[bool], None] | None = None,
        **_legacy_options,
    ) -> None:
        self.root = root
        self.config_manager = config_manager
        self.status_updater = status_updater
        self.image_progress_updater = image_progress_updater
        self.get_mapping_dir = get_mapping_dir
        self.open_settings = open_settings
        self.busy_state_updater = busy_state_updater or (lambda _busy: None)

        self._worker_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._active_text_translator = None
        self._active_image_translator = None
        self._closed = False

        self._ui_mailbox = UICallbackMailbox()
        self._ui_pump = TkUICallbackPump(root, self._ui_mailbox)
        self._ui_pump.start()

    def start_image_translation(self) -> None:
        """Open the V1.5 image translation mode chooser."""
        mapping_dir = self._validate_mapping_dir()
        if mapping_dir is None:
            return
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("图片翻译", "已有图片翻译任务正在运行，请等待完成")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("选择图片翻译方式")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        container = ttk.Frame(dialog, padding=20)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(container, text="请选择图片翻译方式：", font=("微软雅黑", 11)).pack(
            anchor=tk.W, pady=(0, 15)
        )

        def start_smart() -> None:
            dialog.destroy()
            self._start_image_text_translation(mapping_dir)

        def start_all() -> None:
            dialog.destroy()
            self._start_all_image_translation(mapping_dir)

        ttk.Button(container, text="OCR 预筛选 + 插图翻译（推荐）", command=start_smart).pack(
            fill=tk.X, pady=(0, 4)
        )
        ttk.Label(
            container,
            text="先用 OCR 排除无文字图片，再将命中图片交给火山图生图",
            foreground="gray",
            justify=tk.LEFT,
            wraplength=_MODE_DIALOG_DESCRIPTION_WIDTH,
        ).pack(anchor=tk.W, fill=tk.X, pady=(0, 10))
        ttk.Button(container, text="全部插图翻译（火山引擎）", command=start_all).pack(
            fill=tk.X, pady=(0, 4)
        )
        ttk.Label(
            container,
            text="跳过文字检测并处理所有图片，会消耗更多 API 额度",
            foreground="gray",
            justify=tk.LEFT,
            wraplength=_MODE_DIALOG_DESCRIPTION_WIDTH,
        ).pack(anchor=tk.W, fill=tk.X)

        action_bar = ttk.Frame(container)
        action_bar.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(action_bar, text="取消", command=dialog.destroy, width=12).pack()

        self._size_and_center_mode_dialog(dialog)

    def _size_and_center_mode_dialog(self, dialog: tk.Toplevel) -> None:
        """使方式选择窗口容纳完整说明与操作按钮。"""
        dialog.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        max_width = max(1, screen_width - 2 * _MODE_DIALOG_SCREEN_MARGIN)
        max_height = max(1, screen_height - 2 * _MODE_DIALOG_SCREEN_MARGIN)
        width = min(max_width, max(_MODE_DIALOG_MIN_WIDTH, dialog.winfo_reqwidth()))
        height = min(max_height, max(_MODE_DIALOG_MIN_HEIGHT, dialog.winfo_reqheight()))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

    def start_default_image_translation(self) -> None:
        """Run smart detection after the EPUB import confirmation."""
        mapping_dir = self._validate_mapping_dir()
        if mapping_dir is not None:
            self._start_image_text_translation(mapping_dir)

    def start_ai_image_translation(self) -> None:
        """Compatibility alias for callers from the former dual-provider UI."""
        self.start_image_translation()

    def _validate_mapping_dir(self) -> Path | None:
        mapping_dir = self.get_mapping_dir()
        if not mapping_dir:
            messagebox.showwarning("图片翻译", "请先导入 EPUB 文件")
            return None
        mapping_dir = Path(mapping_dir)

        try:
            migrate_legacy_images(mapping_dir)
        except Exception as exc:
            logger.warning("迁移旧图片资源失败: %s", exc)

        images_file = resolve_mapping_file(mapping_dir, "images.json")
        if not images_file.exists():
            messagebox.showwarning("图片翻译", "当前 EPUB 没有图片数据")
            return None
        try:
            images_data = json.loads(images_file.read_text(encoding="utf-8"))
        except Exception:
            messagebox.showwarning("图片翻译", "images.json 解析失败")
            return None
        if not images_data.get("image_mappings"):
            messagebox.showwarning("图片翻译", "当前 EPUB 没有图片")
            return None
        return mapping_dir

    def _start_image_text_translation(self, mapping_dir: Path | None = None) -> None:
        if not self.config_manager.get_volc_key():
            messagebox.showwarning("配置警告", "请先配置火山引擎 API Key")
            self.open_settings()
            return
        mapping_dir = mapping_dir or self._validate_mapping_dir()
        if mapping_dir is None:
            return

        if not self._is_ocr_configured():
            confirmed = messagebox.askyesno(
                "OCR 未配置",
                "当前没有可用的 OCR 预筛选服务。\n\n"
                "继续后，包括无文字图片在内的所有图片都会进入 AI 图生图，"
                "API 调用次数和花销会更高。\n\n"
                "是否仍要继续？",
                parent=self.root,
            )
            if not confirmed:
                return
            self._start_worker("all", Path(mapping_dir))
            return

        self._start_worker("smart", Path(mapping_dir))

    def _is_ocr_configured(self) -> bool:
        checker = getattr(self.config_manager, "is_image_ocr_configured", None)
        if callable(checker):
            return bool(checker())
        return bool(self.config_manager.is_api_configured())

    def _start_all_image_translation(self, mapping_dir: Path | None = None) -> None:
        if not self.config_manager.get_volc_key():
            messagebox.showwarning("配置警告", "请先配置火山引擎 API Key")
            self.open_settings()
            return
        mapping_dir = mapping_dir or self._validate_mapping_dir()
        if mapping_dir is not None:
            self._start_worker("all", Path(mapping_dir))

    def _start_worker(self, mode: str, mapping_dir: Path) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("图片翻译", "已有图片翻译任务正在运行，请等待完成")
            return
        self._cancel_event.clear()
        self.status_updater(
            "正在启动智能图片翻译..." if mode == "smart" else "正在启动全部插图翻译..."
        )
        self.image_progress_updater("翻译中...")
        self.busy_state_updater(True)
        self._worker_thread = threading.Thread(
            target=self._translation_worker,
            args=(mode, mapping_dir),
            name=f"image-translation-{mode}",
            daemon=True,
        )
        self._worker_thread.start()

    def _translation_worker(self, mode: str, mapping_dir: Path) -> None:
        try:
            target_lang = self.config_manager.get_app_config().get("target_language", "中文")

            def progress_callback(current: int, total: int, name: str) -> None:
                self._safe_after(
                    lambda c=current, t=total, n=name: self.status_updater(
                        f"图片处理: {c}/{t} - {n}"
                    )
                )
                self._safe_after(lambda c=current, t=total: self.image_progress_updater(f"{c}/{t}"))

            if mode == "smart":
                from ..core.image_text_translator import ImageTextTranslator

                translator = ImageTextTranslator(self.config_manager)
                self._active_text_translator = translator
                result_map = translator.process_all_images(
                    str(mapping_dir),
                    target_lang,
                    progress_callback,
                    cancel_event=self._cancel_event,
                )
                summary = translator.last_summary
            else:
                from ..core.image_text_translator import ImageTextTranslator
                from ..core.image_translator import ImageTranslator

                translator = ImageTranslator(self.config_manager)
                self._active_image_translator = translator

                def all_progress(success: int, total: int, current: str) -> None:
                    progress_callback(success, total, f"[插图翻译] {current}")

                result_map = translator.translate_images(
                    str(mapping_dir),
                    target_lang,
                    all_progress,
                    cancel_event=self._cancel_event,
                )
                ImageTextTranslator.write_image_translation_result(mapping_dir, result_map)
                summary = {
                    "total": self._image_count(mapping_dir),
                    "detected": self._image_count(mapping_dir),
                    "translated": len(result_map),
                    "skipped": 0,
                    "failed": 0,
                }

            count = len(result_map)
            self._safe_after(
                lambda c=count: self.status_updater(f"图片翻译完成: {c} 张图片已生成译图")
            )
            self._safe_after(lambda c=count: self.image_progress_updater(f"完成({c}张)"))

            if count:
                details = f"已完成 {count} 张图片的翻译。\n导出 EPUB 时将自动使用译图。"
            elif summary.get("failed", 0):
                details = (
                    "没有生成译图。"
                    f"\n视觉检测失败 {summary['failed']} 张，请检查视觉模型配置后重试。"
                )
            else:
                details = "未检测到包含外文文字的图片，无需生成译图。"
            self._safe_after(lambda text=details: messagebox.showinfo("图片翻译", text))
        except ImageTranslationCancelled:
            self._safe_after(lambda: self.status_updater("图片翻译已取消"))
            self._safe_after(lambda: self.image_progress_updater("已取消"))
        except Exception as exc:
            error_message = str(exc)
            logger.error("图片翻译出错: %s", error_message, exc_info=True)
            self._safe_after(lambda msg=error_message: self.status_updater(f"图片翻译出错: {msg}"))
            self._safe_after(lambda: self.image_progress_updater("出错"))
            self._safe_after(
                lambda msg=error_message: messagebox.showerror(
                    "图片翻译", f"图片翻译失败：\n\n{msg}\n\n请检查视觉模型和火山引擎配置。"
                )
            )
        finally:
            for translator in (self._active_text_translator, self._active_image_translator):
                if translator is not None:
                    try:
                        translator.close()
                    except Exception:
                        pass
            self._active_text_translator = None
            self._active_image_translator = None
            self._worker_thread = None
            self._safe_after(lambda: self.busy_state_updater(False))

    @staticmethod
    def _image_count(mapping_dir: Path) -> int:
        try:
            payload = json.loads(
                resolve_mapping_file(mapping_dir, "images.json").read_text(encoding="utf-8")
            )
            return len(payload.get("image_mappings", {}))
        except Exception:
            return 0

    def cancel(self) -> None:
        """Cancel the current detection/generation run."""
        self._cancel_event.set()

    def _safe_after(self, callback: Callable[[], object]) -> None:
        if not self._closed:

            def run_callback() -> None:
                callback()

            self._ui_mailbox.submit(run_callback)

    def close(self, *, timeout_seconds: float = _CLOSE_TIMEOUT_SECONDS) -> None:
        """Cancel work and release HTTP clients within one deadline."""
        if self._closed:
            return
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        self._closed = True
        self._cancel_event.set()

        worker = self._worker_thread
        if worker is not None and worker is not threading.current_thread() and worker.is_alive():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))

        if worker is None or not worker.is_alive():
            for translator in (self._active_text_translator, self._active_image_translator):
                if translator is not None:
                    try:
                        translator.close()
                    except Exception:
                        pass

        pump = self._ui_pump
        self._ui_pump = None
        if pump is not None:
            pump.close()
