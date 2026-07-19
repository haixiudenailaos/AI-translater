#!/usr/bin/env python3
"""
主窗口界面模块
UI 骨架：只负责布局和组件创建，业务逻辑委托给各 controller。
"""

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ..application.autosave import (
    CLEAN,
    DIRTY,
    SAVE_FAILED,
    SAVING,
    AutosaveCoordinator,
    SaveResult,
)
from ..application.preflight import PreflightSeverity, build_preflight_report
from ..application.quality_review import inspect_quality
from ..application.translation_document import TranslationDocument
from ..application.usage import UsageStatistics
from ..domain.project import TranslationProject
from ..domain.translation import TranslationOptions
from ..utils.logger import get_logger
from .file_importer import FileImporter
from .image_translation_handler import ImageTranslationHandler
from .lazy_service import LazyService
from .onboarding import OnboardingController, OnboardingPanel
from .table_editor import TableCellEditor
from .theme import COLORS, accent_button_options, apply_theme
from .translation_controller import TranslationController
from .translation_table_adapter import TranslationTableAdapter

logger = get_logger(__name__)


class MainWindow:
    def __init__(self, root, config_manager, app_paths=None, edition_capabilities=None):
        self.root = root
        self.config_manager = config_manager
        self.app_paths = app_paths
        # P0-2：版本能力契约。``None`` 时由 ImageTranslationHandler 自动检测。
        self.edition_capabilities = edition_capabilities
        from ..core.queue_provider import ProviderLimiterRegistry

        # 应用级文本请求额度：主编辑器和后台队列通过同一注册表取槽。
        self._provider_limiter_registry = ProviderLimiterRegistry()
        self.translator = LazyService(
            "src.core.translator",
            "TranslatorEngine",
            config_manager,
            limiter_registry=self._provider_limiter_registry,
        )
        self.file_handler = LazyService("src.utils.file_handler", "FileHandler")
        self.epub_processor = LazyService(
            "src.core.epub_processor", "EPUBProcessor", app_paths=app_paths
        )

        # PERF §8：自动保存状态由 AutosaveCoordinator 管理（generation 状态机），
        # 旧的 _save_in_progress / _save_dirty_again / _save_thread 跨线程布尔标志已移除。
        self._undo_stack = []
        self._redo_stack = []
        self._manually_edited_items = set()
        self._hidden_items = set()
        self._all_items = []
        # P2-4：缓存每行的 (source, target) 文本，避免 apply_review_filter
        # 对 10,000 行逐行调用 ``table.item(item, "values")``——每次 Tk 调用
        # 都要走 Tcl 解释器，5000+ 行会引入 200ms+ 卡顿。
        self._row_values_cache: dict[str, tuple[str, str]] = {}
        self._table_load_after_id = None
        self._table_load_generation = 0
        self._table_loading = False
        self._api_configured = None
        self._api_status_queue = queue.SimpleQueue()
        self._api_status_thread = None
        self._api_status_after_id = None
        self._queue_status_after_id = None
        self._closed = False
        self._image_translation_busy = False
        # P0-5：独立于 autosave 的未保存编辑标记
        self._unsaved_edits = False
        self._review_filter_var = tk.StringVar(value="全部")
        self._quality_issue_items: set[str] | None = None
        self._last_preflight_report = None
        self._usage_statistics = UsageStatistics()
        self._last_usage_snapshot: dict[str, int | float] = {}
        self._search_var = tk.StringVar()
        self._search_case_var = tk.BooleanVar(value=False)
        self._search_regex_var = tk.BooleanVar(value=False)
        # 新手指导自动展示仅在首次 API 状态返回后评估一次
        self._onboarding_auto_evaluated = False

        self.setup_ui()

        # PERF §7：文档状态模型（业务状态唯一真相来源）和表格适配器。
        # _document 在 Tk 主线程修改，后台保存只接收不可变快照。
        self._document = TranslationDocument()
        self._table_adapter = TranslationTableAdapter(self.translation_table)
        # P2-4：把行值缓存注入适配器，翻译热路径更新译文时同步写缓存，
        # apply_review_filter 直接读缓存，避免逐行 Tcl 调用。
        self._table_adapter.set_row_values_cache(self._row_values_cache)

        # ── 初始化控制器 ────────────────────────────
        self.file_importer = FileImporter(
            root=self.root,
            config_manager=self.config_manager,
            file_handler=self.file_handler,
            epub_processor=self.epub_processor,
            table_loader=self.load_data_to_table,
            status_updater=self.update_status,
            image_translation_starter=None,  # 延迟绑定，见下方
            confirm_replace_session=self.confirm_save_before_replace,
            confirm_stop_active_translation=self.confirm_stop_translation_before_replace,
            document=self._document,
            edition_capabilities=self.edition_capabilities,
        )
        # P0-3：FileImporter 通过该回调读取当前 dirty 状态。
        self.file_importer.is_dirty_callback = lambda: self.has_unsaved_changes

        self.image_handler = ImageTranslationHandler(
            root=self.root,
            config_manager=self.config_manager,
            status_updater=self.update_status,
            image_progress_updater=self.update_image_progress,
            get_mapping_dir=lambda: self.file_importer.current_mapping_dir,
            open_settings=self.open_settings,
            busy_state_updater=self._set_image_translation_busy,
            app_paths=getattr(self, "app_paths", None),
            edition_capabilities=self.edition_capabilities,
        )

        # 延迟绑定：file_importer 需要 image_handler，走 Manga 默认模块
        self.file_importer.image_translation_starter = lambda: (
            self.image_handler.start_default_image_translation()
        )

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
            document=self._document,
            table_adapter=self._table_adapter,
            # P0-3：保存语义修复——Ctrl+S 直接写当前 target，无目标时 Save As 并写回 session。
            get_target_path=lambda: self.file_importer.current_target_path,
            update_target_path=lambda p: self.file_importer.update_session_target_path(p),
            on_save_success=self._mark_clean_after_save,
            preflight_callback=self._run_preflight,
            on_run_terminal=self._record_translation_usage,
        )

        # PERF §8：自动保存协调器（generation 状态机 + 单飞 + debounce）。
        # 工作线程只做文件 I/O，通过队列返回结果，主线程轮询更新状态。
        # cancel_callback 用于实际取消已调度的 debounce/max_delay 回调（§8 D-1）。
        self._autosave = AutosaveCoordinator(
            document=self._document,
            file_handler=self.file_handler,
            epub_processor=self.epub_processor,
            schedule_callback=self.root.after,
            result_callback=self._on_save_result,
            cancel_callback=self.root.after_cancel,
        )

        # 新手指导控制器：在全部控件与控制器创建完成后初始化。
        # 面板回调通过 lambda 延迟引用 self.onboarding，避免初始化顺序问题。
        self.onboarding = OnboardingController(
            root=self.root,
            panel=OnboardingPanel(
                self._onboarding_host,
                on_back=lambda: self.onboarding.back(),
                on_next=lambda: self.onboarding.next(),
                on_postpone=lambda: self.onboarding.postpone(),
                on_dismiss=lambda: self.onboarding.dismiss(),
            ),
            config_manager=self.config_manager,
            targets={
                "settings": lambda: self.settings_btn,
                "import": lambda: self.import_file_btn,
                "translate": lambda: self.translate_btn,
                "review": lambda: self.review_filter,
            },
            actions={
                "open_settings": self.open_settings,
                "import_file": self.file_importer.import_file,
                "paste_text": self.file_importer.import_clipboard,
            },
            status_updater=self.update_status,
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

        # Reserve the footer before packing the expandable work area.  When
        # Windows display scaling increases Tk's requested widget sizes, a
        # footer packed after the work area can fall outside the client area.
        footer_frame = ttk.Frame(main_frame)
        footer_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.create_control_panel(footer_frame)
        self.create_status_bar(footer_frame)

        self.create_onboarding_host(main_frame)
        self.create_work_area(main_frame)

    def create_onboarding_host(self, parent):
        """新手指导面板宿主：始终 pack，面板未显示时不占视觉空间。"""
        self._onboarding_host = ttk.Frame(parent)
        self._onboarding_host.pack(fill=tk.X, pady=(0, 6))

    def create_toolbar(self, parent):
        toolbar_frame = ttk.Frame(parent)
        toolbar_frame.pack(fill=tk.X, pady=(0, 10))

        left_frame = ttk.Frame(toolbar_frame)
        left_frame.pack(side=tk.LEFT)
        self.import_file_btn = ttk.Button(
            left_frame, text="导入文件", command=lambda: self.file_importer.import_file()
        )
        self.import_file_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.paste_text_btn = ttk.Button(
            left_frame, text="粘贴文本", command=lambda: self.file_importer.import_clipboard()
        )
        self.paste_text_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.queue_translate_btn = tk.Button(
            left_frame,
            text="批量翻译队列",
            command=self.open_concurrent,
            borderwidth=0,
            cursor="hand2",
            font=("TkDefaultFont", 10, "bold"),
            padx=12,
            pady=5,
            **accent_button_options(),
        )
        self.queue_translate_btn.pack(side=tk.LEFT, padx=(7, 0))

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
        self.settings_btn = ttk.Button(right_frame, text="设置", command=self.open_settings)
        self.settings_btn.pack(side=tk.RIGHT, padx=(5, 0))

    def create_menu(self):
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=0)
        file_menu.add_command(
            label="导入文件", accelerator="Ctrl+O", command=lambda: self.file_importer.import_file()
        )
        file_menu.add_command(
            label="粘贴文本", command=lambda: self.file_importer.import_clipboard()
        )
        file_menu.add_command(
            label="保存译文",
            accelerator="Ctrl+S",
            command=lambda: self.translation_controller.save_translation(),
        )
        export_menu = tk.Menu(file_menu, tearoff=0)
        export_menu.add_command(
            label="导出对照文件", command=lambda: self.translation_controller.export_comparison()
        )
        export_menu.add_command(
            label="导出 EPUB", command=lambda: self.translation_controller.export_epub_file()
        )
        file_menu.add_cascade(label="导出", menu=export_menu)
        menu_bar.add_cascade(label="文件", menu=file_menu)

        edit_menu = tk.Menu(menu_bar, tearoff=0)
        edit_menu.add_command(label="撤销", accelerator="Ctrl+Z", command=self.undo)
        edit_menu.add_command(label="重做", accelerator="Ctrl+Y", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="查找", accelerator="Ctrl+F", command=self.focus_search)
        edit_menu.add_command(
            label="编辑当前行", accelerator="F2", command=self.open_context_editor
        )
        edit_menu.add_command(label="清空选中译文", command=self.clear_selected_translations)
        edit_menu.add_command(
            label="翻译选中行",
            command=lambda: self.translation_controller.translate_selected_rows(),
        )
        menu_bar.add_cascade(label="编辑", menu=edit_menu)

        self.project_menu = tk.Menu(menu_bar, tearoff=0)
        # P1-10：主命令收敛——工具栏的 translate_btn 是单一上下文主动作
        # （refresh_action_state 按 文档/API/待翻译 状态动态切换文案与命令）。
        # 菜单里的 F5/F6 是该主动作两个子分支（开始新翻译 / 继续暂停后翻译）
        # 的显式入口，复用同一组 controller 方法，避免新增平行命令。
        self.project_menu.add_command(
            label="翻译未完成行", accelerator="F5", command=self._run_primary_action
        )
        self.project_menu.add_command(
            label="继续翻译", accelerator="F6", command=self._continue_translation
        )
        self.project_menu.add_command(label="运行质检", command=self.run_quality_check)
        self.project_menu.add_command(
            label="本地模块图片翻译", command=lambda: self.image_handler.start_image_translation()
        )
        self._project_local_image_action_index = self.project_menu.index("end")
        self.project_menu.add_command(
            label="AI 图片翻译...", command=lambda: self.image_handler.start_ai_image_translation()
        )
        self._project_ai_image_action_index = self.project_menu.index("end")
        menu_bar.add_cascade(label="项目", menu=self.project_menu)

        tools_menu = tk.Menu(menu_bar, tearoff=0)
        tools_menu.add_command(label="批量翻译队列...", command=self.open_concurrent)
        tools_menu.add_command(label="术语库", command=self.open_glossary)
        tools_menu.add_separator()
        tools_menu.add_command(label="设置", command=self.open_settings)
        menu_bar.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(menu_bar, tearoff=0)
        help_menu.add_command(label="新手指导", command=self.open_onboarding)
        help_menu.add_separator()
        help_menu.add_command(label="支持作者", command=self.open_support_dialog)
        menu_bar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menu_bar)

    def create_work_area(self, parent):
        work_frame = ttk.LabelFrame(parent, text="翻译内容对照表", padding=5)
        work_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        review_bar = ttk.Frame(work_frame)
        review_bar.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(review_bar, text="筛选:").pack(side=tk.LEFT)
        self.review_filter = ttk.Combobox(
            review_bar,
            textvariable=self._review_filter_var,
            values=("全部", "未翻译", "手工修改", "质检问题"),
            state="readonly",
            width=11,
        )
        self.review_filter.pack(side=tk.LEFT, padx=(4, 12))
        self.review_filter.bind("<<ComboboxSelected>>", lambda _event: self.apply_review_filter())
        ttk.Label(review_bar, text="搜索:").pack(side=tk.LEFT)
        self.search_entry = ttk.Entry(review_bar, textvariable=self._search_var, width=28)
        self.search_entry.pack(side=tk.LEFT, padx=(4, 4))
        self.search_entry.bind("<Return>", lambda _event: self.find_next())
        ttk.Checkbutton(review_bar, text="区分大小写", variable=self._search_case_var).pack(
            side=tk.LEFT
        )
        ttk.Checkbutton(review_bar, text="正则", variable=self._search_regex_var).pack(side=tk.LEFT)
        ttk.Button(review_bar, text="上一个", command=lambda: self.find_next(reverse=True)).pack(
            side=tk.RIGHT
        )
        ttk.Button(review_bar, text="下一个", command=self.find_next).pack(
            side=tk.RIGHT, padx=(0, 4)
        )

        self.empty_state = ttk.Frame(work_frame, padding=28)
        ttk.Label(
            self.empty_state, text="导入内容后即可开始翻译", font=("TkDefaultFont", 12, "bold")
        ).pack(pady=(10, 16))
        empty_actions = ttk.Frame(self.empty_state)
        empty_actions.pack()
        ttk.Button(
            empty_actions, text="导入文件", command=lambda: self.file_importer.import_file()
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            empty_actions, text="粘贴文本", command=lambda: self.file_importer.import_clipboard()
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(empty_actions, text="打开最近项目", command=self.open_recent_project).pack(
            side=tk.LEFT, padx=4
        )
        self.empty_state.pack(fill=tk.X, pady=(12, 20))

        table_container = ttk.Frame(work_frame)
        table_container.pack(fill=tk.BOTH, expand=True)

        columns = ("line_number", "source_text", "target_text")
        self.translation_table = ttk.Treeview(
            table_container,
            columns=columns,
            show="headings",
            selectmode="extended",
            # The table expands into all remaining space.  A smaller requested
            # row count keeps the complete footer visible on high-DPI displays.
            height=5,
        )
        self.translation_table.heading("line_number", text="行号")
        self.translation_table.heading("source_text", text="原文")
        self.translation_table.heading("target_text", text="译文")
        self.translation_table.column(
            "line_number", width=60, minwidth=50, anchor="center", stretch=False
        )
        self.translation_table.column("source_text", width=400, minwidth=200, anchor="w")
        self.translation_table.column("target_text", width=400, minwidth=200, anchor="w")

        vsb = ttk.Scrollbar(
            table_container, orient="vertical", command=self.translation_table.yview
        )
        hsb = ttk.Scrollbar(
            table_container, orient="horizontal", command=self.translation_table.xview
        )
        self.translation_table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.translation_table.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_container.grid_rowconfigure(0, weight=1)
        table_container.grid_columnconfigure(0, weight=1)

        # 单元格编辑
        self._cell_editor = TableCellEditor(
            self.translation_table, editable_columns={1, 2}, on_save=self._on_cell_edited
        )
        self.translation_table.bind("<Double-Button-1>", self._cell_editor.on_double_click)
        self.translation_table.bind("<F2>", lambda _event: self.open_context_editor())

        # P2-1：补齐键盘等价路径，避免操作只能依赖双击和右键
        # Enter：打开上下文编辑器（与 F2 等价，符合桌面列表交互直觉）
        self.translation_table.bind("<Return>", lambda _event: self.open_context_editor())
        # Shift+F10 与 Menu 键：等价于右键，弹出上下文菜单
        self.translation_table.bind(
            "<Shift-F10>", self._show_context_menu_from_keyboard
        )
        self.translation_table.bind(
            "<App>", self._show_context_menu_from_keyboard
        )
        # Delete：清空选中行译文（多选时弹确认，避免误删）
        self.translation_table.bind("<Delete>", lambda _event: self.clear_selected_translations())

        # 右键菜单（由 TranslationController 管理）
        self.translation_table.bind(
            "<Button-3>", lambda e: self.translation_controller.show_context_menu(e)
        )

        self.setup_table_styles()

    def setup_table_styles(self):
        """P2-2：通过 theme.apply_theme 统一应用 token、named font 和对比度。

        “界面字号”不再只更新 Treeview：``apply_theme`` 会把 named font
        ``AppFont`` 注册到 ttk.Style 默认 widget，后续 ``update_font_size``
        通过 nametofont 即可全局生效。
        """
        style = ttk.Style()
        font_family = self.config_manager.get_app_config().get("ui_font_family", "TkDefaultFont")
        font_size = self.config_manager.get_app_config().get("ui_font_size", 10)
        apply_theme(style, font_family=font_family, font_size=font_size)

    def create_control_panel(self, parent):
        control_frame = ttk.Frame(parent)
        control_frame.pack(fill=tk.X, pady=(0, 5))

        left_control = ttk.Frame(control_frame)
        left_control.pack(side=tk.LEFT)

        self.translate_btn = ttk.Button(
            left_control, text="翻译未完成行", command=self._run_primary_action
        )
        self.translate_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.continue_btn = ttk.Button(
            left_control, text="继续翻译", command=self._continue_translation
        )
        self.continue_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.stop_btn = ttk.Button(
            left_control,
            text="停止",
            state=tk.DISABLED,
            command=lambda: self.translation_controller.stop_translation(),
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 5))

        right_control = ttk.Frame(control_frame)
        right_control.pack(side=tk.RIGHT)
        self.export_epub_btn = ttk.Button(
            right_control,
            text="导出 EPUB",
            command=lambda: self.translation_controller.export_epub_file(),
        )
        self.export_epub_btn.pack(side=tk.RIGHT, padx=(5, 0))
        self.more_actions_menu = tk.Menu(right_control, tearoff=0)
        self.more_actions_menu.add_command(label="运行质检", command=self.run_quality_check)
        self.more_actions_menu.add_command(
            label="本地模块图片翻译", command=lambda: self.image_handler.start_image_translation()
        )
        self._local_image_action_index = self.more_actions_menu.index("end")
        self.more_actions_menu.add_command(
            label="AI 图片翻译...", command=lambda: self.image_handler.start_ai_image_translation()
        )
        self._ai_image_action_index = self.more_actions_menu.index("end")
        self.more_actions_menu.add_separator()
        self.more_actions_menu.add_command(
            label="导出对照文件", command=lambda: self.translation_controller.export_comparison()
        )
        self.more_actions_menu.add_command(
            label="导出 EPUB", command=lambda: self.translation_controller.export_epub_file()
        )
        self._epub_action_index = self.more_actions_menu.index("end")
        ttk.Menubutton(right_control, text="更多操作", menu=self.more_actions_menu).pack(
            side=tk.RIGHT
        )

        image_control = ttk.Frame(parent)
        image_control.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(image_control, text="图片翻译方式:").pack(side=tk.LEFT)
        self.local_image_translate_btn = ttk.Button(
            image_control,
            text="本地模块翻译",
            width=16,
            command=lambda: self.image_handler.start_image_translation(),
        )
        self.local_image_translate_btn.pack(side=tk.LEFT, padx=(8, 5))
        self.ai_image_translate_btn = ttk.Button(
            image_control,
            text="AI 图片翻译",
            width=16,
            command=lambda: self.image_handler.start_ai_image_translation(),
        )
        self.ai_image_translate_btn.pack(side=tk.LEFT)

        middle_control = ttk.Frame(control_frame)
        middle_control.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(20, 20))
        ttk.Label(middle_control, text="文本翻译:").pack(side=tk.LEFT)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            middle_control, variable=self.progress_var, maximum=100, length=200
        )
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
        self.root.bind("<F5>", lambda e: self._run_primary_action())
        self.root.bind("<F6>", lambda e: self._continue_translation())
        shortcuts = {
            "o": lambda: self.file_importer.import_file(),
            "s": lambda: self.translation_controller.save_translation(),
            "f": self.focus_search,
            "z": self.undo,
            "y": self.redo,
        }
        for key, command in shortcuts.items():
            self.root.bind(f"<Control-{key}>", lambda _event, action=command: action())
            self.root.bind(f"<Command-{key}>", lambda _event, action=command: action())
        # macOS convention for redo; Windows/Linux keep Ctrl+Y above.
        self.root.bind("<Command-Shift-Z>", lambda _event: self.redo())

    def _on_cell_edited(self, item_id, col_idx, old_value, new_value):
        """P0-2：人工编辑写回唯一模型。

        TableCellEditor 不再直接修改 Treeview，而是通过此回调提交 edit command。
        _apply_cell_value 同时更新 Treeview 和 TranslationDocument，
        确保模型与视图保持一致，避免已编辑内容按旧值保存或导出。
        """
        self._apply_cell_value(item_id, col_idx, new_value)

    # ── 表格数据操作 ───────────────────────────────────

    def load_data_to_table(self, source_lines, target_lines=None):
        # P0-4：替换文档前使翻译控制器的当前 run_id 失效，
        # 防止旧翻译任务的迟到事件写入新文档的行索引。
        if hasattr(self, "translation_controller"):
            self.translation_controller.invalidate_session()

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
        self._quality_issue_items = None
        self._usage_statistics = UsageStatistics()
        self._last_usage_snapshot = {}
        # P2-4：行值缓存随表格清空一起重置。
        # getattr 兜底——部分测试用 MainWindow.__new__ 跳过 __init__，
        # 此时 _row_values_cache 未初始化（load_data_to_table 是它们的入口）。
        cache = getattr(self, "_row_values_cache", None)
        if cache is not None:
            cache.clear()
        self._all_items.clear()
        # PERF §7.5 步骤2：同步重置表格适配器（item ID 映射）。
        self._table_adapter.reset()

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
            target_lines = target_lines[: len(source_lines)]

        # PERF §7.5 步骤2：导入时同时填充模型（唯一真相来源）。
        # Treeview 分块加载在 _load_table_chunk 中追加 item ID 到适配器。
        # FileImporter 已将导入内容写入共享文档模型；直接调用此方法的旧
        # 调用方仍需更新模型，但相同内容不重复递增 version。
        if (
            self._document.source_lines() != source_lines
            or self._document.target_lines() != target_lines
        ):
            self._document.replace(source_lines, target_lines)

        # A successful import starts a clean session. Clear UI/autosave dirty
        # flags left by the previous document after the replacement is ready.
        self._unsaved_edits = False
        autosave = getattr(self, "_autosave", None)
        if autosave is not None:
            try:
                autosave.mark_clean()
            except Exception as exc:
                logger.debug("重置导入前的自动保存状态失败: %s", exc)
        session = getattr(getattr(self, "file_importer", None), "session", None)
        if session is not None:
            session.mark_clean()

        # 新手指导：确认非空内容后通知导入完成事件。空内容不完成本步骤。
        onboarding = getattr(self, "onboarding", None)
        if onboarding is not None and source_lines:
            onboarding.notify("content_loaded", count=len(source_lines))

        self._table_loading = True
        self.translate_btn.config(text="正在加载", state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.update_status(f"正在加载 0/{len(source_lines)} 行")
        source_path = getattr(getattr(self, "file_importer", None), "current_source_path", None)
        self._load_table_chunk(source_lines, target_lines, 0, generation, source_path)

    def _load_table_chunk(self, source_lines, target_lines, start, generation, source_path):
        if generation != self._table_load_generation:
            return

        chunk_size = 250
        end = min(start + chunk_size, len(source_lines))
        for i in range(start, end):
            source = source_lines[i]
            line_num = i + 1
            target = target_lines[i] if i < len(target_lines) else ""
            item = self.translation_table.insert(
                "",
                "end",
                values=(line_num, source, target),
                tags=("evenrow" if i % 2 == 0 else "oddrow",),
            )
            self._all_items.append(item)
            # P2-4：填充行值缓存，apply_review_filter 直接读缓存
            self._row_values_cache[item] = (source, target)
            # PERF §7.5 步骤2：同步维护适配器的行号→item ID 映射。
            self._table_adapter.append_item(item)

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
        self.translation_table.tag_configure("evenrow", background=COLORS["row_even"])
        self.translation_table.tag_configure("oddrow", background=COLORS["row_odd"])
        self.project_label.config(
            text=source_path.name if source_path else f"临时文本 ({len(source_lines)} 行)"
        )
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
        values = list(self.translation_table.item(item_id, "values"))
        if not values or col_idx >= len(values):
            return
        old_value = values[col_idx]
        if old_value == value:
            return
        values[col_idx] = value
        self.translation_table.item(item_id, values=values)
        # P2-4：同步更新行值缓存，保持 apply_review_filter 读到最新值
        if col_idx in (1, 2):
            cached = self._row_values_cache.get(item_id)
            if cached is None:
                source = values[1] if len(values) > 1 else ""
                target = values[2] if len(values) > 2 else ""
                self._row_values_cache[item_id] = (str(source), str(target))
            else:
                source, target = cached
                if col_idx == 1:
                    self._row_values_cache[item_id] = (str(value), target)
                else:
                    self._row_values_cache[item_id] = (source, str(value))
        # PERF §7.4/§7.5 步骤5：人工编辑同时更新文档模型，
        # 保持模型与 Treeview 一致。撤销/重做也走此路径。
        row_index = self._row_index_of(item_id)
        if row_index is not None:
            if col_idx == 2:
                self._document.update_target(row_index, value, manually_edited=True)
            elif col_idx == 1:
                self._document.update_source(row_index, value)
        if record:
            self._record_edit(item_id, col_idx, old_value, value)
        self._manually_edited_items.add(item_id)
        # P0-3：dirty 状态独立于 target_path 与 autosave。
        # 剪贴板会话没有 target_path，但人工编辑仍需进入关闭保护，
        # 因此这里无条件标记未保存，再让 _schedule_save_to_target 决定是否触发后台写入。
        self._unsaved_edits = True
        session = getattr(getattr(self, "file_importer", None), "session", None)
        if session is not None:
            session.mark_dirty()
        self._schedule_save_to_target()
        self.refresh_action_state()

    def _row_index_of(self, item_id) -> int | None:
        """根据 item ID 查找行号（线性查找，仅用于低频的单元格编辑）。"""
        try:
            return self._all_items.index(item_id)
        except ValueError:
            return None

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
        start = (
            items.index(current[-1])
            if current and current[-1] in items
            else (-1 if not reverse else 0)
        )
        ordered = (
            items[start + 1 :] + items[: start + 1]
            if not reverse
            else list(reversed(items[:start])) + list(reversed(items[start:]))
        )
        for item in ordered:
            values = self.translation_table.item(item, "values")
            if matcher.search(str(values[1])) or matcher.search(str(values[2])):
                self.translation_table.selection_set(item)
                self.translation_table.focus(item)
                self.translation_table.see(item)
                return
        self.update_status("未找到匹配内容")

    def apply_review_filter(self):
        mode = self._review_filter_var.get()
        # P1-6：按 _all_items 原始顺序重建 treeview，确保筛选恢复后行序正确。
        # 旧实现只 reattach 刚恢复可见的行到 "end"，导致它们追加到已显示行
        # 的末尾——例如 _all_items=[A,B,C,D,E]，先隐藏 B 和 D 再恢复全部，
        # 结果变成 [A,C,E,B,D] 而非 [A,B,C,D,E]。
        # 新实现清空 _hidden_items 后逐项按 _all_items 顺序 reattach/detach，
        # 每个可见项 reattach 到 "end" 时自然按原始顺序追加。
        # P2-4：用 _row_values_cache 替代逐行 table.item()——后者每次都要
        # 走 Tcl 解释器，5000+ 行的文档筛选会引入 200ms+ 卡顿。
        # 缓存在 _load_table_chunk 填充、_apply_cell_value 同步更新。
        # PERF-6c：只对可见性发生变化的行调用 reattach/detach，避免对
        # 已经处于正确可见状态的行发起冗余 Tcl 调用。
        previously_hidden = set(self._hidden_items)
        self._hidden_items.clear()
        # P2-4：getattr 兜底——测试用 __new__ 跳过 __init__ 时不设此属性。
        cache = getattr(self, "_row_values_cache", None) or {}
        # 当筛选模式与上次不同时，所有行的可见性都可能变化，需要全量
        # reattach 以保证顺序正确；模式相同时可跳过未变化的行。
        mode_changed = getattr(self, "_last_review_filter_mode", None) != mode
        self._last_review_filter_mode = mode
        for item in self._all_items:
            cached = cache.get(item)
            if cached is None:
                # 兜底：缓存未命中（理论上不应发生），回退到 Tk 调用
                values = self.translation_table.item(item, "values")
                source, target = str(values[1]).strip(), str(values[2]).strip()
            else:
                source, target = cached[0].strip(), cached[1].strip()
            visible = mode == "全部"
            if mode == "未翻译":
                visible = bool(source and not target)
            elif mode == "手工修改":
                visible = item in self._manually_edited_items
            elif mode == "质检问题":
                quality_items = getattr(self, "_quality_issue_items", None)
                visible = (
                    item in quality_items
                    if quality_items is not None
                    else bool(source and (not target or source == target))
                )
            was_hidden = item in previously_hidden
            if visible:
                # PERF-6c：模式未变且原本可见时跳过冗余 reattach
                if mode_changed or was_hidden:
                    self.translation_table.reattach(item, "", "end")
            else:
                self._hidden_items.add(item)
                # PERF-6c：原本已隐藏且模式未变时跳过冗余 detach
                if mode_changed or not was_hidden:
                    self.translation_table.detach(item)

    def clear_selected_translations(self):
        selected = self.translation_table.selection()
        if not selected:
            self.update_status("请先选择需要清空的行")
            return
        # P2-1：多选清空属于破坏性操作，必须显式确认；
        # 单选直接清空（属于常规编辑，可由 Ctrl+Z 撤销）。
        if len(selected) > 1:
            confirmed = messagebox.askyesno(
                "确认清空",
                f"将清空 {len(selected)} 行译文，确定继续吗？",
                default="no",
            )
            if not confirmed:
                return
        for item in selected:
            self._apply_cell_value(item, 2, "")
        self.update_status(f"已清空 {len(selected)} 行译文")

    def _show_context_menu_from_keyboard(self, event: tk.Event) -> None:
        """P2-1：Shift+F10 / Menu 键触发右键菜单的键盘等价路径。

        在选中行可见区域的中央位置弹出菜单，避免依赖鼠标坐标。
        """
        table = self.translation_table
        selected = table.selection()
        if not selected:
            return
        try:
            bbox = table.bbox(selected[0])
        except KeyError:
            return
        if not bbox:
            return
        # bbox = (x, y, width, height) 相对于 table widget
        x = bbox[0] + max(0, bbox[2] // 2)
        y = bbox[1] + max(0, bbox[3] // 2)
        try:
            root_x = table.winfo_rootx() + x
            root_y = table.winfo_rooty() + y
        except tk.TclError:
            return
        # 构造一个轻量事件对象复用 show_context_menu
        try:
            # tk_popup 接受屏幕坐标
            self.translation_controller.context_menu.tk_popup(root_x, root_y)
        finally:
            self.translation_controller.context_menu.grab_release()

    def open_context_editor(self):
        selected = self.translation_table.selection()
        if not selected:
            self.update_status("请先选择一行")
            return
        item = selected[0]
        values = self.translation_table.item(item, "values")
        items = self._all_items
        index = items.index(item) if item in items else 0
        dialog = tk.Toplevel(self.root)
        dialog.title(f"校对第 {values[0]} 行")
        dialog.geometry("820x540")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        before = self.translation_table.item(items[index - 1], "values") if index else None
        after = (
            self.translation_table.item(items[index + 1], "values")
            if index + 1 < len(items)
            else None
        )
        ttk.Label(frame, text=f"上一行: {before[1] if before else '无'}", wraplength=780).pack(
            anchor=tk.W
        )
        ttk.Label(frame, text="原文").pack(anchor=tk.W, pady=(10, 2))
        source = tk.Text(frame, height=7, wrap=tk.WORD, state=tk.NORMAL)
        source.insert("1.0", values[1])
        source.configure(state=tk.DISABLED)
        source.pack(fill=tk.X)
        ttk.Label(frame, text="译文").pack(anchor=tk.W, pady=(10, 2))
        target = tk.Text(frame, height=10, wrap=tk.WORD)
        target.insert("1.0", values[2])
        target.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=f"下一行: {after[1] if after else '无'}", wraplength=780).pack(
            anchor=tk.W, pady=(8, 0)
        )

        def save_and_close():
            self._apply_cell_value(item, 2, target.get("1.0", tk.END).rstrip("\n"))
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(10, 0))
        save_btn = ttk.Button(buttons, text="保存", command=save_and_close)
        save_btn.pack(side=tk.RIGHT)
        cancel_btn = ttk.Button(buttons, text="取消", command=dialog.destroy)
        cancel_btn.pack(side=tk.RIGHT, padx=(0, 5))
        target.focus_set()
        # P2-1：模态窗口补 Escape 关闭、Ctrl+S 保存、焦点恢复
        # 不绑定 Return：target 是多行 Text，Enter 用于换行；
        # Ctrl+S 已提供键盘保存路径，符合桌面校对工具惯例。
        dialog.bind("<Control-s>", lambda _event: save_and_close())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Destroy>", lambda _event: self.translation_table.focus_set(), add="+")

    def run_quality_check(self):
        project = self._build_runtime_project()
        report = inspect_quality(project, glossary_terms=self._glossary_pairs())
        issues = []
        for issue in report.issues:
            item = self._table_adapter.item_id(issue.line_index)
            if item is not None:
                issues.append(item)
        self._quality_issue_items = set(issues)
        # 新手指导：通知质检已接触，不自动把总状态改为 completed
        onboarding = getattr(self, "onboarding", None)
        if onboarding is not None:
            onboarding.notify("quality_check_run", issue_count=len(issues))
        if issues:
            self._review_filter_var.set("质检问题")
            self.apply_review_filter()
            self.translation_table.selection_set(issues[0])
            self.translation_table.see(issues[0])
            self.update_status(
                f"质检发现 {len(issues)} 行问题（错误 {report.error_count}，可在筛选中查看）"
            )
        else:
            self.update_status("质检完成，未发现需要处理的问题")

    def _build_runtime_project(self) -> TranslationProject:
        source_lines = self._document.source_lines()
        target_lines = self._document.target_lines()
        source_path = getattr(getattr(self, "file_importer", None), "current_source_path", None)
        mapping_dir = getattr(getattr(self, "file_importer", None), "current_mapping_dir", None)
        completed_indices = {
            index for index, target in enumerate(target_lines) if target and target.strip()
        }
        manually_edited_indices = {
            index
            for index in range(self._document.row_count)
            if self._document.is_manually_edited(index)
        }
        completed_indices.update(manually_edited_indices)
        return TranslationProject(
            project_id="active-session",
            source_path=str(source_path or ""),
            source_fingerprint="",
            file_type="epub" if mapping_dir else "txt",
            mapping_dir=str(mapping_dir or ""),
            original_lines=source_lines,
            translated_lines=target_lines,
            manually_edited_indices=manually_edited_indices,
            completed_indices=completed_indices,
        )

    def _glossary_pairs(self) -> tuple[tuple[str, str], ...]:
        terms = self.config_manager.get_glossary().get("terms", [])
        if not isinstance(terms, list):
            return ()
        return tuple(
            (str(term.get("source", "")), str(term.get("target", "")))
            for term in terms
            if isinstance(term, dict)
        )

    def _run_preflight(self, action: str) -> bool:
        project = self._build_runtime_project()
        app_config = self.config_manager.get_app_config()
        api_config = self.config_manager.get_api_config(load_secret=False)
        options = TranslationOptions(
            target_language=str(app_config.get("target_language", "")),
            model_name=str(api_config.get("model_name", "")),
            batch_size=int(app_config.get("batch_lines", 20)),
            temperature=float(api_config.get("temperature", 0.3)),
            max_tokens=int(api_config.get("max_tokens", 2048)),
        )
        report = build_preflight_report(
            project,
            options,
            action=action,
            provider=str(api_config.get("provider", "")),
            glossary_terms=self._glossary_pairs(),
            input_price_per_million=api_config.get("input_price_per_million"),
            output_price_per_million=api_config.get("output_price_per_million"),
        )
        self._last_preflight_report = report
        errors = [issue.message for issue in report.issues if issue.severity is PreflightSeverity.ERROR]
        if errors:
            messagebox.showerror("翻译预检", "\n".join(errors), parent=self.root)
            return False
        warnings = [issue.message for issue in report.issues if issue.severity is PreflightSeverity.WARNING]
        if warnings and app_config.get("preflight_confirm_warnings", True):
            approved = messagebox.askyesno(
                "翻译预检",
                "\n".join(warnings)
                + f"\n\n待翻译 {report.pending_lines} 行，预计输入 {report.estimated_input_tokens} token。\n是否继续？",
                parent=self.root,
            )
            if not approved:
                return False
        self.update_status(
            f"预检通过：待翻译 {report.pending_lines} 行，预计输入 {report.estimated_input_tokens} token"
        )
        return True

    def _record_translation_usage(self, _result, _mode: str) -> None:
        get_snapshot = getattr(self.translator, "call_if_initialized", None)
        if not callable(get_snapshot):
            return
        snapshot = get_snapshot("get_usage_snapshot")
        if not isinstance(snapshot, dict):
            return
        self._usage_statistics.record_metrics_delta(snapshot, self._last_usage_snapshot)
        self._last_usage_snapshot = dict(snapshot)

    def _run_primary_action(self):
        """P1-10：单一上下文主动作。

        工具栏的 ``translate_btn`` 是面向用户的主入口，``refresh_action_state``
        根据当前文档/API/待翻译状态动态切换其文案（导入文件 / 配置 API /
        翻译未完成行 / 重试失败行 / 运行质检）与命令。本方法是其中
        「翻译/导入/配置」分支的统一派发器；F5 菜单与按钮共用同一 command。

        「继续翻译」（F6）是暂停后恢复的显式子分支，保留为独立入口但
        复用同一 controller，避免平行命令分裂。
        """
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
            # 新手指导：即将开始真实翻译前通知里程碑事件
            onboarding = getattr(self, "onboarding", None)
            if onboarding is not None:
                onboarding.notify("translation_started")
            self.translation_controller.start_translation()

    def _remember_recent_file(self, source_path):
        config = self.config_manager.get_app_config()
        recent_files = [path for path in config.get("recent_files", []) if path != str(source_path)]
        recent_files.insert(0, str(source_path))
        config["recent_files"] = recent_files[:8]
        self.config_manager.save_app_config(config)

    def open_recent_project(self):
        recent_files = [
            Path(path) for path in self.config_manager.get_app_config().get("recent_files", [])
        ]
        recent_files = [path for path in recent_files if path.exists()]
        if not recent_files:
            self.update_status("没有可打开的最近项目")
            return
        menu = tk.Menu(self.root, tearoff=0)
        for path in recent_files:
            menu.add_command(
                label=path.name,
                command=lambda selected=path: self.file_importer.import_file(str(selected)),
            )
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
        # PERF §7：从文档模型读取统计，避免逐行调用 Tk item()。
        source_lines = self._document.source_lines()
        target_lines = self._document.target_lines()
        has_content = bool(source_lines)
        api_configured = self._api_configured
        pending = 0
        completed = 0
        for source, target in zip(source_lines, target_lines, strict=True):
            src = source.strip()
            tgt = target.strip()
            if src:
                if tgt:
                    completed += 1
                else:
                    pending += 1
        is_epub = (
            bool(getattr(self.file_importer, "current_mapping_dir", None))
            if hasattr(self, "file_importer")
            else False
        )
        if not has_content:
            label, state = "导入文件", tk.NORMAL
        elif api_configured is None:
            label, state = "正在检查 API", tk.DISABLED
        elif not api_configured:
            label, state = "配置 API", tk.NORMAL
        elif pending:
            label, state = (
                ("重试失败行" if self._review_filter_var.get() == "质检问题" else "翻译未完成行"),
                tk.NORMAL,
            )
        else:
            label, state = "运行质检", tk.NORMAL
        self.translate_btn.config(
            text=label,
            state=state,
            command=self.run_quality_check
            if not pending and has_content and api_configured
            else self._run_primary_action,
        )
        if hasattr(self, "more_actions_menu"):
            menu_state = tk.NORMAL if is_epub else tk.DISABLED
            if hasattr(self, "export_epub_btn"):
                self.export_epub_btn.config(state=menu_state)
            self.more_actions_menu.entryconfigure(self._epub_action_index, state=menu_state)
            image_state = tk.DISABLED if self._image_translation_busy else menu_state
            # P0-2：Text Edition 下禁用本地 Manga 入口。
            manga_state = image_state if self._manga_enabled() else tk.DISABLED
            self.more_actions_menu.entryconfigure(
                self._local_image_action_index, state=manga_state
            )
            self.more_actions_menu.entryconfigure(self._ai_image_action_index, state=image_state)
        if hasattr(self, "local_image_translate_btn"):
            image_state = tk.NORMAL if is_epub and not self._image_translation_busy else tk.DISABLED
            # P0-2：Text Edition 下禁用本地 Manga 入口。
            manga_state = image_state if self._manga_enabled() else tk.DISABLED
            self.local_image_translate_btn.config(state=manga_state)
            self.ai_image_translate_btn.config(state=image_state)
        if hasattr(self, "project_menu"):
            project_image_state = (
                tk.NORMAL if is_epub and not self._image_translation_busy else tk.DISABLED
            )
            # P0-2：Text Edition 下禁用本地 Manga 入口。
            project_manga_state = (
                project_image_state if self._manga_enabled() else tk.DISABLED
            )
            self.project_menu.entryconfigure(
                self._project_local_image_action_index, state=project_manga_state
            )
            self.project_menu.entryconfigure(
                self._project_ai_image_action_index, state=project_image_state
            )
        if hasattr(self, "task_summary_label"):
            self.task_summary_label.config(text=f"待翻译 {pending} · 已完成 {completed}")

    def _manga_enabled(self) -> bool:
        """P0-2：当前版本是否启用本地 Manga 图片翻译。"""
        handler = getattr(self, "image_handler", None)
        if handler is None:
            return True  # UI 初始化早期：保守允许，后续 refresh 会校正
        return handler.manga_enabled

    def _set_save_status(self, status):
        if hasattr(self, "save_status_label"):
            self.save_status_label.config(text=f"保存: {status}")

    def get_table_data(self):
        """PERF §7.5 步骤3：从文档模型返回副本，不调用 Tk API。

        保留方法签名以降低调用方改动。模型数据是业务状态的唯一真相来源。
        """
        return self._document.source_lines(), self._document.target_lines()

    # ── 实时保存 ───────────────────────────────────────

    def _schedule_save_to_target(self, delay_ms: int = 1000):
        """PERF §8：调度自动保存（委托给 AutosaveCoordinator）。

        - 默认 debounce 1000ms（人工编辑），最大延迟 5000ms。
        - delay_ms=0 表示立即保存（窗口关闭、停止翻译等关键事件），
          映射到 ``source="flush"`` 跳过 debounce 直接启动。
        - 保存期间的新编辑只增加 document.version，不启动并行写入（单飞）。
        - P0-3/P0-5：dirty 状态独立于 target_path 与 autosave。
          剪贴板会话没有 target_path，但仍需显示"有未保存更改"并进入关闭保护。
        - 只有文档模型实际发生变更时才进入 dirty；拒绝或同值更新不保存。
        """
        if self._table_loading:
            return
        if not self._document.dirty_indices:
            return
        app_config = self.config_manager.get_app_config()
        auto_save_enabled = app_config.get("auto_save", True)
        if getattr(self, "_disable_auto_save", False):
            auto_save_enabled = False
        tgt_path = getattr(self.file_importer, "current_target_path", None)

        # P0-3：所有来源的文档更新（人工编辑、机器翻译、撤销/重做）
        # 都在这里进入会话 dirty 状态。dirty 与 target_path/autosave 开关
        # 无关；否则剪贴板会话的机器翻译结果无法触发关闭保护。
        self._unsaved_edits = True
        session = getattr(getattr(self, "file_importer", None), "session", None)
        if session is not None:
            session.mark_dirty()
        if delay_ms != 0:
            self._set_save_status("有未保存更改" if tgt_path else "有未保存更改（未指定文件）")

        # 无 target_path 时只跟踪 dirty 状态，不触发后台写入。
        if not tgt_path:
            return

        # auto_save=False 时只跟踪 dirty 状态，不触发后台写入
        if not auto_save_enabled:
            return

        # 同步保存路径到协调器（文件导入后路径可能变化）
        self._autosave.set_save_paths(
            tgt_path,
            getattr(self.file_importer, "current_mapping_dir", None),
        )

        if delay_ms == 0:
            # 立即保存：停止/完成/关闭/显式保存
            self._autosave.mark_dirty(source="flush")
        else:
            self._autosave.mark_dirty(source="edit", debounce_ms=delay_ms)

    def _on_save_result(self, result: SaveResult) -> None:
        """PERF §8：保存结果回调（在主线程调用）。

        根据协调器状态更新保存状态标签。``AutosaveCoordinator`` 已完成
        状态转换，这里只负责 UI 展示。
        """
        state = self._autosave.state
        if state == CLEAN:
            self._set_save_status("已保存")
            # P0-5：保存成功时清除未保存标记
            self._unsaved_edits = False
            self._document.clear_dirty()
            session = getattr(getattr(self, "file_importer", None), "session", None)
            if session is not None:
                session.mark_clean()
        elif state == SAVING:
            self._set_save_status("正在保存")
        elif state == SAVE_FAILED:
            self._set_save_status("保存失败")
            if result and result.error_message:
                self.update_status(f"⚠ 自动保存失败: {result.error_message}")
        elif state == DIRTY:
            self._set_save_status("有未保存更改")
        # SAVE_SCHEDULED 视为等待保存
        if state == "SAVE_SCHEDULED":
            self._set_save_status("等待保存")

    @property
    def has_unsaved_changes(self) -> bool:
        """P0-5：检查是否有未保存的更改。

        dirty 独立于 autosave：自动保存关闭时仍能检测未保存状态；
        保存失败时也视为有未保存更改。
        """
        session = getattr(getattr(self, "file_importer", None), "session", None)
        if session is not None and session.dirty:
            return True
        if self._unsaved_edits:
            return True
        state = self._autosave.state
        return state in (DIRTY, SAVE_FAILED, "SAVE_SCHEDULED")

    def flush_pending_save(self, timeout: float = 5.0) -> bool:
        """PERF §8：等待后台保存完成（窗口关闭时调用）。

        委托给 ``AutosaveCoordinator.flush``，超时返回 False 且明确提示，
        绝不假装已保存。
        """
        if self._autosave.state == CLEAN:
            return True
        ok = self._autosave.flush(timeout=timeout)
        if not ok:
            logger.error("等待自动保存超时（%.1fs），可能丢失未保存数据", timeout)
            self.update_status("⚠ 自动保存超时，部分数据可能未保存")
        return ok

    def confirm_save_before_close(self) -> str:
        """P0-5：关闭前检查未保存更改，返回用户选择。

        返回值：
        - "save"：用户选择保存（调用方应执行保存并检查结果）
        - "discard"：用户选择放弃更改
        - "cancel"：用户取消关闭
        - "proceed"：无需保存，直接继续
        """
        if not self.has_unsaved_changes:
            return "proceed"
        result = messagebox.askyesnocancel(
            "未保存的更改",
            "当前文档有未保存的更改。\n\n是否在关闭前保存？",
            icon=messagebox.WARNING,
        )
        if result is None:
            return "cancel"
        if result:
            return "save"
        return "discard"

    def confirm_save_before_replace(self) -> str:
        """Run the Save/Discard/Cancel guard before replacing the session.

        Closing is handled by ``main.py`` and performs the save after asking.
        Imports need a callback that completes the save before returning, so a
        successful ``save`` choice can safely proceed with session replacement.
        """
        choice = self.confirm_save_before_close()
        if choice != "save":
            return choice
        return "proceed" if self.save_and_flush() else "cancel"

    def confirm_stop_translation_before_replace(self) -> bool:
        """Stop an active translation before replacing the document session.

        A new document must never merely invalidate an active run: the old
        request could continue consuming API quota and its missing terminal
        event would keep the UI in a busy state.  The controller performs the
        cancellation and atomically retires the old run before import commits.
        """
        controller = getattr(self, "translation_controller", None)
        if controller is None or not controller.is_translating:
            return True
        should_stop = messagebox.askyesno(
            "翻译正在进行",
            "当前翻译仍在运行。是否停止当前翻译并切换到新文档？\n\n"
            "已完成的译文会保留并触发保存。",
            icon=messagebox.WARNING,
            parent=self.root,
        )
        if not should_stop:
            return False
        return controller.cancel_for_session_replacement()

    def save_and_flush(self) -> bool:
        """P0-5：执行同步保存并等待完成。返回是否成功。"""
        tgt_path = getattr(self.file_importer, "current_target_path", None)
        if not tgt_path:
            # 剪贴板会话没有目标路径时，关闭/切换会话必须允许 Save As，
            # 否则 Save/Discard/Cancel 守卫中的“保存”分支永远失败。
            saved = self.translation_controller.save_translation(synchronous=True)
            return saved and not self.has_unsaved_changes
        self._autosave.set_save_paths(
            tgt_path,
            getattr(self.file_importer, "current_mapping_dir", None),
        )
        self._autosave.mark_dirty(source="flush")
        ok = self.flush_pending_save(timeout=10.0)
        if ok:
            self._mark_clean_after_save()
        if not ok:
            messagebox.showwarning(
                "保存失败",
                "保存超时或失败，请检查文件权限或磁盘空间。",
            )
        return ok

    def _mark_clean_after_save(self) -> None:
        """P0-3：Ctrl+S 保存成功后清除未保存状态。

        - 重置 ``_unsaved_edits`` 让关闭/切项目守卫不再拦截。
        - 调用 ``AutosaveCoordinator.clear_dirty`` 标记模型行为已保存，
          避免下一次 flush 重复写入。
        - 更新保存状态标签为"已保存"。
        """
        self._unsaved_edits = False
        session = getattr(getattr(self, "file_importer", None), "session", None)
        if session is not None:
            session.mark_clean()
        try:
            self._document.clear_dirty()
        except Exception as exc:
            logger.warning("清除 document dirty 标记失败: %s", exc)
        try:
            self._autosave.mark_clean()
        except Exception as exc:
            logger.warning("清除 autosave dirty 状态失败: %s", exc)
        self._set_save_status("已保存")

    def _debounce(self, key, delay_ms, callback):
        attr = f"_debounce_{key}"
        old_id = getattr(self, attr, None)
        if old_id:
            self.root.after_cancel(old_id)
        setattr(self, attr, self.root.after(delay_ms, callback))

    # ── 窗口打开 ──────────────────────────────────────

    def open_settings(self):
        from .settings_window import SettingsWindow

        SettingsWindow(
            self.root,
            self.config_manager,
            self._on_settings_updated,
            edition_capabilities=self.edition_capabilities,
        )

    def open_onboarding(self):
        """帮助 > 新手指导：手动重新打开引导，忽略自动展示条件。"""
        onboarding = getattr(self, "onboarding", None)
        if onboarding is not None:
            onboarding.start(force=True)

    def open_glossary(self):
        from .glossary_window import GlossaryWindow

        GlossaryWindow(self.root, self.config_manager)

    def open_concurrent(self):
        from ..core.concurrent_manager import ConcurrentTranslationManager
        from .concurrent_window import ConcurrentWindow

        # R2-BUG-013：只允许一个队列窗口，避免旧窗口管理器泄漏
        cw = getattr(self, "_concurrent_window", None)
        if cw is not None and not cw._closed:
            try:
                if cw.win.winfo_exists():
                    cw.win.lift()
                    cw.win.focus_force()
                    return
            except Exception:
                pass

        # P1-3：manager 归应用生命周期所有，窗口只订阅快照。
        # 首次打开时懒创建，后续打开复用同一 manager，任务在窗口关闭后继续运行。
        if getattr(self, "_queue_manager", None) is None:
            self._queue_manager = ConcurrentTranslationManager(
                self.config_manager,
                app_paths=self.app_paths,
                limiter_registry=self._provider_limiter_registry,
            )
        self._concurrent_window = ConcurrentWindow(
            self.root,
            self.config_manager,
            app_paths=self.app_paths,
            manager=self._queue_manager,
            edition_capabilities=self.edition_capabilities,
        )
        self._schedule_queue_background_status()

    def _schedule_queue_background_status(self) -> None:
        """Keep background queue work visible after its dedicated window closes."""
        if getattr(self, "_closed", False):
            return
        manager = getattr(self, "_queue_manager", None)
        label = getattr(self, "task_summary_label", None)
        if manager is not None and label is not None:
            try:
                snapshot = manager.get_snapshot()
                if snapshot is not None:
                    active = sum(
                        task.state.value
                        in {
                            "pending",
                            "preparing",
                            "ready",
                            "running",
                            "pause_requested",
                            "finalizing",
                        }
                        for task in snapshot.tasks
                    )
                    if active:
                        completed = sum(task.state.value == "completed" for task in snapshot.tasks)
                        label.config(text=f"后台队列 {active} 个进行中 · 已完成 {completed}")
                    elif not snapshot.tasks:
                        label.config(text="")
            except Exception as exc:
                logger.debug("读取后台队列状态失败: %s", exc)
        if hasattr(self.root, "after"):
            self._queue_status_after_id = self.root.after(500, self._schedule_queue_background_status)

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
        ttk.Label(email_frame, text="996043050@qq.com", font=("微软雅黑", 10)).pack(
            side=tk.LEFT, padx=(5, 10)
        )

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

    # ── P1-UX-2：TXT 队列跨重启恢复 ────────────────────

    def _show_fingerprint_mismatch_dialog(self, info: dict) -> str:
        """P1-UX-2：源文件指纹变化时让用户选择恢复策略。

        回调在 ``ConcurrentTranslationManager.add_task`` 路径同步调用
        （Tk 主线程），可安全弹出对话框。

        Args:
            info: 包含 file_path / old_fingerprint / new_fingerprint /
                old_total_lines / new_total_lines 的字典。

        Returns:
            ``"new"`` / ``"map"`` / ``"discard"`` 之一。
        """
        from tkinter import messagebox as _msgbox
        from pathlib import Path

        file_name = Path(str(info.get("file_path", ""))).name
        old_lines = info.get("old_total_lines", "?")
        new_lines = info.get("new_total_lines", "?")
        message = (
            f"文件 {file_name} 的内容已变化：\n\n"
            f"- 旧版本行数：{old_lines}\n"
            f"- 新版本行数：{new_lines}\n\n"
            "选择恢复策略：\n"
            "- 是：尝试映射旧进度（行数相同时复用译文，行数不同会退化为新建）\n"
            "- 否：新建任务（不恢复任何旧进度）\n"
            "- 取消：放弃旧进度并删除旧项目文件"
        )
        choice = _msgbox.askyesnocancel(
            "源文件已变化",
            message,
            icon=_msgbox.WARNING,
            parent=self.root,
        )
        if choice is None:
            return "discard"
        if choice:
            return "map"
        return "new"

    # ── 状态更新 ──────────────────────────────────────

    def _on_settings_updated(self, refresh_engine=True):
        if not refresh_engine:
            self.api_status_label.config(text="API: 正在检查")
            self.model_label.config(text="正在读取配置")
            self.setup_table_styles()
            self.refresh_action_state()
            self._start_api_status_load()
            return

        # P2-2：设置保存后同步刷新 named font，让“界面字号”全局生效，
        # 不再只更新 Treeview。
        self.setup_table_styles()
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
            self.model_label.config(
                text=f"{self.config_manager.get_app_config().get('target_language', '中文')} / {model_name}"
            )
        else:
            self.api_status_label.config(text="API: 未配置")
            self.model_label.config(text="请配置 API")
        self.setup_table_styles()
        if hasattr(self, "translate_btn"):
            self.refresh_action_state()

        # 新手指导：自动展示仅在首次 API 状态返回后评估一次，
        # 避免在 Tk 主线程启动阶段同步读取系统密钥环。
        onboarding = getattr(self, "onboarding", None)
        if onboarding is not None:
            if not self._onboarding_auto_evaluated:
                self._onboarding_auto_evaluated = True
                has_recent = bool(self.config_manager.get_app_config().get("recent_files"))
                onboarding.maybe_start(
                    api_configured=self._api_configured,
                    has_recent_files=has_recent,
                )
            onboarding.notify("api_status_changed", configured=self._api_configured)

    def update_image_progress(self, text):
        self.image_progress_label.config(text=text)

    def _set_image_translation_busy(self, busy: bool) -> None:
        self._image_translation_busy = bool(busy)
        self.refresh_action_state()

    def update_status(self, message):
        self.status_label.config(text=message)

    def close(self, decision: str = "proceed"):
        """BUG-005：主窗口关闭时停止翻译、关闭所有引擎，释放 API 资源。

        P0-1：``decision`` 来自关闭守卫的 Save/Discard/Cancel/Proceed 选择。
        - ``"save"`` / ``"proceed"``：等待后台保存完成（flush 语义）。
        - ``"discard"``：放弃未保存的更改，不启动新保存；若已有 SAVING 在执行，
          仍等待其完成（无法安全取消磁盘写入），完成后置 CLEAN 而不调度新保存。
        - ``"cancel"``：理论上不会到达此方法（调用方已 return），保留参数
          仅用于完整性，按 ``"proceed"`` 处理。
        """
        self._closed = True
        if self._api_status_after_id is not None:
            try:
                self.root.after_cancel(self._api_status_after_id)
            except Exception:
                pass
            self._api_status_after_id = None
        if self._queue_status_after_id is not None:
            try:
                self.root.after_cancel(self._queue_status_after_id)
            except Exception:
                pass
            self._queue_status_after_id = None

        if self._table_load_after_id is not None:
            try:
                self.root.after_cancel(self._table_load_after_id)
            except Exception:
                pass
            self._table_load_after_id = None
            self._table_load_generation += 1

        # 新手指导：关闭面板并解除快捷键绑定，避免残留 after 回调
        onboarding = getattr(self, "onboarding", None)
        if onboarding is not None:
            try:
                onboarding.close()
            except Exception as e:
                logger.warning("关闭新手指导失败: %s", e)

        # 1. 停止主翻译引擎
        try:
            self.translator.call_if_initialized("stop")
        except Exception as e:
            logger.warning("停止翻译引擎失败: %s", e)

        # PERF：关闭翻译控制器的事件泵并使当前 run_id 失效，
        # 避免残留 after 回调和迟到事件污染（见 §6.6 步骤 8）。
        try:
            self.translation_controller.close()
        except Exception as e:
            logger.warning("关闭翻译控制器失败: %s", e)

        # 2. 关闭队列翻译管理器（如有）
        # P1-3：manager 归应用生命周期所有，应用退出时真正停止任务。
        # 先关闭队列窗口（销毁 UI），再关闭 manager（停止任务、释放资源）。
        # R2-BUG-013：窗口可能已通过 WM_DELETE_WINDOW 关闭，需检查 _closed 标志
        cw = getattr(self, "_concurrent_window", None)
        if cw is not None and not cw._closed:
            try:
                cw._on_close()
            except Exception as e:
                logger.warning("关闭队列窗口失败: %s", e)
        # P1-3：关闭应用级 manager（真正停止后台任务）
        queue_manager = getattr(self, "_queue_manager", None)
        if queue_manager is not None:
            try:
                queue_manager.close()
            except Exception as e:
                logger.warning("关闭队列翻译管理器失败: %s", e)
            finally:
                self._queue_manager = None

        # 3. 关闭主翻译引擎的 API 资源
        try:
            self.translator.call_if_initialized("close")
        except Exception as e:
            logger.warning("关闭翻译引擎API失败: %s", e)
        try:
            self._provider_limiter_registry.close_all()
        except Exception as e:
            logger.warning("关闭共享翻译限流器失败: %s", e)

        # 4. 关闭图片翻译 Provider（包括本地模型运行时）
        try:
            self.image_handler.close()
        except Exception as e:
            logger.warning("关闭图片翻译 Provider 失败: %s", e)

        # P1-1：关闭文件导入控制器的 UI 回调事件泵
        try:
            self.file_importer.close()
        except Exception as e:
            logger.warning("关闭文件导入 UI 事件泵失败: %s", e)

        # PERF §8：等待后台保存完成（generation 状态机 flush），避免关闭时丢失数据。
        # P0-1：根据用户在关闭守卫中的选择分支：
        # - discard：放弃未保存的更改，不启动新保存；仅等待已在途的 SAVING 完成。
        # - save/proceed：等待后台保存完成（包括按需启动新一轮保存）。
        if decision == "discard":
            try:
                ok = self._autosave.discard_pending(timeout=5.0)
            except Exception as e:
                logger.warning("放弃未保存更改失败: %s", e)
                ok = False
            if not ok:
                logger.error("放弃更改时仍有未完成的保存，可能残留写入")
                self.update_status("⚠ 仍有保存未完成，部分更改可能已写入")
        else:
            try:
                self.flush_pending_save(timeout=5.0)
            except Exception as e:
                logger.warning("等待后台保存完成失败: %s", e)
        try:
            self._autosave.close()
        except Exception as e:
            logger.warning("关闭自动保存协调器失败: %s", e)
