#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主窗口界面模块
UI 骨架：只负责布局和组件创建，业务逻辑委托给各 controller。
"""

import tkinter as tk
from tkinter import ttk, messagebox
import queue
import threading
import time
from pathlib import Path

from .table_editor import TableCellEditor
from .translation_controller import TranslationController
from .file_importer import FileImporter
from .image_translation_handler import ImageTranslationHandler
from .lazy_service import LazyService
from ..utils.logger import get_logger

logger = get_logger(__name__)


class MainWindow:
    def __init__(self, root, config_manager, app_paths=None):
        self.root = root
        self.config_manager = config_manager
        self.app_paths = app_paths
        self.translator = LazyService(
            "src.core.translator", "TranslatorEngine", config_manager
        )
        self.file_handler = LazyService(
            "src.utils.file_handler", "FileHandler"
        )
        self.epub_processor = LazyService(
            "src.core.epub_processor", "EPUBProcessor", app_paths=app_paths
        )

        # PERF-003：自动保存单飞化状态
        self._save_in_progress = False
        self._save_dirty_again = False
        self._save_thread = None
        self._undo_stack = []
        self._redo_stack = []
        self._manually_edited_items = set()
        self._hidden_items = set()
        self._all_items = []
        self._table_load_after_id = None
        self._table_load_generation = 0
        self._table_loading = False
        self._api_configured = None
        self._api_status_queue = queue.SimpleQueue()
        self._api_status_thread = None
        self._api_status_after_id = None
        self._closed = False
        self._review_filter_var = tk.StringVar(value="全部")
        self._search_var = tk.StringVar()
        self._search_case_var = tk.BooleanVar(value=False)
        self._search_regex_var = tk.BooleanVar(value=False)

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
            app_paths=getattr(self, 'app_paths', None),
        )

        # 延迟绑定：file_importer 需要 image_handler，走 Manga 默认模块
        self.file_importer.image_translation_starter = lambda: self.image_handler.start_default_image_translation()

        self.translation_controller = TranslationController(
            root=self.root,
            config_manager=self.config_manager,
            translator=self.translator,
            file_handler=self.file_handler,
            epub_processor=self.epub_processor,
            translation_table=self.translation_table,
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
        # Initial status rendering must not construct the HTTP client. The
        # settings callback refreshes it later only if it has been used.
        self._on_settings_updated(refresh_engine=False)

    # ── UI 布局 ─────────────────────────────────────────

    def setup_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.create_menu()
        self.create_toolbar(main_frame)
        self.create_work_area(main_frame)
        self.create_control_panel(main_frame)
        self.create_status_bar(main_frame)

    def create_toolbar(self, parent):
        toolbar_frame = ttk.Frame(parent)
        toolbar_frame.pack(fill=tk.X, pady=(0, 10))

        left_frame = ttk.Frame(toolbar_frame)
        left_frame.pack(side=tk.LEFT)
        ttk.Button(left_frame, text="导入文件", command=lambda: self.file_importer.import_file()).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(left_frame, text="粘贴文本", command=lambda: self.file_importer.import_clipboard()).pack(side=tk.LEFT, padx=(0, 5))

        middle_frame = ttk.Frame(toolbar_frame)
        middle_frame.pack(side=tk.LEFT, expand=True)

        right_frame = ttk.Frame(toolbar_frame)
        right_frame.pack(side=tk.RIGHT)
        self.project_label = ttk.Label(right_frame, text="未打开项目")
        self.project_label.pack(side=tk.LEFT, padx=(0, 12))
        self.model_label = ttk.Label(right_frame, text="")
        self.model_label.pack(side=tk.LEFT, padx=(0, 12))
        self.save_status_label = ttk.Label(right_frame, text="保存: 未保存")
        self.save_status_label.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(right_frame, text="设置", command=self.open_settings).pack(side=tk.RIGHT, padx=(5, 0))

    def create_menu(self):
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=0)
        file_menu.add_command(label="导入文件", accelerator="Ctrl+O", command=lambda: self.file_importer.import_file())
        file_menu.add_command(label="粘贴文本", command=lambda: self.file_importer.import_clipboard())
        file_menu.add_command(label="保存译文", accelerator="Ctrl+S", command=lambda: self.translation_controller.save_translation())
        export_menu = tk.Menu(file_menu, tearoff=0)
        export_menu.add_command(label="导出对照文件", command=lambda: self.translation_controller.export_comparison())
        export_menu.add_command(label="导出 EPUB", command=lambda: self.translation_controller.export_epub_file())
        file_menu.add_cascade(label="导出", menu=export_menu)
        menu_bar.add_cascade(label="文件", menu=file_menu)

        edit_menu = tk.Menu(menu_bar, tearoff=0)
        edit_menu.add_command(label="撤销", accelerator="Ctrl+Z", command=self.undo)
        edit_menu.add_command(label="重做", accelerator="Ctrl+Y", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="查找", accelerator="Ctrl+F", command=self.focus_search)
        edit_menu.add_command(label="编辑当前行", accelerator="F2", command=self.open_context_editor)
        edit_menu.add_command(label="清空选中译文", command=self.clear_selected_translations)
        edit_menu.add_command(label="翻译选中行", command=lambda: self.translation_controller.translate_selected_rows())
        menu_bar.add_cascade(label="编辑", menu=edit_menu)

        project_menu = tk.Menu(menu_bar, tearoff=0)
        project_menu.add_command(label="翻译未完成行", accelerator="F5", command=self._run_primary_action)
        project_menu.add_command(label="继续翻译", accelerator="F6", command=self._continue_translation)
        project_menu.add_command(label="运行质检", command=self.run_quality_check)
        project_menu.add_command(label="图片翻译", command=lambda: self.image_handler.start_image_translation())
        project_menu.add_command(label="AI 图片翻译...", command=lambda: self.image_handler.start_ai_image_translation())
        menu_bar.add_cascade(label="项目", menu=project_menu)

        tools_menu = tk.Menu(menu_bar, tearoff=0)
        tools_menu.add_command(label="翻译队列", command=self.open_concurrent)
        tools_menu.add_command(label="术语库", command=self.open_glossary)
        tools_menu.add_separator()
        tools_menu.add_command(label="设置", command=self.open_settings)
        menu_bar.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(menu_bar, tearoff=0)
        help_menu.add_command(label="支持作者", command=self.open_support_dialog)
        menu_bar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menu_bar)

    def create_work_area(self, parent):
        work_frame = ttk.LabelFrame(parent, text="翻译内容对照表", padding=5)
        work_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        review_bar = ttk.Frame(work_frame)
        review_bar.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(review_bar, text="筛选:").pack(side=tk.LEFT)
        self.review_filter = ttk.Combobox(review_bar, textvariable=self._review_filter_var,
                                          values=("全部", "未翻译", "手工修改", "质检问题"), state="readonly", width=11)
        self.review_filter.pack(side=tk.LEFT, padx=(4, 12))
        self.review_filter.bind("<<ComboboxSelected>>", lambda _event: self.apply_review_filter())
        ttk.Label(review_bar, text="搜索:").pack(side=tk.LEFT)
        self.search_entry = ttk.Entry(review_bar, textvariable=self._search_var, width=28)
        self.search_entry.pack(side=tk.LEFT, padx=(4, 4))
        self.search_entry.bind("<Return>", lambda _event: self.find_next())
        ttk.Checkbutton(review_bar, text="区分大小写", variable=self._search_case_var).pack(side=tk.LEFT)
        ttk.Checkbutton(review_bar, text="正则", variable=self._search_regex_var).pack(side=tk.LEFT)
        ttk.Button(review_bar, text="上一个", command=lambda: self.find_next(reverse=True)).pack(side=tk.RIGHT)
        ttk.Button(review_bar, text="下一个", command=self.find_next).pack(side=tk.RIGHT, padx=(0, 4))

        self.empty_state = ttk.Frame(work_frame, padding=28)
        ttk.Label(self.empty_state, text="导入内容后即可开始翻译", font=("TkDefaultFont", 12, "bold")).pack(pady=(10, 16))
        empty_actions = ttk.Frame(self.empty_state)
        empty_actions.pack()
        ttk.Button(empty_actions, text="导入文件", command=lambda: self.file_importer.import_file()).pack(side=tk.LEFT, padx=4)
        ttk.Button(empty_actions, text="粘贴文本", command=lambda: self.file_importer.import_clipboard()).pack(side=tk.LEFT, padx=4)
        ttk.Button(empty_actions, text="打开最近项目", command=self.open_recent_project).pack(side=tk.LEFT, padx=4)
        self.empty_state.pack(fill=tk.X, pady=(12, 20))

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
        self.translation_table.bind('<F2>', lambda _event: self.open_context_editor())

        # 右键菜单（由 TranslationController 管理）
        self.translation_table.bind('<Button-3>', lambda e: self.translation_controller.show_context_menu(e))

        self.setup_table_styles()

    def setup_table_styles(self):
        style = ttk.Style()
        font_family = self.config_manager.get_app_config().get("ui_font_family", "TkDefaultFont")
        font_size = self.config_manager.get_app_config().get("ui_font_size", 10)
        style.configure("Treeview", font=(font_family, font_size), rowheight=max(28, font_size * 3), background="white")
        style.configure("Treeview.Heading", font=(font_family, font_size, 'bold'),
                        background="#e0e0e0", foreground="#333")
        style.map('Treeview',
                  background=[('selected', '#0078D7')],
                  foreground=[('selected', 'white')])

    def create_control_panel(self, parent):
        control_frame = ttk.Frame(parent)
        control_frame.pack(fill=tk.X, pady=(0, 5))

        left_control = ttk.Frame(control_frame)
        left_control.pack(side=tk.LEFT)

        self.translate_btn = ttk.Button(left_control, text="翻译未完成行", command=self._run_primary_action)
        self.translate_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.continue_btn = ttk.Button(left_control, text="继续翻译",
                                        command=self._continue_translation)
        self.continue_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.stop_btn = ttk.Button(left_control, text="停止", state=tk.DISABLED,
                                    command=lambda: self.translation_controller.stop_translation())
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 5))

        right_control = ttk.Frame(control_frame)
        right_control.pack(side=tk.RIGHT)
        ttk.Button(right_control, text="保存", command=lambda: self.translation_controller.save_translation()).pack(side=tk.RIGHT, padx=(5, 0))
        self.more_actions_menu = tk.Menu(right_control, tearoff=0)
        self.more_actions_menu.add_command(label="运行质检", command=self.run_quality_check)
        self.more_actions_menu.add_command(
            label="图片翻译", command=lambda: self.image_handler.start_image_translation()
        )
        self._image_action_index = self.more_actions_menu.index("end")
        self.more_actions_menu.add_separator()
        self.more_actions_menu.add_command(
            label="导出对照文件", command=lambda: self.translation_controller.export_comparison()
        )
        self.more_actions_menu.add_command(
            label="导出 EPUB", command=lambda: self.translation_controller.export_epub_file()
        )
        self._epub_action_index = self.more_actions_menu.index("end")
        ttk.Menubutton(
            right_control, text="更多操作", menu=self.more_actions_menu
        ).pack(side=tk.RIGHT)

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
        self.task_summary_label = ttk.Label(middle_control, text="")
        self.task_summary_label.pack(side=tk.LEFT, padx=(12, 0))

    def create_status_bar(self, parent):
        status_frame = ttk.Frame(parent)
        status_frame.pack(fill=tk.X)
        self.status_label = ttk.Label(status_frame, text="就绪", relief=tk.SUNKEN)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.api_status_label = ttk.Label(status_frame, text="API: 未配置", relief=tk.SUNKEN)
        self.api_status_label.pack(side=tk.RIGHT, padx=(5, 0))

    # ── 绑定 & 回调 ────────────────────────────────────

    def setup_bindings(self):
        self.root.bind('<F5>', lambda e: self._run_primary_action())
        self.root.bind('<F6>', lambda e: self._continue_translation())
        self.root.bind('<Control-o>', lambda e: self.file_importer.import_file())
        self.root.bind('<Control-s>', lambda e: self.translation_controller.save_translation())
        self.root.bind('<Control-f>', lambda e: self.focus_search())
        self.root.bind('<Control-z>', lambda e: self.undo())
        self.root.bind('<Control-y>', lambda e: self.redo())
        self.root.bind('<FocusOut>', lambda e: self._schedule_save_to_target(0))

    def _on_cell_edited(self, item_id, col_idx, old_value, new_value):
        self._record_edit(item_id, col_idx, old_value, new_value)
        self._manually_edited_items.add(item_id)
        self._schedule_save_to_target()
        self.refresh_action_state()

    # ── 表格数据操作 ───────────────────────────────────

    def load_data_to_table(self, source_lines, target_lines=None):
        self._table_load_generation += 1
        generation = self._table_load_generation
        if self._table_load_after_id is not None:
            try:
                self.root.after_cancel(self._table_load_after_id)
            except Exception:
                pass
            self._table_load_after_id = None

        existing_items = self.translation_table.get_children()
        if existing_items:
            self.translation_table.delete(*existing_items)
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._manually_edited_items.clear()
        self._hidden_items.clear()
        self._all_items.clear()

        # Work on copies: callers retain ownership of their parsed content.
        source_lines = list(source_lines)
        while source_lines and not source_lines[-1].strip():
            source_lines.pop()

        target_lines = list(target_lines) if target_lines is not None else []
        while len(target_lines) > len(source_lines) and not target_lines[-1].strip():
            target_lines.pop()

        while len(target_lines) < len(source_lines):
            target_lines.append("")
        if len(target_lines) > len(source_lines):
            target_lines = target_lines[:len(source_lines)]

        self._table_loading = True
        self.translate_btn.config(text="正在加载", state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.update_status(f"正在加载 0/{len(source_lines)} 行")
        source_path = getattr(
            getattr(self, 'file_importer', None), 'current_source_path', None
        )
        self._load_table_chunk(
            source_lines, target_lines, 0, generation, source_path
        )

    def _load_table_chunk(
        self, source_lines, target_lines, start, generation, source_path
    ):
        if generation != self._table_load_generation:
            return

        chunk_size = 250
        end = min(start + chunk_size, len(source_lines))
        for i in range(start, end):
            source = source_lines[i]
            line_num = i + 1
            target = target_lines[i] if i < len(target_lines) else ""
            item = self.translation_table.insert(
                '', 'end', values=(line_num, source, target),
                tags=('evenrow' if i % 2 == 0 else 'oddrow',))
            self._all_items.append(item)

        if end < len(source_lines):
            self.update_status(f"正在加载 {end}/{len(source_lines)} 行")
            self._table_load_after_id = self.root.after(
                1,
                lambda: self._load_table_chunk(
                    source_lines, target_lines, end, generation, source_path
                ),
            )
            return

        self._table_load_after_id = None
        self._table_loading = False
        self.translation_table.tag_configure('evenrow', background='#f9f9f9')
        self.translation_table.tag_configure('oddrow', background='white')
        self.project_label.config(text=source_path.name if source_path else f"临时文本 ({len(source_lines)} 行)")
        if source_path:
            self._remember_recent_file(source_path)
        if source_lines:
            self.empty_state.pack_forget()
        self._set_save_status("已加载")
        self.continue_btn.config(state=tk.NORMAL)
        self.refresh_action_state()

    # ── 校对工作流 ────────────────────────────────────

    def _record_edit(self, item_id, col_idx, old_value, new_value):
        if old_value == new_value:
            return
        self._undo_stack.append((item_id, col_idx, old_value, new_value))
        self._undo_stack = self._undo_stack[-200:]
        self._redo_stack.clear()

    def _apply_cell_value(self, item_id, col_idx, value, *, record=True):
        values = list(self.translation_table.item(item_id, 'values'))
        if not values or col_idx >= len(values):
            return
        old_value = values[col_idx]
        if old_value == value:
            return
        values[col_idx] = value
        self.translation_table.item(item_id, values=values)
        if record:
            self._record_edit(item_id, col_idx, old_value, value)
        self._manually_edited_items.add(item_id)
        self._schedule_save_to_target()
        self.refresh_action_state()

    def undo(self):
        if not self._undo_stack:
            self.update_status("没有可撤销的编辑")
            return
        item_id, col_idx, old_value, new_value = self._undo_stack.pop()
        self._redo_stack.append((item_id, col_idx, old_value, new_value))
        self._apply_cell_value(item_id, col_idx, old_value, record=False)
        self.update_status("已撤销编辑")

    def redo(self):
        if not self._redo_stack:
            self.update_status("没有可重做的编辑")
            return
        item_id, col_idx, old_value, new_value = self._redo_stack.pop()
        self._undo_stack.append((item_id, col_idx, old_value, new_value))
        self._apply_cell_value(item_id, col_idx, new_value, record=False)
        self.update_status("已重做编辑")

    def focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, tk.END)
        return "break"

    def find_next(self, reverse=False):
        import re
        query = self._search_var.get()
        if not query:
            return self.focus_search()
        flags = 0 if self._search_case_var.get() else re.IGNORECASE
        try:
            matcher = re.compile(query if self._search_regex_var.get() else re.escape(query), flags)
        except re.error as exc:
            self.update_status(f"搜索表达式无效: {exc}")
            return
        items = list(self.translation_table.get_children())
        current = self.translation_table.selection()
        start = items.index(current[-1]) if current and current[-1] in items else (-1 if not reverse else 0)
        ordered = items[start + 1:] + items[:start + 1] if not reverse else list(reversed(items[:start])) + list(reversed(items[start:]))
        for item in ordered:
            values = self.translation_table.item(item, 'values')
            if matcher.search(str(values[1])) or matcher.search(str(values[2])):
                self.translation_table.selection_set(item)
                self.translation_table.focus(item)
                self.translation_table.see(item)
                return
        self.update_status("未找到匹配内容")

    def apply_review_filter(self):
        mode = self._review_filter_var.get()
        for item in self._all_items:
            values = self.translation_table.item(item, 'values')
            source, target = str(values[1]).strip(), str(values[2]).strip()
            visible = mode == "全部"
            if mode == "未翻译":
                visible = bool(source and not target)
            elif mode == "手工修改":
                visible = item in self._manually_edited_items
            elif mode == "质检问题":
                visible = bool(source and (not target or source == target))
            if visible and item in self._hidden_items:
                self.translation_table.reattach(item, '', 'end')
                self._hidden_items.discard(item)
            elif not visible and item not in self._hidden_items:
                self.translation_table.detach(item)
                self._hidden_items.add(item)

    def clear_selected_translations(self):
        selected = self.translation_table.selection()
        if not selected:
            self.update_status("请先选择需要清空的行")
            return
        for item in selected:
            self._apply_cell_value(item, 2, "")
        self.update_status(f"已清空 {len(selected)} 行译文")

    def open_context_editor(self):
        selected = self.translation_table.selection()
        if not selected:
            self.update_status("请先选择一行")
            return
        item = selected[0]
        values = self.translation_table.item(item, 'values')
        items = self._all_items
        index = items.index(item) if item in items else 0
        dialog = tk.Toplevel(self.root)
        dialog.title(f"校对第 {values[0]} 行")
        dialog.geometry("820x540")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        before = self.translation_table.item(items[index - 1], 'values') if index else None
        after = self.translation_table.item(items[index + 1], 'values') if index + 1 < len(items) else None
        ttk.Label(frame, text=f"上一行: {before[1] if before else '无'}", wraplength=780).pack(anchor=tk.W)
        ttk.Label(frame, text="原文").pack(anchor=tk.W, pady=(10, 2))
        source = tk.Text(frame, height=7, wrap=tk.WORD, state=tk.NORMAL)
        source.insert('1.0', values[1])
        source.configure(state=tk.DISABLED)
        source.pack(fill=tk.X)
        ttk.Label(frame, text="译文").pack(anchor=tk.W, pady=(10, 2))
        target = tk.Text(frame, height=10, wrap=tk.WORD)
        target.insert('1.0', values[2])
        target.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=f"下一行: {after[1] if after else '无'}", wraplength=780).pack(anchor=tk.W, pady=(8, 0))
        def save_and_close():
            self._apply_cell_value(item, 2, target.get('1.0', tk.END).rstrip('\n'))
            dialog.destroy()
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text="保存", command=save_and_close).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 5))
        target.focus_set()
        dialog.bind('<Control-s>', lambda _event: save_and_close())

    def run_quality_check(self):
        issues = []
        for item in self._all_items:
            values = self.translation_table.item(item, 'values')
            if str(values[1]).strip() and (not str(values[2]).strip() or values[1] == values[2]):
                issues.append(item)
        if issues:
            self._review_filter_var.set("质检问题")
            self.apply_review_filter()
            self.translation_table.selection_set(issues[0])
            self.translation_table.see(issues[0])
            self.update_status(f"质检发现 {len(issues)} 行待处理内容")
        else:
            self.update_status("质检完成，未发现空译文或未翻译行")

    def _run_primary_action(self):
        if self._table_loading:
            self.update_status("内容仍在加载，请稍候")
            return
        self._show_all_rows()
        if not self.translation_table.get_children():
            self.file_importer.import_file()
        elif self._api_configured is None:
            self._apply_api_status(self.config_manager.is_api_configured())
            self._run_primary_action()
        elif not self._api_configured:
            self.open_settings()
        else:
            self.translation_controller.start_translation()

    def _remember_recent_file(self, source_path):
        config = self.config_manager.get_app_config()
        recent_files = [path for path in config.get("recent_files", []) if path != str(source_path)]
        recent_files.insert(0, str(source_path))
        config["recent_files"] = recent_files[:8]
        self.config_manager.save_app_config(config)

    def open_recent_project(self):
        recent_files = [Path(path) for path in self.config_manager.get_app_config().get("recent_files", [])]
        recent_files = [path for path in recent_files if path.exists()]
        if not recent_files:
            self.update_status("没有可打开的最近项目")
            return
        menu = tk.Menu(self.root, tearoff=0)
        for path in recent_files:
            menu.add_command(label=path.name, command=lambda selected=path: self.file_importer.import_file(str(selected)))
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def _continue_translation(self):
        self._show_all_rows()
        self.translation_controller.continue_translation()

    def _show_all_rows(self):
        if self._hidden_items:
            self._review_filter_var.set("全部")
            self.apply_review_filter()

    def refresh_action_state(self):
        if self._table_loading:
            self.translate_btn.config(text="正在加载", state=tk.DISABLED)
            return
        items = self._all_items
        has_content = bool(items)
        api_configured = self._api_configured
        pending = 0
        completed = 0
        for item in items:
            values = self.translation_table.item(item, 'values')
            pending += bool(str(values[1]).strip() and not str(values[2]).strip())
            completed += bool(str(values[1]).strip() and str(values[2]).strip())
        is_epub = bool(getattr(self.file_importer, 'current_mapping_dir', None)) if hasattr(self, 'file_importer') else False
        if not has_content:
            label, state = "导入文件", tk.NORMAL
        elif api_configured is None:
            label, state = "正在检查 API", tk.DISABLED
        elif not api_configured:
            label, state = "配置 API", tk.NORMAL
        elif pending:
            label, state = ("重试失败行" if self._review_filter_var.get() == "质检问题" else "翻译未完成行"), tk.NORMAL
        else:
            label, state = "运行质检", tk.NORMAL
        self.translate_btn.config(text=label, state=state,
                                  command=self.run_quality_check if not pending and has_content and api_configured else self._run_primary_action)
        if hasattr(self, 'more_actions_menu'):
            menu_state = tk.NORMAL if is_epub else tk.DISABLED
            self.more_actions_menu.entryconfigure(
                self._epub_action_index, state=menu_state
            )
            self.more_actions_menu.entryconfigure(
                self._image_action_index, state=menu_state
            )
        if hasattr(self, 'task_summary_label'):
            self.task_summary_label.config(text=f"待翻译 {pending} · 已完成 {completed}")

    def _set_save_status(self, status):
        if hasattr(self, 'save_status_label'):
            self.save_status_label.config(text=f"保存: {status}")

    def get_table_data(self):
        source_lines, target_lines = [], []
        for item in self._all_items:
            values = self.translation_table.item(item)['values']
            if values:
                source_lines.append(values[1])
                target_lines.append(values[2] if len(values) > 2 else "")
        return source_lines, target_lines

    # ── 实时保存 ───────────────────────────────────────

    def _schedule_save_to_target(self, delay_ms: int = 1000):
        """PERF-003：调度自动保存。

        - 默认 debounce 从 400ms 提升至 1000ms，减少翻译期间的写入频率。
        - delay_ms=0 表示立即保存（窗口关闭、停止翻译等关键事件）。
        """
        # BUG-008：尊重 auto_save 配置，禁用时不执行后台写入
        if self._table_loading:
            return
        app_config = self.config_manager.get_app_config()
        if not app_config.get("auto_save", True):
            return
        if getattr(self, '_disable_auto_save', False):
            return
        tgt_path = getattr(self.file_importer, 'current_target_path', None)
        if not tgt_path:
            return
        self._set_save_status("有未保存更改")
        self._debounce('save_tgt', delay_ms, self._atomic_save_target)

    def _atomic_save_target(self):
        """PERF-003：单飞化后台保存，避免阻塞 UI 主线程。

        - 保存正在进行时只标记 dirty_again，不启动第二次保存。
        - 在主线程捕获不可变快照，后台线程只做文件 I/O。
        - 保存完成后检查 dirty_again，若有新变更则再次保存。
        """
        # 单飞：保存正在进行时只标记 dirty
        if self._save_in_progress:
            self._save_dirty_again = True
            return

        tgt_path = getattr(self.file_importer, 'current_target_path', None)
        if not tgt_path:
            return

        # 在主线程捕获数据快照（Tkinter 非线程安全）
        try:
            _, target_lines = self.get_table_data()
        except Exception as e:
            logger.error("获取表格数据失败: %s", e)
            return

        # 不可变快照，避免后台线程遍历期间主线程修改列表
        target_snapshot = list(target_lines)
        mapping_dir = self.file_importer.current_mapping_dir

        self._save_in_progress = True
        self._save_dirty_again = False
        self._set_save_status("正在保存")

        def _save_worker():
            try:
                content = "\n".join(target_snapshot)
                # write_file 内部已使用原子写入（临时文件+replace）
                self.file_handler.write_file(str(tgt_path), content)

                if mapping_dir:
                    try:
                        self.epub_processor.save_translations(str(mapping_dir), target_snapshot)
                    except Exception as e:
                        logger.error("自动保存EPUB映射失败: %s", e)
                        self.root.after(0, lambda msg=str(e): self.update_status(f"⚠ 自动保存EPUB映射失败: {msg}"))
                self.root.after(0, lambda: self._set_save_status("已保存"))
            except Exception as e:
                logger.error("自动保存失败: %s", e)
                self.root.after(0, lambda msg=str(e): self.update_status(f"⚠ 自动保存失败: {msg}"))
                self.root.after(0, lambda: self._set_save_status("保存失败"))
            finally:
                self._save_in_progress = False
                # 保存期间有新变更请求时，再次调度保存
                if self._save_dirty_again:
                    self._save_dirty_again = False
                    self.root.after(0, self._atomic_save_target)

        self._save_thread = threading.Thread(target=_save_worker, daemon=True)
        self._save_thread.start()

    def flush_pending_save(self, timeout: float = 5.0):
        """PERF-003：等待后台保存完成（窗口关闭时调用）。"""
        if not self._save_in_progress:
            return
        deadline = time.time() + timeout
        while self._save_in_progress and time.time() < deadline:
            time.sleep(0.05)

    def _debounce(self, key, delay_ms, callback):
        attr = f'_debounce_{key}'
        old_id = getattr(self, attr, None)
        if old_id:
            self.root.after_cancel(old_id)
        setattr(self, attr, self.root.after(delay_ms, callback))

    # ── 窗口打开 ──────────────────────────────────────

    def open_settings(self):
        from .settings_window import SettingsWindow

        SettingsWindow(self.root, self.config_manager, self._on_settings_updated)

    def open_glossary(self):
        from .glossary_window import GlossaryWindow

        GlossaryWindow(self.root, self.config_manager)

    def open_concurrent(self):
        from .concurrent_window import ConcurrentWindow

        # R2-BUG-013：只允许一个队列窗口，避免旧窗口管理器泄漏
        cw = getattr(self, '_concurrent_window', None)
        if cw is not None and not cw._closed:
            try:
                if cw.win.winfo_exists():
                    cw.win.lift()
                    cw.win.focus_force()
                    return
            except Exception:
                pass
        self._concurrent_window = ConcurrentWindow(self.root, self.config_manager, app_paths=self.app_paths)

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

    def _on_settings_updated(self, refresh_engine=True):
        if not refresh_engine:
            self.api_status_label.config(text="API: 正在检查")
            self.model_label.config(text="正在读取配置")
            self.setup_table_styles()
            self.refresh_action_state()
            self._start_api_status_load()
            return

        configured = self.config_manager.is_api_configured()
        self._apply_api_status(configured)
        self.translator.call_if_initialized("refresh_api")

    def _start_api_status_load(self):
        def _load_status():
            try:
                result = self.config_manager.is_api_configured()
            except Exception as exc:
                logger.warning("后台读取 API 配置失败: %s", exc)
                result = False
            self._api_status_queue.put(result)

        self._api_status_thread = threading.Thread(
            target=_load_status,
            name="api-config-loader",
            daemon=True,
        )
        self._api_status_thread.start()
        self._api_status_after_id = self.root.after(25, self._poll_api_status)

    def _poll_api_status(self):
        self._api_status_after_id = None
        if self._closed:
            return
        try:
            configured = self._api_status_queue.get_nowait()
        except queue.Empty:
            self._api_status_after_id = self.root.after(25, self._poll_api_status)
            return
        self._apply_api_status(configured)

    def _apply_api_status(self, configured):
        self._api_configured = bool(configured)
        if self._api_configured:
            api_config = self.config_manager.get_api_config(load_secret=False)
            model_name = api_config.get("model_name", "未知模型")
            self.api_status_label.config(text=f"API: 已配置 ({model_name})")
            self.model_label.config(text=f"{self.config_manager.get_app_config().get('target_language', '中文')} / {model_name}")
        else:
            self.api_status_label.config(text="API: 未配置")
            self.model_label.config(text="请配置 API")
        self.setup_table_styles()
        if hasattr(self, 'translate_btn'):
            self.refresh_action_state()

    def update_image_progress(self, text):
        self.image_progress_label.config(text=text)

    def update_status(self, message):
        self.status_label.config(text=message)

    def close(self):
        """BUG-005：主窗口关闭时停止翻译、关闭所有引擎，释放 API 资源。"""
        self._closed = True
        if self._api_status_after_id is not None:
            try:
                self.root.after_cancel(self._api_status_after_id)
            except Exception:
                pass
            self._api_status_after_id = None

        if self._table_load_after_id is not None:
            try:
                self.root.after_cancel(self._table_load_after_id)
            except Exception:
                pass
            self._table_load_after_id = None
            self._table_load_generation += 1

        # 1. 停止主翻译引擎
        try:
            self.translator.call_if_initialized("stop")
        except Exception as e:
            logger.warning("停止翻译引擎失败: %s", e)

        # 2. 关闭队列翻译管理器（如有）
        # R2-BUG-013：窗口可能已通过 WM_DELETE_WINDOW 关闭，需检查 _closed 标志
        cw = getattr(self, '_concurrent_window', None)
        if cw is not None and not cw._closed and hasattr(cw, 'manager'):
            try:
                cw._on_close()
            except Exception as e:
                logger.warning("关闭队列翻译管理器失败: %s", e)

        # 3. 关闭主翻译引擎的 API 资源
        try:
            self.translator.call_if_initialized("close")
        except Exception as e:
            logger.warning("关闭翻译引擎API失败: %s", e)

        # PERF-003：等待后台保存完成，避免关闭时丢失数据
        try:
            self.flush_pending_save(timeout=5.0)
        except Exception as e:
            logger.warning("等待后台保存完成失败: %s", e)
