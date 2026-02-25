#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片翻译处理器模块
从MainWindow中提取的图片翻译逻辑
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
from typing import Callable, Optional
from pathlib import Path

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
        open_settings: Callable
    ):
        """
        初始化图片翻译处理器

        Args:
            root: Tkinter根窗口
            config_manager: 配置管理器
            status_updater: 状态栏更新回调函数
            image_progress_updater: 图片进度标签更新回调函数
            get_mapping_dir: 获取当前映射目录的回调函数
            open_settings: 打开设置窗口的回调函数
        """
        self.root = root
        self.config_manager = config_manager
        self.status_updater = status_updater
        self.image_progress_updater = image_progress_updater
        self.get_mapping_dir = get_mapping_dir
        self.open_settings = open_settings

    def start_image_translation(self):
        """独立的图片翻译入口：弹出选择对话框"""
        current_mapping_dir = self.get_mapping_dir()
        if not current_mapping_dir:
            messagebox.showwarning("图片翻译", "请先导入EPUB文件")
            return

        # 检查images.json是否存在且有图片
        images_file = current_mapping_dir / "images.json"
        if not images_file.exists():
            messagebox.showwarning("图片翻译", "当前EPUB没有图片数据")
            return

        import json
        images_data = json.loads(images_file.read_text(encoding="utf-8"))
        if not images_data.get("image_mappings"):
            messagebox.showwarning("图片翻译", "当前EPUB没有图片")
            return

        # 弹出选择对话框
        choice_dialog = tk.Toplevel(self.root)
        choice_dialog.title("选择图片翻译方式")
        choice_dialog.transient(self.root)
        choice_dialog.grab_set()
        choice_dialog.resizable(False, False)

        w, h = 380, 240
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        choice_dialog.geometry(f"{w}x{h}+{x}+{y}")

        container = ttk.Frame(choice_dialog, padding=20)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(container, text="请选择图片翻译方式：",
                 font=("微软雅黑", 11)).pack(pady=(0, 15))

        def _check_image_gen_api_key():
            """检查插图翻译API Key是否已配置"""
            if not self.config_manager.get_volc_key():
                messagebox.showwarning("图片翻译", "请先在设置中配置火山引擎API Key")
                return False
            return True

        def on_smart_translate():
            choice_dialog.destroy()
            self._start_image_text_translation()

        def on_img2img_all():
            choice_dialog.destroy()
            if not _check_image_gen_api_key():
                return
            img_thread = threading.Thread(target=self._image_translation_worker, daemon=True)
            img_thread.start()

        app_config = self.config_manager.get_app_config()

        ttk.Button(container, text="🔍 智能检测+插图翻译（推荐）",
                  command=on_smart_translate).pack(fill=tk.X, pady=(0, 4))
        ttk.Label(container, text="  先用视觉模型检测含外文的图片，再插图翻译",
                 font=("微软雅黑", 8), foreground="gray").pack(anchor=tk.W, pady=(0, 8))
        ttk.Button(container, text="🎨 全部插图翻译（火山引擎）",
                  command=on_img2img_all).pack(fill=tk.X, pady=(0, 4))
        ttk.Label(container, text="  对所有图片进行插图翻译（消耗较多API额度）",
                 font=("微软雅黑", 8), foreground="gray").pack(anchor=tk.W, pady=(0, 8))

        ttk.Button(container, text="取消", command=choice_dialog.destroy).pack(pady=(10, 0))

    def _start_image_text_translation(self):
        """启动LLM图片文字识别翻译"""
        app_config = self.config_manager.get_app_config()
        if not app_config.get("image_text_translation_enabled", True):
            messagebox.showinfo("图片翻译", "图片文字识别翻译已在设置中禁用")
            return

        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置（视觉模型）")
            self.open_settings()
            return

        app_config = self.config_manager.get_app_config()
        if not self.config_manager.get_volc_key():
            messagebox.showwarning("配置警告", "请先在设置中配置火山引擎API Key（插图翻译需要）")
            return

        self.status_updater("正在启动智能图片翻译（检测+插图翻译）...")
        self.image_progress_updater("翻译中...")
        worker_thread = threading.Thread(
            target=self._image_text_translation_worker, daemon=True
        )
        worker_thread.start()

    def _image_text_translation_worker(self):
        """图片文字检测+插图翻译工作线程

        流程：视觉模型检测非中文文字 -> 插图翻译模型翻译 -> 保存结果
        """
        from ..core.image_text_translator import ImageTextTranslator

        try:
            translator = ImageTextTranslator(self.config_manager)
            target_lang = self.config_manager.get_app_config().get("target_language", "中文")

            def progress_cb(current, total, name):
                self.root.after(0, lambda: self.status_updater(
                    f"图片处理: {current}/{total} - {name}"
                ))
                self.root.after(0, lambda c=current, t=total: self.image_progress_updater(f"{c}/{t}"))

            current_mapping_dir = self.get_mapping_dir()
            result_map = translator.process_all_images(
                str(current_mapping_dir), target_lang, progress_cb
            )

            count = len(result_map)
            self.root.after(0, lambda: self.status_updater(
                f"图片翻译完成: {count} 张图片已生成翻译图片"
            ))
            self.root.after(0, lambda: self.image_progress_updater(f"完成({count}张)"))
            if count > 0:
                self.root.after(0, lambda: messagebox.showinfo(
                    "图片翻译",
                    f"已完成 {count} 张图片的插图翻译。\n导出EPUB时将自动使用翻译后的图片。"
                ))
            else:
                self.root.after(0, lambda: messagebox.showinfo(
                    "图片翻译",
                    "未检测到包含非中文文字的图片，或插图翻译未成功。"
                ))

        except Exception as e:
            logger.error(f"图片翻译出错: {e}", exc_info=True)
            self.root.after(0, lambda: self.status_updater(f"图片翻译出错: {str(e)}"))
            self.root.after(0, lambda: self.image_progress_updater("出错"))

    def _image_translation_worker(self):
        """插图翻译工作线程"""
        from ..core.image_translator import ImageTranslator
        import asyncio

        try:
            # 更新状态
            self.root.after(0, lambda: self.status_updater("正在启动插图翻译..."))
            self.root.after(0, lambda: self.image_progress_updater("翻译中..."))

            current_mapping_dir = self.get_mapping_dir()
            mapping_dir = str(current_mapping_dir)
            target_lang = self.config_manager.get_app_config().get("target_language", "中文")

            async def run_translation():
                translator = ImageTranslator(self.config_manager)

                def progress_cb(success, total, current):
                    self.root.after(0, lambda: self.image_progress_updater(f"{success}/{total}"))
                    self.root.after(0, lambda: self.status_updater(f"插图翻译进度: {success}/{total}"))

                return await translator.translate_images(mapping_dir, target_lang, progress_cb)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result_map = loop.run_until_complete(run_translation())
            loop.close()

            # 保存结果到 image_translation_result.json
            if result_map:
                result_file = current_mapping_dir / "image_translation_result.json"
                import json
                with open(result_file, "w", encoding="utf-8") as f:
                    json.dump(result_map, f, ensure_ascii=False, indent=2)

                count = len(result_map)
                self.root.after(0, lambda: self.status_updater(f"插图翻译完成: {count} 张图片"))
                self.root.after(0, lambda: self.image_progress_updater(f"完成({count}张)"))
            else:
                self.root.after(0, lambda: self.status_updater("插图翻译完成: 无图片处理"))
                self.root.after(0, lambda: self.image_progress_updater("完成(0张)"))

        except Exception as e:
            logger.error(f"插图翻译出错: {e}", exc_info=True)
            self.root.after(0, lambda: self.status_updater(f"插图翻译出错: {str(e)}"))
            self.root.after(0, lambda: self.image_progress_updater("出错"))
