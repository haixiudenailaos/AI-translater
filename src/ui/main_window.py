#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主窗口界面模块
UI 骨架：只负责布局和组件创建，业务逻辑委托给各 controller。
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
from pathlib import Path

from .settings_window import SettingsWindow
from .glossary_window import GlossaryWindow
from .concurrent_window import ConcurrentWindow
from .table_editor import TableCellEditor
from .translation_controller import TranslationController
from .file_importer import FileImporter
from .image_translation_handler import ImageTranslationHandler
from ..core.translator import TranslatorEngine
from ..utils.file_handler import FileHandler
from ..core.epub_processor import EPUBProcessor


class MainWindow:
    def __init__(self, root, config_manager):
        self.root = root
        self.config_manager = config_manager
        self.translator = TranslatorEngine(config_manager)
        self.file_handler = FileHandler()
        self.epub_processor = EPUBProcessor()

        # 界面变量
        self.translation_mode = tk.StringVar(value="快速模式")

        self.setup_ui()

        # ── 初始化控制器 ────────────────────────────
        self.file_importer = FileImporter(
            root=self.root,
            config_manager=self.config_manager,
            file_handler=self.file_handler,
            epub_processor=self.epub_processor,
            table_loader=self.load_data_to_table,
            status_updater=self.update_status,
            image_translation_starter=None,  # 延迟绑定，见下方
        )

        self.image_handler = ImageTranslationHandler(
            root=self.root,
            config_manager=self.config_manager,
            status_updater=self.update_status,
            image_progress_updater=self.update_image_progress,
            get_mapping_dir=lambda: self.file_importer.current_mapping_dir,
            open_settings=self.open_settings,
        )

        # 延迟绑定：file_importer 需要 image_handler
        self.file_importer.image_translation_starter = lambda: self.image_handler._start_image_text_translation()

        self.translation_controller = TranslationController(
            root=self.root,
            config_manager=self.config_manager,
            translator=self.translator,
            file_handler=self.file_handler,
            epub_processor=self.epub_processor,
            translation_table=self.translation_table,
            translation_mode=self.translation_mode,
            progress_var=self.progress_var,
            progress_bar=self.progress_bar,
            translate_btn=self.translate_btn,
            continue_btn=self.continue_btn,
            stop_btn=self.stop_btn,
            status_updater=self.update_status,
            get_table_data=self.get_table_data,
            schedule_save=self._schedule_save_to_target,
            open_settings=self.open_settings,
            get_source_path=lambda: self.file_importer.current_source_path,
            get_mapping_dir=lambda: self.file_importer.current_mapping_dir,
        )

        self.setup_bindings()
        self._on_settings_updated()

    # ── UI 布局 ─────────────────────────────────────────

    def setup_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.create_toolbar(main_frame)
        self.create_work_area(main_frame)
        self.create_control_panel(main_frame)
        self.create_status_bar(main_frame)

    def create_toolbar(self, parent):
        toolbar_frame = ttk.Frame(parent)
        toolbar_frame.pack(fill=tk.X, pady=(0, 10))

        left_frame = ttk.Frame(toolbar_frame)
        left_frame.pack(side=tk.LEFT)
        ttk.Button(left_frame, text="📁 文件导入",
                   command=lambda: self.file_importer.import_file()).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(left_frame, text="📋 剪贴板导入",
                   command=lambda: self.file_importer.import_clipboard()).pack(side=tk.LEFT, padx=(0, 5))

        middle_frame = ttk.Frame(toolbar_frame)
        middle_frame.pack(side=tk.LEFT, expand=True)
        ttk.Label(middle_frame, text="翻译模式:").pack(side=tk.LEFT, padx=(20, 5))
        ttk.Combobox(middle_frame, textvariable=self.translation_mode,
                     values=["逐行模式", "快速模式"], state="readonly", width=10).pack(side=tk.LEFT)

        right_frame = ttk.Frame(toolbar_frame)
        right_frame.pack(side=tk.RIGHT)
        ttk.Button(right_frame, text="📋 并发翻译",
                   command=self.open_concurrent).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_frame, text="📚 术语库",
                   command=self.open_glossary).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_frame, text="⚙️ 设置",
                   command=self.open_settings).pack(side=tk.RIGHT, padx=(5, 0))

    def create_work_area(self, parent):
        work_frame = ttk.LabelFrame(parent, text="翻译内容对照表", padding=5)
        work_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        table_container = ttk.Frame(work_frame)
        table_container.pack(fill=tk.BOTH, expand=True)

        columns = ('line_number', 'source_text', 'target_text')
        self.translation_table = ttk.Treeview(
            table_container, columns=columns, show='headings', selectmode='extended')
        self.translation_table.heading('line_number', text='行号')
        self.translation_table.heading('source_text', text='原文')
        self.translation_table.heading('target_text', text='译文')
        self.translation_table.column('line_number', width=60, minwidth=50, anchor='center', stretch=False)
        self.translation_table.column('source_text', width=400, minwidth=200, anchor='w')
        self.translation_table.column('target_text', width=400, minwidth=200, anchor='w')

        vsb = ttk.Scrollbar(table_container, orient="vertical", command=self.translation_table.yview)
        hsb = ttk.Scrollbar(table_container, orient="horizontal", command=self.translation_table.xview)
        self.translation_table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.translation_table.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        table_container.grid_rowconfigure(0, weight=1)
        table_container.grid_columnconfigure(0, weight=1)

        # 单元格编辑
        self._cell_editor = TableCellEditor(
            self.translation_table, editable_columns={1, 2},
            on_save=self._on_cell_edited)
        self.translation_table.bind('<Double-Button-1>', self._cell_editor.on_double_click)

        # 右键菜单（由 TranslationController 管理）
        self.translation_table.bind('<Button-3>', lambda e: self.translation_controller.show_context_menu(e))

        self.setup_table_styles()

    def setup_table_styles(self):
        style = ttk.Style()
        style.configure("Treeview", font=('微软雅黑', 10), rowheight=30, background="white")
        style.configure("Treeview.Heading", font=('微软雅黑', 10, 'bold'),
                        background="#e0e0e0", foreground="#333")
        style.map('Treeview',
                  background=[('selected', '#0078D7')],
                  foreground=[('selected', 'white')])

    def create_control_panel(self, parent):
        control_frame = ttk.Frame(parent)
        control_frame.pack(fill=tk.X, pady=(0, 5))

        left_control = ttk.Frame(control_frame)
        left_control.pack(side=tk.LEFT)

        self.translate_btn = ttk.Button(left_control, text="▶ 开始翻译",
                                        command=lambda: self.translation_controller.start_translation())
        self.translate_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.continue_btn = ttk.Button(left_control, text="⏩ 续翻",
                                        command=lambda: self.translation_controller.continue_translation())
        self.continue_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.stop_btn = ttk.Button(left_control, text="⏹ 停止", state=tk.DISABLED,
                                    command=lambda: self.translation_controller.stop_translation())
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 5))

        right_control = ttk.Frame(control_frame)
        right_control.pack(side=tk.RIGHT)
        ttk.Button(right_control, text="🔍 翻译查漏",
                   command=lambda: self.translation_controller._start_missing_translation_check()).pack(
                       side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_control, text="🖼️ 图片翻译",
                   command=lambda: self.image_handler.start_image_translation()).pack(
                       side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_control, text="📥 导出EPUB",
                   command=lambda: self.translation_controller.export_epub_file()).pack(
                       side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_control, text="💾 保存译文",
                   command=lambda: self.translation_controller.save_translation()).pack(
                       side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_control, text="📄 导出对照",
                   command=lambda: self.translation_controller.export_comparison()).pack(
                       side=tk.RIGHT, padx=(5, 0))

        middle_control = ttk.Frame(control_frame)
        middle_control.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(20, 20))
        ttk.Label(middle_control, text="文本翻译:").pack(side=tk.LEFT)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(middle_control, variable=self.progress_var,
                                            maximum=100, length=200)
        self.progress_bar.pack(side=tk.LEFT, padx=(5, 0), fill=tk.X, expand=True)
        ttk.Label(middle_control, text=" | ").pack(side=tk.LEFT)
        ttk.Label(middle_control, text="图片翻译:").pack(side=tk.LEFT)
        self.image_progress_label = ttk.Label(middle_control, text="待机")
        self.image_progress_label.pack(side=tk.LEFT, padx=(5, 0))

        support_center = ttk.Frame(parent)
        support_center.pack(fill=tk.X)
        ttk.Button(support_center, text="❤️ 支持作者",
                   command=self.open_support_dialog).pack(anchor='center', pady=(4, 0))

    def create_status_bar(self, parent):
        status_frame = ttk.Frame(parent)
        status_frame.pack(fill=tk.X)
        self.status_label = ttk.Label(status_frame, text="就绪", relief=tk.SUNKEN)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.api_status_label = ttk.Label(status_frame, text="API: 未配置", relief=tk.SUNKEN)
        self.api_status_label.pack(side=tk.RIGHT, padx=(5, 0))

    # ── 绑定 & 回调 ────────────────────────────────────

    def setup_bindings(self):
        self.root.bind('<F5>', lambda e: self.translation_controller.start_translation())
        self.root.bind('<F6>', lambda e: self.translation_controller.continue_translation())
        self.root.bind('<FocusOut>', lambda e: self._schedule_save_to_target(0))

    def _on_cell_edited(self, item_id, col_idx, old_value, new_value):
        self._schedule_save_to_target()

    # ── 表格数据操作 ───────────────────────────────────

    def load_data_to_table(self, source_lines, target_lines=None):
        for item in self.translation_table.get_children():
            self.translation_table.delete(item)

        while source_lines and not source_lines[-1].strip():
            source_lines.pop()

        if target_lines is None:
            target_lines = []
        else:
            while len(target_lines) > len(source_lines) and not target_lines[-1].strip():
                target_lines.pop()

        while len(target_lines) < len(source_lines):
            target_lines.append("")
        if len(target_lines) > len(source_lines):
            target_lines = target_lines[:len(source_lines)]

        for i, source in enumerate(source_lines):
            line_num = i + 1
            target = target_lines[i] if i < len(target_lines) else ""
            self.translation_table.insert(
                '', 'end', values=(line_num, source, target),
                tags=('evenrow' if i % 2 == 0 else 'oddrow',))

        self.translation_table.tag_configure('evenrow', background='#f9f9f9')
        self.translation_table.tag_configure('oddrow', background='white')

    def get_table_data(self):
        source_lines, target_lines = [], []
        for item in self.translation_table.get_children():
            values = self.translation_table.item(item)['values']
            if values:
                source_lines.append(values[1])
                target_lines.append(values[2] if len(values) > 2 else "")
        return source_lines, target_lines

    # ── 实时保存 ───────────────────────────────────────

    def _schedule_save_to_target(self, delay_ms: int = 400):
        if getattr(self, '_disable_auto_save', False):
            return
        tgt_path = getattr(self.file_importer, 'current_target_path', None)
        if not tgt_path:
            return
        self._debounce('save_tgt', delay_ms, self._atomic_save_target)

    def _atomic_save_target(self):
        tgt_path = getattr(self.file_importer, 'current_target_path', None)
        if not tgt_path:
            return
        try:
            _, target_lines = self.get_table_data()
            content = "\n".join(target_lines)
            tmp_path = tgt_path.with_suffix(tgt_path.suffix + '.tmp')
            self.file_handler.write_file(str(tmp_path), content)
            os.replace(str(tmp_path), str(tgt_path))

            mapping_dir = self.file_importer.current_mapping_dir
            if mapping_dir:
                try:
                    self.epub_processor.save_translations(str(mapping_dir), target_lines)
                except Exception:
                    pass
        except Exception:
            pass

    def _debounce(self, key, delay_ms, callback):
        attr = f'_debounce_{key}'
        old_id = getattr(self, attr, None)
        if old_id:
            self.root.after_cancel(old_id)
        setattr(self, attr, self.root.after(delay_ms, callback))

    # ── 窗口打开 ──────────────────────────────────────

    def open_settings(self):
        SettingsWindow(self.root, self.config_manager, self._on_settings_updated)

    def open_glossary(self):
        GlossaryWindow(self.root, self.config_manager)

    def open_concurrent(self):
        ConcurrentWindow(self.root, self.config_manager)

    def open_support_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("支持作者")
        win.transient(self.root)
        win.resizable(False, False)
        container = ttk.Frame(win, padding=10)
        container.pack(fill=tk.BOTH, expand=True)
        email_frame = ttk.Frame(container)
        email_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(email_frame, text="作者邮箱：", font=("微软雅黑", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(email_frame, text="996043050@qq.com", font=("微软雅黑", 10)).pack(side=tk.LEFT, padx=(5, 10))

        def copy_email():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append("996043050@qq.com")
                messagebox.showinfo("复制成功", "邮箱已复制到剪贴板")
            except Exception:
                messagebox.showwarning("提示", "复制失败，请手动复制")

        ttk.Button(email_frame, text="复制邮箱", command=copy_email).pack(side=tk.LEFT)
        ttk.Frame(container).pack(pady=(8, 0))
        ttk.Button(container, text="关闭", command=win.destroy).pack()

    # ── 状态更新 ──────────────────────────────────────

    def _on_settings_updated(self):
        self.translator.refresh_api()
        if self.config_manager.is_api_configured():
            api_config = self.config_manager.get_api_config()
            model_name = api_config.get("model_name", "未知模型")
            self.api_status_label.config(text=f"API: 已配置 ({model_name})")
        else:
            self.api_status_label.config(text="API: 未配置")

    def update_image_progress(self, text):
        self.image_progress_label.config(text=text)

    def update_status(self, message):
        self.status_label.config(text=message)
