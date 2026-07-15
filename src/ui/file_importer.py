#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件导入控制器模块
负责处理文件导入、EPUB解析、剪贴板导入等功能
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
from pathlib import Path
from typing import Callable


class FileImporter:
    """文件导入控制器"""

    def __init__(
        self,
        root: tk.Tk,
        config_manager,
        file_handler,
        epub_processor,
        table_loader: Callable,
        status_updater: Callable,
        image_translation_starter: Callable
    ):
        """
        初始化文件导入控制器

        Args:
            root: Tkinter根窗口
            config_manager: 配置管理器
            file_handler: 文件处理器
            epub_processor: EPUB处理器
            table_loader: 加载数据到表格的回调函数
            status_updater: 更新状态栏的回调函数
            image_translation_starter: 启动图片翻译的回调函数
        """
        self.root = root
        self.config_manager = config_manager
        self.file_handler = file_handler
        self.epub_processor = epub_processor
        self.table_loader = table_loader
        self.status_updater = status_updater
        self.image_translation_starter = image_translation_starter

        # 当前会话的原文/译文文件路径
        self.current_source_path = None
        self.current_target_path = None
        # EPUB映射相关
        self.current_mapping_dir = None
        self.current_mapping_keys = []

    def _safe_after(self, target_window, func):
        """R2-BUG-026：安全地在主线程调度回调，窗口已销毁时丢弃事件。"""
        try:
            if target_window is not None and target_window.winfo_exists():
                self.root.after(0, func)
        except Exception:
            pass

    def import_file(self, file_path=None):
        """导入文件并自动检测/创建对应译文文件，加载译文并定位到末尾"""
        try:
            if file_path is None:
                file_path = filedialog.askopenfilename(
                    title="选择要翻译的文件",
                    filetypes=[
                        ("所有文件", "*.*"),
                        ("文本文件", "*.txt"),
                        ("EPUB电子书", "*.epub"),
                    ]
                )

            if file_path:
                # 记录原文路径
                src_path = Path(file_path)
                self.current_source_path = src_path

                # 分支处理：EPUB走映射导入流程；文本文件走原有流程
                if src_path.suffix.lower() == ".epub":
                    # 创建解析提示弹窗并在新线程中执行解析
                    self._import_epub_with_progress(src_path)
                    return

                # 非EPUB：原有文本导入
                content = self.file_handler.read_file(str(src_path))
                source_lines = content.splitlines()

                # 派生译文路径：同目录、同名加"_译文.txt"
                tgt_path = src_path.with_name(f"{src_path.stem}_译文.txt")
                self.current_target_path = tgt_path

                # 检测并加载/创建译文文件
                target_lines = []
                if tgt_path.exists():
                    try:
                        tgt_content = self.file_handler.read_file(str(tgt_path))
                        target_lines = tgt_content.splitlines()
                    except Exception:
                        target_lines = []
                else:
                    # 不存在则创建空文件，确保后续实时保存有目标
                    try:
                        self.file_handler.write_file(str(tgt_path), "")
                    except Exception:
                        pass

                # 加载数据到表格
                self.table_loader(source_lines, target_lines)

                # 状态更新
                self.status_updater(f"已导入文件: {src_path.name}（译文文件：{tgt_path.name}）")

        except Exception as e:
            messagebox.showerror("导入错误", f"文件导入失败: {str(e)}")

    def _import_epub_with_progress(self, src_path):
        """在弹窗中执行EPUB解析，解析期间禁止操作界面"""
        # 创建模态对话框
        progress_dialog = tk.Toplevel(self.root)
        progress_dialog.title("正在解析EPUB")
        progress_dialog.transient(self.root)
        progress_dialog.grab_set()  # 模态窗口
        progress_dialog.resizable(False, False)

        # 计算居中位置
        window_width = 400
        window_height = 150
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        progress_dialog.geometry(f"{window_width}x{window_height}+{x}+{y}")

        # 创建界面元素
        container = ttk.Frame(progress_dialog, padding=20)
        container.pack(fill=tk.BOTH, expand=True)

        # 图标和提示文字
        icon_label = ttk.Label(container, text="📖", font=("微软雅黑", 32))
        icon_label.pack(pady=(0, 10))

        message_label = ttk.Label(container, text="正在解析EPUB文件，请稍候...",
                                 font=("微软雅黑", 11))
        message_label.pack(pady=(0, 15))

        # 进度条
        progress_bar = ttk.Progressbar(container, mode='indeterminate', length=300)
        progress_bar.pack(pady=(0, 10))
        progress_bar.start(10)  # 启动动画

        # 状态标签
        status_label = ttk.Label(container, text="正在提取文档结构...",
                               font=("微软雅黑", 9), foreground="#666")
        status_label.pack()

        # 禁用主窗口按钮
        self._disable_ui_controls()

        # 存储结果和错误信息
        result_container = {'success': False, 'error': None}

        def epub_import_worker():
            """完全重构：EPUB解析工作线程，简化为纯行号对齐"""
            try:
                # 更新状态
                self._safe_after(progress_dialog, lambda: status_label.config(text="正在解析EPUB结构..."))

                # 生成/刷新mapping
                mapping_info = self.epub_processor.import_epub(str(src_path))

                self._safe_after(progress_dialog, lambda: status_label.config(text="正在加载内容映射..."))

                # 存储映射目录
                self.current_mapping_dir = Path(mapping_info["mapping_dir"])

                # 加载原文和译文列表（完全重构：直接返回原文和译文）
                originals, translations = self.epub_processor.load_content_mapping(str(self.current_mapping_dir))

                self._safe_after(progress_dialog, lambda: status_label.config(text="正在填充原文区域..."))

                # 派生译文路径（EPUB模式下，为了避免权限问题，将译文txt保存到mapping目录）
                # 注意：这个txt只是辅助，真正的译文存储在content_mapping.json中
                # 兼容旧逻辑：如果有mapping目录，则保存在mapping目录中
                if self.current_mapping_dir:
                    tgt_path = self.current_mapping_dir / f"{src_path.stem}_译文.txt"
                else:
                    tgt_path = src_path.with_name(f"{src_path.stem}_译文.txt")

                self.current_target_path = tgt_path

                # 确保目标目录存在（对于mapping目录通常已存在，但做个保险）
                try:
                    tgt_path.parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass

                # 如果文件不存在，创建一个空的，避免保存时出错
                if not tgt_path.exists():
                    try:
                        with open(tgt_path, 'w', encoding='utf-8') as f:
                            f.write("")
                    except Exception:
                        pass

                # 在主线程中更新UI
                def update_ui():
                    # 加载数据到表格（严格按行号对齐）
                    self.table_loader(originals, translations)

                    # 显示映射目录名称（如果可以计算相对路径则显示相对路径，否则显示绝对路径）
                    try:
                        # 尝试相对于源文件目录
                        mapping_display = self.current_mapping_dir.relative_to(src_path.parent)
                    except ValueError:
                        try:
                            # 尝试相对于项目根目录
                            mapping_display = self.current_mapping_dir.relative_to(Path.cwd())
                        except ValueError:
                            # 都不行则显示绝对路径
                            mapping_display = self.current_mapping_dir

                    self.status_updater(f"已导入EPUB: {src_path.name}（映射目录：{mapping_display}）")

                self._safe_after(progress_dialog, update_ui)
                result_container['success'] = True

            except Exception as e:
                result_container['error'] = str(e)
            finally:
                # 关闭弹窗并恢复UI
                self._safe_after(progress_dialog, lambda: self._finish_epub_import(progress_dialog, result_container, src_path.name))

        # 在新线程中执行解析
        import_thread = threading.Thread(target=epub_import_worker, daemon=True)
        import_thread.start()

    def _disable_ui_controls(self):
        """禁用主界面控件"""
        try:
            # 禁用工具栏按钮（通过遍历所有子控件）
            for widget in self.root.winfo_children():
                self._disable_widget_recursive(widget)
        except Exception:
            pass

    def _enable_ui_controls(self):
        """启用主界面控件"""
        try:
            for widget in self.root.winfo_children():
                self._enable_widget_recursive(widget)
        except Exception:
            pass

    def _disable_widget_recursive(self, widget):
        """递归禁用控件（不禁用Text控件，避免影响数据填充）"""
        try:
            if isinstance(widget, (ttk.Button, tk.Button)):
                widget.config(state=tk.DISABLED)
            elif isinstance(widget, ttk.Combobox):
                widget.config(state=tk.DISABLED)
            # 注意：不禁用Text控件，以便后台线程能填充数据
            for child in widget.winfo_children():
                self._disable_widget_recursive(child)
        except Exception:
            pass

    def _enable_widget_recursive(self, widget):
        """递归启用控件"""
        try:
            if isinstance(widget, (ttk.Button, tk.Button)):
                # 根据翻译状态决定按钮是否启用
                widget.config(state=tk.NORMAL)
            elif isinstance(widget, tk.Text):
                widget.config(state=tk.NORMAL)
            elif isinstance(widget, ttk.Combobox):
                widget.config(state="readonly")
            for child in widget.winfo_children():
                self._enable_widget_recursive(child)
        except Exception:
            pass

    def _finish_epub_import(self, dialog, result, filename):
        """完成EPUB导入，关闭弹窗并显示结果"""
        try:
            dialog.destroy()
        except Exception:
            pass

        # 恢复UI控件
        self._enable_ui_controls()

        # 显示结果（仅在失败时显示错误）
        if not result['success']:
            messagebox.showerror("导入错误", f"EPUB映射导入失败：\n\n{result['error']}")
            return

        # EPUB导入成功后，检查是否有图片且启用了图片文字识别
        self._check_epub_images_for_text()

    def _check_epub_images_for_text(self):
        """EPUB 导入后询问是否使用默认图片翻译模块处理。

        接入 Manga 模块后：不再检查火山 Key，直接走默认 Manga 模块。
        """
        if not self.current_mapping_dir:
            return

        # 检查images.json是否有图片
        images_file = self.current_mapping_dir / "images.json"
        if not images_file.exists():
            return

        # PERF-004：迁移旧格式 Base64 图片到二进制资源文件
        from ..infrastructure.image_asset_store import migrate_legacy_images
        try:
            migrate_legacy_images(self.current_mapping_dir)
        except Exception as e:
            # 迁移失败不阻断流程，旧格式仍可回退使用
            pass

        import json
        try:
            images_data = json.loads(images_file.read_text(encoding="utf-8"))
            if not images_data.get("image_mappings"):
                return
        except Exception:
            return

        img_count = len(images_data["image_mappings"])

        # 弹出确认对话框：走默认 Manga 模块，不检查火山 Key
        do_translate = messagebox.askyesno(
            "图片翻译",
            f"检测到 EPUB 包含 {img_count} 张图片，"
            "是否使用默认图片翻译模块处理？\n\n"
            "（可稍后通过「项目 - 图片翻译」手动触发）"
        )

        if do_translate:
            self.image_translation_starter()

    def import_clipboard(self):
        """导入剪贴板内容"""
        try:
            clipboard_content = self.root.clipboard_get()
            if clipboard_content.strip():
                source_lines = clipboard_content.splitlines()
                self.table_loader(source_lines, [])
                self.status_updater("已导入剪贴板内容")
            else:
                messagebox.showwarning("导入警告", "剪贴板内容为空")

        except tk.TclError:
            messagebox.showerror("导入错误", "无法获取剪贴板内容")
