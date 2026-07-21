#!/usr/bin/env python3
"""
设置窗口模块
"""

import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from ..config.image_ocr import SILICONFLOW_OCR_DEFAULT_MODEL
from ..config.translation_profile import (
    DEEPSEEK_V4_FLASH_MODEL,
    DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY,
    DEFAULT_QUEUE_HARD_REQUEST_CAP,
    DEFAULT_QUEUE_MAX_ACTIVE_TASKS,
    DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS,
    DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT,
    DEFAULT_QUEUE_RPM_LIMIT,
    DEFAULT_QUEUE_TPM_LIMIT,
    DEFAULT_QUEUE_TRANSLATION_BATCH_LINES,
    DEFAULT_TRANSLATION_BATCH_LINES,
    DEFAULT_TRANSLATION_CONCURRENCY,
    MAX_QUEUE_HARD_REQUEST_CAP,
    MAX_QUEUE_MAX_ACTIVE_TASKS,
    MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS,
    MAX_QUEUE_PER_TASK_SOFT_LIMIT,
    MAX_QUEUE_RPM_LIMIT,
    MAX_QUEUE_TPM_LIMIT,
    MAX_QUEUE_TRANSLATION_BATCH_LINES,
    MAX_STABLE_TRANSLATION_BATCH_LINES,
    OPENAI_COMPATIBLE_PROVIDER,
    SILICONFLOW_DEEPSEEK_V32_MODEL,
    apply_text_translation_profile,
    default_base_url_for_provider,
    normalize_openai_base_url,
)
from ..config.volcengine_image import (
    VOLCENGINE_IMAGE_DEFAULT_BASE_URL,
    VOLCENGINE_IMAGE_DEFAULT_MODEL,
    VOLCENGINE_IMAGE_MODEL_SUGGESTIONS,
)
from .form_validation import FormValidator
from .theme import COLORS
from .ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox
from .window_geometry import WindowGeometryTracker


class SettingsWindow:
    # 默认使用接近主窗口的大视图，减少首次打开设置页时的滚动。
    # 最小尺寸单独保留，用户仍可按需缩小窗口。
    WINDOW_WIDTH = 960
    WINDOW_HEIGHT = 840
    MIN_WINDOW_WIDTH = 600
    MIN_WINDOW_HEIGHT = 560
    SCREEN_MARGIN = 32

    def __init__(
        self,
        parent,
        config_manager,
        callback=None,
    ):
        self.parent = parent
        self.config_manager = config_manager
        self.callback = callback
        # P2-5：表单字段级校验器 + testing busy 状态（防止连接测试/模块检测
        # 重复启动，避免用户在测试中误触发多次付费请求）。
        self._form_validator = FormValidator()
        self._test_in_progress = False
        self._test_buttons: list = []

        self.provider_display_map = {
            "siliconflow": "SiliconFlow",
            "deepseek": "DeepSeek",
            OPENAI_COMPATIBLE_PROVIDER: "自定义 OpenAI 兼容服务",
        }
        self.display_to_provider_map = {
            display: provider for provider, display in self.provider_display_map.items()
        }

        # 模型名称映射：完整名称 -> 显示名称
        self.model_display_map = {
            # SiliconFlow 模型
            "tencent/Hunyuan-MT-7B": "Hunyuan MT 7B（免费）",
            SILICONFLOW_DEEPSEEK_V32_MODEL: "DeepSeek V3.2（流式）",
            "deepseek-ai/DeepSeek-V4-Flash": "DeepSeek V4 Flash（性价比高）",
            # DeepSeek 官方模型
            DEEPSEEK_V4_FLASH_MODEL: "DeepSeek V4 Flash",
        }

        # 反向映射：显示名称 -> 完整名称
        self.display_to_model_map = {v: k for k, v in self.model_display_map.items()}

        # 创建设置窗口
        self.window = tk.Toplevel(parent)
        self.window.title("设置")
        # P1-7：允许缩放，配合各 tab 的页内滚动避免高 DPI/小屏裁切
        self.window.resizable(True, True)
        # P2-3：minsize 在 center_window 中按工作区收敛，不在 __init__ 提前钉死。
        self.window.transient(parent)
        self.window.grab_set()

        self._window_geometry_tracker = WindowGeometryTracker(
            self.window,
            config_manager,
            "settings",
            default_size=(self.WINDOW_WIDTH, self.WINDOW_HEIGHT),
            minimum_size=(self.MIN_WINDOW_WIDTH, self.MIN_WINDOW_HEIGHT),
            screen_margin=self.SCREEN_MARGIN,
            center=True,
        )

        # P1-1：UI 回调邮箱 + Tk 主线程事件泵
        # 工作线程只调用 ``_ui_mailbox.submit(func)``，不再直接调用 ``window.after()``
        # 或 ``winfo_exists()``（跨线程 Tk 调用）。
        self._ui_mailbox = UICallbackMailbox()
        self._ui_pump = TkUICallbackPump(self.window, self._ui_mailbox)
        self._ui_pump.start()
        # 窗口销毁时关闭 pump（绑定 <Destroy> 覆盖所有销毁路径）
        self.window.bind("<Destroy>", self._on_window_destroy)

        # 居中显示
        self.center_window()
        self._window_geometry_tracker.bind()

        # 加载当前配置
        self.api_config = config_manager.get_api_config()
        self.app_config = config_manager.get_app_config()

        self.setup_ui()

        # P2-1：模态窗口补 Escape 关闭，关闭后焦点回到父窗口
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Destroy>", self._on_window_destroy_restore_focus, add="+")

    def _on_window_destroy_restore_focus(self, event: tk.Event) -> None:
        """P2-1：窗口销毁后把焦点还给父窗口，便于键盘用户继续操作。"""
        if event.widget is not self.window:
            return
        parent = self.parent
        try:
            if parent is not None and parent.winfo_exists():
                parent.focus_set()
        except tk.TclError:
            pass

    def _on_window_destroy(self, event):
        """P1-1：窗口销毁时关闭 UI 回调事件泵。幂等。"""
        if event.widget is self.window:
            tracker = getattr(self, "_window_geometry_tracker", None)
            if tracker is not None:
                tracker.save(persist=True)
            self.close_ui_pump()

    def close_ui_pump(self):
        """P1-1：关闭 UI 回调事件泵，释放 ``after`` 调度。幂等。"""
        pump = getattr(self, "_ui_pump", None)
        if pump is not None:
            pump.close()
            self._ui_pump = None

    # ── P2-5：表单校验与 testing busy 状态 ──────────────────────

    def _register_spin(
        self,
        spin: ttk.Widget,
        var: tk.IntVar,
        lo: int,
        hi: int,
        name: str,
        label: str,
    ) -> None:
        """注册一个整数 Spinbox 字段到表单校验器，并绑定 ``<FocusOut>`` 自动收敛。

        P2-5：``ttk.Spinbox`` 允许用户输入任意文本，原实现只在 ``save_settings``
        时统一弹通用错误。改为字段级校验：用户离开字段时立即把非法值收敛到
        ``[lo, hi]``，并在保存前再次统一校验，聚焦首个错误字段。
        """
        spec = self._form_validator.register_int(name, label, var, lo, hi, widget=spin)
        self._form_validator.attach_focus_out_clamp(spec)

    def _validate_form(self) -> tuple[bool, str, object | None]:
        """P2-5：整表校验。返回 ``(ok, first_message, first_failed_widget)``。"""
        result = self._form_validator.validate_all()
        return result.ok, result.first_message or "", result.first_failed_widget

    def _set_testing_busy(self, busy: bool) -> None:
        """P2-5：testing busy 状态。testing 期间禁用所有测试按钮，避免重复启动。

        连接测试、模块检测和火山测试都可能触发网络请求或付费调用；并发
        启动会让用户难以判断哪次结果对应哪次点击，也可能造成多次扣费。
        """
        self._test_in_progress = busy
        state = "disabled" if busy else "normal"
        for btn in self._test_buttons:
            try:
                btn.configure(state=state)
            except (tk.TclError, AttributeError):
                pass

    def _begin_test(self) -> bool:
        """P2-5：尝试开始一次测试。若已有测试在进行，弹提示并返回 False。"""
        if self._test_in_progress:
            try:
                messagebox.showinfo(
                    "测试进行中",
                    "上一次测试尚未结束，请稍候再试。",
                    parent=self.window,
                )
            except tk.TclError:
                pass
            return False
        self._set_testing_busy(True)
        return True

    def _end_test(self) -> None:
        """P2-5：测试结束，恢复测试按钮可用状态。"""
        self._set_testing_busy(False)

    def center_window(self):
        """窗口居中显示。

        默认尽量展示完整设置内容，同时让 ``minsize`` 服从可用工作区。在
        800x600、高 DPI 缩放或小屏环境下，窗口尺寸和最小尺寸都会收敛到
        “屏幕尺寸 - 边距”，避免确认按钮被挤出可视范围。
        """
        tracker = getattr(self, "_window_geometry_tracker", None)
        if tracker is None:
            tracker = WindowGeometryTracker(
                self.window,
                getattr(self, "config_manager", None),
                "settings",
                default_size=(self.WINDOW_WIDTH, self.WINDOW_HEIGHT),
                minimum_size=(self.MIN_WINDOW_WIDTH, self.MIN_WINDOW_HEIGHT),
                screen_margin=self.SCREEN_MARGIN,
                center=True,
            )
            self._window_geometry_tracker = tracker
        tracker.restore()

    def _safe_after(self, func):
        """P1-1：通过 UI 回调邮箱提交回调，不在工作线程调用 Tk API。

        回调执行时若窗口已销毁，pump 的异常处理器会捕获并记录
        ``TclError``，不会中断后续回调。
        """
        self._ui_mailbox.submit(func)

    def setup_ui(self):
        """设置界面"""
        # P2-5：兼容 ``SettingsWindow.__new__`` 跳过 ``__init__`` 的测试路径，
        # 确保 testing busy 状态相关属性存在。
        if not hasattr(self, "_test_buttons"):
            self._test_buttons = []
            self._test_in_progress = False
            self._form_validator = FormValidator()
        # 先预留底部操作区，避免高 DPI 或较小屏幕把确认按钮挤出窗口。
        button_frame = ttk.Frame(self.window)
        button_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 12))

        # P2-5：测试连接按钮纳入 busy 列表，testing 期间禁用避免重复启动。
        self.test_connection_btn = ttk.Button(
            button_frame, text="测试连接", command=self.test_connection
        )
        self.test_connection_btn.pack(side=tk.LEFT)
        self._test_buttons.append(self.test_connection_btn)
        self.confirm_button = ttk.Button(button_frame, text="确定", command=self.save_settings)
        self.confirm_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(button_frame, text="取消", command=self.window.destroy).pack(side=tk.RIGHT)

        # 创建笔记本控件
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 8))

        # API配置页面
        self.create_api_tab(notebook)

        # 翻译设置页面
        self.create_translation_tab(notebook)

        # 图生图配置页面 -> 插图翻译配置页面
        self.create_volc_tab(notebook)

        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def _create_scrollable_tab(self, notebook, title):
        """创建固定视口的设置页，内容过长时仅在页内滚动。

        P2-3：补齐滚轮、PageUp/PageDown 和 FocusIn 自动滚入视区，避免只能
        拖动滚动条。鼠标悬停在 canvas 上时滚轮生效；键盘 PageUp/PageDown
        在 canvas 获得焦点时生效；Tab 遍历到的 Entry/Spinbox 若被遮挡，
        通过 ``see`` 把对应 widget 滚入可视区。
        """
        tab = ttk.Frame(notebook)
        notebook.add(tab, text=title)

        background = ttk.Style().lookup("TFrame", "background")
        canvas = tk.Canvas(
            tab,
            borderwidth=0,
            highlightthickness=0,
            background=background or self.window.cget("background"),
        )
        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=canvas.yview)
        content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=content, anchor=tk.NW)

        resize_after_id = None
        pending_width = None

        def _refresh_canvas_layout() -> None:
            nonlocal resize_after_id
            resize_after_id = None
            try:
                if pending_width is not None:
                    canvas.itemconfigure(content_window, width=pending_width)
                canvas.configure(scrollregion=canvas.bbox("all"))
            except tk.TclError:
                pass

        def _schedule_canvas_layout(width=None) -> None:
            nonlocal pending_width, resize_after_id
            if width is not None:
                pending_width = width
            if resize_after_id is not None:
                try:
                    canvas.after_cancel(resize_after_id)
                except tk.TclError:
                    pass
            resize_after_id = canvas.after(45, _refresh_canvas_layout)

        content.bind("<Configure>", lambda _event: _schedule_canvas_layout())
        canvas.bind("<Configure>", lambda event: _schedule_canvas_layout(event.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._bind_scrollable_canvas_events(canvas, content)
        return content

    def _bind_scrollable_canvas_events(self, canvas: tk.Canvas, content: ttk.Frame) -> None:
        """P2-3：为可滚动 canvas 绑定滚轮、键盘翻页和 FocusIn 自动定位。"""

        def _scroll_by(delta: int) -> None:
            """按像素增量滚动；delta 正向下、负向上。"""
            try:
                canvas.yview_scroll(int(delta), "units")
            except tk.TclError:
                pass

        def _on_mouse_wheel(event: tk.Event) -> None:
            # Windows: event.delta 是 120 的倍数；macOS: 也是 120 倍数但符号相反
            step = -1 * (event.delta // 120)
            _scroll_by(step)

        def _on_linux_wheel_up(_event: tk.Event) -> str:
            _scroll_by(-1)
            return "break"

        def _on_linux_wheel_down(_event: tk.Event) -> str:
            _scroll_by(1)
            return "break"

        def _on_key_page_up(_event: tk.Event) -> None:
            _scroll_by(-2)

        def _on_key_page_down(_event: tk.Event) -> None:
            _scroll_by(2)

        def _on_focus_in(event: tk.Event) -> None:
            """聚焦的 widget 若被遮挡，滚动使其可见。"""
            widget = event.widget
            try:
                bbox = canvas.bbox("all")
                if not bbox:
                    return
                widget_y = widget.winfo_y()
                widget_h = widget.winfo_height()
                canvas_h = canvas.winfo_height()
                yview = canvas.yview()
                top = yview[0]
                bottom = yview[1]
                content_h = bbox[3]
                if content_h <= canvas_h:
                    return
                # widget 在 content 中的相对位置 (0..1)
                widget_top_ratio = widget_y / max(1, content_h)
                widget_bottom_ratio = (widget_y + widget_h) / max(1, content_h)
                if widget_top_ratio < top:
                    canvas.yview_moveto(widget_top_ratio)
                elif widget_bottom_ratio > bottom:
                    # 让 widget 底部刚好出现在 canvas 底部
                    new_top = max(0.0, widget_bottom_ratio - (canvas_h / max(1, content_h)))
                    canvas.yview_moveto(new_top)
            except (tk.TclError, AttributeError, ZeroDivisionError):
                pass

        # 滚轮：绑定到 canvas 和所有子 widget，使鼠标悬停时即可滚动
        for widget in (canvas, content):
            widget.bind("<MouseWheel>", _on_mouse_wheel, add="+")
            widget.bind("<Shift-MouseWheel>", _on_mouse_wheel, add="+")
            widget.bind("<Button-4>", _on_linux_wheel_up, add="+")
            widget.bind("<Button-5>", _on_linux_wheel_down, add="+")

        # 键盘翻页：canvas 需可获得焦点
        try:
            canvas.configure(takefocus=True)
        except tk.TclError:
            pass
        canvas.bind("<Prior>", _on_key_page_up, add="+")
        canvas.bind("<Next>", _on_key_page_down, add="+")

        # FocusIn：Tab 遍历到被遮挡的 Entry/Spinbox 时自动滚入视区
        content.bind("<FocusIn>", _on_focus_in, add="+")

    def create_api_tab(self, notebook):
        """创建API配置页面"""
        # P1-7：使用页内滚动容器，避免高 DPI 或缩放后内容被裁切
        api_frame = self._create_scrollable_tab(notebook, "API配置")
        # 响应式布局：列 1（输入控件列）随窗口宽度伸缩，列 0 和列 2 保持自然宽度
        api_frame.grid_columnconfigure(1, weight=1)

        # API提供商
        ttk.Label(api_frame, text="API提供商:").grid(row=0, column=0, sticky=tk.W, padx=10, pady=10)
        current_provider = self.api_config.get("provider", "siliconflow")
        if current_provider not in self.provider_display_map:
            current_provider = "siliconflow"
        self.provider_var = tk.StringVar(value=current_provider)
        self.provider_display_var = tk.StringVar(value=self.provider_display_map[current_provider])
        provider_combo = ttk.Combobox(
            api_frame,
            textvariable=self.provider_display_var,
            values=list(self.provider_display_map.values()),
            state="readonly",
            width=25,
        )
        provider_combo.grid(row=0, column=1, padx=10, pady=10, sticky=tk.EW)
        provider_combo.bind("<<ComboboxSelected>>", self.on_provider_changed)
        ttk.Button(
            api_frame,
            text="添加自定义服务",
            command=self.open_custom_api_dialog,
        ).grid(row=0, column=2, padx=(0, 10), pady=10, sticky=tk.W)

        # API密钥
        ttk.Label(api_frame, text="API密钥:").grid(row=1, column=0, sticky=tk.W, padx=10, pady=10)
        self.api_key_var = tk.StringVar(value=self.api_config.get("api_key", ""))
        api_key_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, show="*", width=25)
        api_key_entry.grid(row=1, column=1, padx=10, pady=10, sticky=tk.EW)

        # API预设管理按钮
        preset_frame = ttk.Frame(api_frame)
        preset_frame.grid(row=1, column=2, padx=5, pady=10)
        ttk.Button(preset_frame, text="保存预设", width=8, command=self.save_api_preset).pack(
            side=tk.TOP, pady=(0, 2)
        )
        ttk.Button(preset_frame, text="加载预设", width=8, command=self.load_api_preset).pack(
            side=tk.TOP
        )

        # 基础URL
        ttk.Label(api_frame, text="基础URL:").grid(row=2, column=0, sticky=tk.W, padx=10, pady=10)
        self.base_url_var = tk.StringVar(
            value=self.api_config.get("base_url", "https://api.siliconflow.cn/v1")
        )
        base_url_entry = ttk.Entry(api_frame, textvariable=self.base_url_var, width=35)
        base_url_entry.grid(row=2, column=1, padx=10, pady=10, sticky=tk.EW)

        # 模型名称
        ttk.Label(api_frame, text="模型名称:").grid(row=3, column=0, sticky=tk.W, padx=10, pady=10)

        # 获取当前模型名称并转换为显示名称
        current_model = self.api_config.get("model_name", SILICONFLOW_DEEPSEEK_V32_MODEL)
        current_display = self.model_display_map.get(current_model, current_model)

        self.model_var = tk.StringVar(value=current_display)

        # 根据提供商设置模型选项
        self.siliconflow_models = [
            "Hunyuan MT 7B（免费）",
            "DeepSeek V3.2（流式）",
            "DeepSeek V4 Flash（性价比高）",
        ]

        self.deepseek_models = [
            "DeepSeek V4 Flash",
        ]

        # 根据当前提供商选择模型列表
        current_provider = self.provider_var.get()
        if current_provider == "deepseek":
            model_display_values = self.deepseek_models
            model_state = "readonly"
        elif current_provider == OPENAI_COMPATIBLE_PROVIDER:
            model_display_values = []
            model_state = "normal"
        else:
            model_display_values = self.siliconflow_models
            model_state = "readonly"

        self.model_combo = ttk.Combobox(
            api_frame,
            textvariable=self.model_var,
            values=model_display_values,
            width=30,
            state=model_state,
        )
        self.model_combo.grid(row=3, column=1, padx=10, pady=10, sticky=tk.EW)

        # 温度参数
        ttk.Label(api_frame, text="温度参数:").grid(row=4, column=0, sticky=tk.W, padx=10, pady=10)
        self.temperature_var = tk.DoubleVar(value=self.api_config.get("temperature", 0.3))
        temperature_scale = ttk.Scale(
            api_frame,
            from_=0.0,
            to=1.0,
            variable=self.temperature_var,
            orient=tk.HORIZONTAL,
        )
        temperature_scale.grid(row=4, column=1, padx=10, pady=10, sticky=tk.EW)

        # 温度值显示
        self.temp_label = ttk.Label(api_frame, text=f"{self.temperature_var.get():.1f}")
        self.temp_label.grid(row=4, column=2, padx=5, pady=10)
        temperature_scale.configure(command=self.update_temperature_label)

    def create_translation_tab(self, notebook):
        """创建翻译设置页面"""
        trans_frame = self._create_scrollable_tab(notebook, "翻译设置")
        # 响应式布局：列 1（输入控件列）随窗口宽度伸缩
        trans_frame.grid_columnconfigure(1, weight=1)

        # 添加提示信息
        tip_label = ttk.Label(
            trans_frame,
            text="建议使用文本库功能确定人名、地名等专有名词，这将显著提升翻译效果！",
            foreground="#0066CC",
            font=("TkDefaultFont", 9),
        )
        tip_label.grid(row=0, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=(10, 5))
        tip_label.bind("<Configure>", lambda e: tip_label.configure(wraplength=e.width - 4))

        # 目标语言
        ttk.Label(trans_frame, text="目标语言:").grid(
            row=1, column=0, sticky=tk.W, padx=10, pady=10
        )
        self.target_lang_var = tk.StringVar(value=self.app_config.get("target_language", "中文"))
        lang_combo = ttk.Combobox(
            trans_frame,
            textvariable=self.target_lang_var,
            values=["中文", "英文", "日文", "韩文", "法文", "德文", "西班牙文"],
            width=30,
        )
        lang_combo.grid(row=1, column=1, padx=10, pady=10, sticky=tk.EW)

        # 批次翻译行数
        ttk.Label(trans_frame, text="批次翻译行数:").grid(
            row=2, column=0, sticky=tk.W, padx=10, pady=10
        )
        self.batch_lines_var = tk.IntVar(
            value=self.app_config.get("batch_lines", DEFAULT_TRANSLATION_BATCH_LINES)
        )
        batch_spin = ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_STABLE_TRANSLATION_BATCH_LINES,
            increment=1,
            textvariable=self.batch_lines_var,
            width=33,
        )
        batch_spin.grid(row=2, column=1, padx=10, pady=10, sticky=tk.EW)
        self._register_spin(
            batch_spin,
            self.batch_lines_var,
            1,
            MAX_STABLE_TRANSLATION_BATCH_LINES,
            "batch_lines",
            "批次翻译行数",
        )

        ttk.Label(trans_frame, text="主界面局部并发:").grid(
            row=3, column=0, sticky=tk.W, padx=10, pady=8
        )
        self.translation_concurrency_var = tk.IntVar(
            value=self.app_config.get("translation_concurrency", DEFAULT_TRANSLATION_CONCURRENCY)
        )
        concurrency_spin = ttk.Spinbox(
            trans_frame,
            from_=1,
            to=8,
            textvariable=self.translation_concurrency_var,
            width=33,
        )
        concurrency_spin.grid(row=3, column=1, padx=10, pady=8, sticky=tk.EW)
        self._register_spin(
            concurrency_spin,
            self.translation_concurrency_var,
            1,
            8,
            "translation_concurrency",
            "主界面局部并发",
        )

        # 自动保存
        self.auto_save_var = tk.BooleanVar(value=self.app_config.get("auto_save", True))
        auto_save_check = ttk.Checkbutton(
            trans_frame, text="启用自动保存", variable=self.auto_save_var
        )
        auto_save_check.grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=10, pady=8)

        ttk.Label(trans_frame, text="界面字号:").grid(row=5, column=0, sticky=tk.W, padx=10, pady=8)
        self.ui_font_size_var = tk.IntVar(value=self.app_config.get("ui_font_size", 10))
        font_size_spin = ttk.Spinbox(
            trans_frame, from_=8, to=18, textvariable=self.ui_font_size_var, width=33
        )
        font_size_spin.grid(row=5, column=1, padx=10, pady=8, sticky=tk.EW)
        self._register_spin(
            font_size_spin,
            self.ui_font_size_var,
            8,
            18,
            "ui_font_size",
            "界面字号",
        )

        # 翻译提示词
        ttk.Label(trans_frame, text="翻译提示词:").grid(
            row=6, column=0, sticky=tk.NW, padx=10, pady=8
        )

        prompt_frame = ttk.Frame(trans_frame)
        prompt_frame.grid(row=6, column=1, padx=10, pady=8, sticky=tk.EW)

        self.prompt_text = tk.Text(prompt_frame, height=10, width=40, wrap=tk.WORD)
        prompt_scroll = ttk.Scrollbar(
            prompt_frame, orient=tk.VERTICAL, command=self.prompt_text.yview
        )
        self.prompt_text.configure(yscrollcommand=prompt_scroll.set)

        self.prompt_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        prompt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 插入当前提示词
        self.prompt_text.insert(1.0, self.app_config.get("translation_prompt", ""))

        # 重置按钮
        ttk.Button(trans_frame, text="重置为默认", command=self.reset_prompt).grid(
            row=7, column=1, padx=10, pady=8, sticky=tk.E
        )

        # 队列翻译并发优化阶段 1：队列翻译调度参数（QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §3）
        # 这些参数构造 QueuePolicy，影响全局公平调度器和共享 ProviderLimiter。
        queue_sep = ttk.Separator(trans_frame, orient=tk.HORIZONTAL)
        queue_sep.grid(row=8, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=(10, 4))
        ttk.Label(
            trans_frame,
            text="文本翻译全局调度（主界面 + 后台队列）",
            font=("TkDefaultFont", 9, "bold"),
        ).grid(row=9, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(0, 4))

        # 最大在途请求数（ProviderLimiter configured_max）
        ttk.Label(trans_frame, text="全局最大在途请求:").grid(
            row=10, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_max_in_flight_var = tk.IntVar(
            value=self.app_config.get(
                "queue_max_in_flight_requests", DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS
            )
        )
        q_max_in_flight_spin = ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS,
            increment=1,
            textvariable=self.queue_max_in_flight_var,
            width=33,
        )
        q_max_in_flight_spin.grid(row=10, column=1, padx=10, pady=4, sticky=tk.EW)
        self._register_spin(
            q_max_in_flight_spin,
            self.queue_max_in_flight_var,
            1,
            MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS,
            "queue_max_in_flight",
            "全局最大在途请求",
        )

        # 硬上限（ThreadPoolExecutor max_workers）
        ttk.Label(trans_frame, text="硬并发上限:").grid(
            row=11, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_hard_cap_var = tk.IntVar(
            value=self.app_config.get("queue_hard_request_cap", DEFAULT_QUEUE_HARD_REQUEST_CAP)
        )
        q_hard_cap_spin = ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_HARD_REQUEST_CAP,
            increment=1,
            textvariable=self.queue_hard_cap_var,
            width=33,
        )
        q_hard_cap_spin.grid(row=11, column=1, padx=10, pady=4, sticky=tk.EW)
        self._register_spin(
            q_hard_cap_spin,
            self.queue_hard_cap_var,
            1,
            MAX_QUEUE_HARD_REQUEST_CAP,
            "queue_hard_cap",
            "硬并发上限",
        )

        # 最大活跃任务数
        ttk.Label(trans_frame, text="最大活跃任务:").grid(
            row=12, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_max_active_var = tk.IntVar(
            value=self.app_config.get("queue_max_active_tasks", DEFAULT_QUEUE_MAX_ACTIVE_TASKS)
        )
        q_max_active_spin = ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_MAX_ACTIVE_TASKS,
            increment=1,
            textvariable=self.queue_max_active_var,
            width=33,
        )
        q_max_active_spin.grid(row=12, column=1, padx=10, pady=4, sticky=tk.EW)
        self._register_spin(
            q_max_active_spin,
            self.queue_max_active_var,
            1,
            MAX_QUEUE_MAX_ACTIVE_TASKS,
            "queue_max_active",
            "最大活跃任务",
        )

        # 每任务软上限（round-robin 第一轮）
        ttk.Label(trans_frame, text="每任务软上限:").grid(
            row=13, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_per_task_soft_var = tk.IntVar(
            value=self.app_config.get(
                "queue_per_task_soft_limit", DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT
            )
        )
        q_per_task_spin = ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_PER_TASK_SOFT_LIMIT,
            increment=1,
            textvariable=self.queue_per_task_soft_var,
            width=33,
        )
        q_per_task_spin.grid(row=13, column=1, padx=10, pady=4, sticky=tk.EW)
        self._register_spin(
            q_per_task_spin,
            self.queue_per_task_soft_var,
            1,
            MAX_QUEUE_PER_TASK_SOFT_LIMIT,
            "queue_per_task_soft",
            "每任务软上限",
        )

        # 队列批次行数
        ttk.Label(trans_frame, text="队列批次行数:").grid(
            row=14, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_batch_lines_var = tk.IntVar(
            value=self.app_config.get("queue_batch_lines", DEFAULT_QUEUE_TRANSLATION_BATCH_LINES)
        )
        q_batch_lines_spin = ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_TRANSLATION_BATCH_LINES,
            increment=1,
            textvariable=self.queue_batch_lines_var,
            width=33,
        )
        q_batch_lines_spin.grid(row=14, column=1, padx=10, pady=4, sticky=tk.EW)
        self._register_spin(
            q_batch_lines_spin,
            self.queue_batch_lines_var,
            1,
            MAX_QUEUE_TRANSLATION_BATCH_LINES,
            "queue_batch_lines",
            "队列批次行数",
        )

        # RPM 限制
        ttk.Label(trans_frame, text="RPM 限制 (0=不限):").grid(
            row=15, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_rpm_var = tk.IntVar(
            value=self.app_config.get("queue_rpm_limit", DEFAULT_QUEUE_RPM_LIMIT)
        )
        q_rpm_spin = ttk.Spinbox(
            trans_frame,
            from_=0,
            to=MAX_QUEUE_RPM_LIMIT,
            increment=10,
            textvariable=self.queue_rpm_var,
            width=33,
        )
        q_rpm_spin.grid(row=15, column=1, padx=10, pady=4, sticky=tk.EW)
        self._register_spin(
            q_rpm_spin,
            self.queue_rpm_var,
            0,
            MAX_QUEUE_RPM_LIMIT,
            "queue_rpm",
            "RPM 限制",
        )

        # TPM 限制
        ttk.Label(trans_frame, text="TPM 限制 (0=不限):").grid(
            row=16, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_tpm_var = tk.IntVar(
            value=self.app_config.get("queue_tpm_limit", DEFAULT_QUEUE_TPM_LIMIT)
        )
        q_tpm_spin = ttk.Spinbox(
            trans_frame,
            from_=0,
            to=MAX_QUEUE_TPM_LIMIT,
            increment=1000,
            textvariable=self.queue_tpm_var,
            width=33,
        )
        q_tpm_spin.grid(row=16, column=1, padx=10, pady=4, sticky=tk.EW)
        self._register_spin(
            q_tpm_spin,
            self.queue_tpm_var,
            0,
            MAX_QUEUE_TPM_LIMIT,
            "queue_tpm",
            "TPM 限制",
        )

        # 自适应并发（AIMD）
        self.queue_adaptive_var = tk.BooleanVar(
            value=self.app_config.get(
                "queue_adaptive_concurrency", DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY
            )
        )
        ttk.Checkbutton(
            trans_frame,
            text="启用自适应并发（AIMD：429 减半，连续成功 +1）",
            variable=self.queue_adaptive_var,
        ).grid(row=17, column=0, columnspan=2, sticky=tk.W, padx=10, pady=4)

    def create_volc_tab(self, notebook):
        """创建 V1.5 AI 图片翻译设置页面。"""
        volc_frame = self._create_scrollable_tab(notebook, "图片翻译设置")
        volc_frame.grid_columnconfigure(1, weight=1)

        img_config = self.config_manager.get_image_translation_config()
        ocr_cfg = img_config.get("ocr", {})
        ai_volc_cfg = img_config.get("ai_volcengine", {})

        ttk.Label(
            volc_frame,
            text="OCR 文字预筛选（推荐）",
            font=("TkDefaultFont", 10, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(10, 4))
        workflow_desc = ttk.Label(
            volc_frame,
            text="OCR 会先检测图片中是否有需要翻译的文字，只把命中的图片交给 AI 图生图。",
            foreground=COLORS["muted"],
            justify=tk.LEFT,
        )
        workflow_desc.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 8), sticky=tk.EW)
        workflow_desc.bind(
            "<Configure>", lambda event: workflow_desc.configure(wraplength=event.width - 4)
        )

        self.image_text_enabled_var = tk.BooleanVar(
            value=self.app_config.get("image_text_translation_enabled", True)
        )
        ttk.Checkbutton(
            volc_frame,
            text="启用 OCR 预筛选",
            variable=self.image_text_enabled_var,
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, padx=10, pady=6)

        default_ocr_tip = ttk.Label(
            volc_frame,
            text=(
                "自定义地址留空时，如果已在「API 配置」中保存硅基流动 API Key，"
                "将自动使用硅基流动 PaddleOCR-VL-1.5 和该 Key。"
            ),
            foreground=COLORS["muted"],
            justify=tk.LEFT,
        )
        default_ocr_tip.grid(row=3, column=0, columnspan=3, padx=10, pady=(0, 6), sticky=tk.EW)
        default_ocr_tip.bind(
            "<Configure>", lambda event: default_ocr_tip.configure(wraplength=event.width - 4)
        )

        self.ocr_base_url_var = tk.StringVar(value=ocr_cfg.get("base_url", ""))
        ttk.Label(volc_frame, text="自定义 OCR API 地址:").grid(
            row=4, column=0, sticky=tk.W, padx=10, pady=6
        )
        ttk.Entry(volc_frame, textvariable=self.ocr_base_url_var, width=42).grid(
            row=4, column=1, columnspan=2, padx=10, pady=6, sticky=tk.EW
        )

        ttk.Label(volc_frame, text="OCR 模型 ID:").grid(
            row=5, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.ocr_model_var = tk.StringVar(
            value=ocr_cfg.get(
                "model",
                self.app_config.get("vision_model_name", SILICONFLOW_OCR_DEFAULT_MODEL),
            )
        )
        ttk.Entry(volc_frame, textvariable=self.ocr_model_var, width=42).grid(
            row=5, column=1, columnspan=2, padx=10, pady=6, sticky=tk.EW
        )

        get_ocr_key = getattr(self.config_manager, "get_ocr_key", None)
        ocr_key = str(get_ocr_key() or "") if callable(get_ocr_key) else ""
        self.ocr_key_var = tk.StringVar(value=ocr_key)
        ttk.Label(volc_frame, text="自定义 OCR API Key:").grid(
            row=6, column=0, sticky=tk.W, padx=10, pady=6
        )
        ttk.Entry(volc_frame, textvariable=self.ocr_key_var, show="*", width=32).grid(
            row=6, column=1, columnspan=2, padx=10, pady=6, sticky=tk.EW
        )

        ocr_cost_tip = ttk.Label(
            volc_frame,
            text=(
                "费用提示：不启用或未配置可用 OCR 时，无文字的图片也会进入 AI 翻译，"
                "因此图生图调用次数和花销会更高。"
            ),
            foreground=COLORS["warning"],
            justify=tk.LEFT,
        )
        ocr_cost_tip.grid(row=7, column=0, columnspan=3, padx=10, pady=(2, 10), sticky=tk.EW)
        ocr_cost_tip.bind(
            "<Configure>", lambda event: ocr_cost_tip.configure(wraplength=event.width - 4)
        )

        ttk.Separator(volc_frame, orient=tk.HORIZONTAL).grid(
            row=8, column=0, columnspan=3, sticky=tk.EW, padx=10, pady=10
        )

        ttk.Label(
            volc_frame,
            text="火山引擎图生图",
            font=("TkDefaultFont", 10, "bold"),
        ).grid(row=9, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(0, 4))

        ai_desc = ttk.Label(
            volc_frame,
            text="OCR 预筛选后的图片和「全部插图翻译」都会调用该服务生成译图。",
            foreground=COLORS["muted"],
            justify=tk.LEFT,
        )
        ai_desc.grid(row=10, column=0, columnspan=3, padx=10, pady=(0, 8), sticky=tk.EW)
        ai_desc.bind("<Configure>", lambda e: ai_desc.configure(wraplength=e.width - 4))

        self.volc_base_url_var = tk.StringVar(
            value=ai_volc_cfg.get("base_url", VOLCENGINE_IMAGE_DEFAULT_BASE_URL)
        )
        ttk.Label(volc_frame, text="API 地址:").grid(row=11, column=0, sticky=tk.W, padx=10, pady=6)
        volc_base_url_entry = ttk.Entry(volc_frame, textvariable=self.volc_base_url_var, width=42)
        volc_base_url_entry.grid(row=11, column=1, columnspan=2, padx=10, pady=6, sticky=tk.EW)
        self._form_validator.register_required_string(
            "volc_base_url", "火山引擎 API 地址", self.volc_base_url_var, volc_base_url_entry
        )

        self._volc_model_options = list(VOLCENGINE_IMAGE_MODEL_SUGGESTIONS)
        current_volc_model = ai_volc_cfg.get("model", VOLCENGINE_IMAGE_DEFAULT_MODEL)
        self.volc_model_var = tk.StringVar(value=current_volc_model)
        ttk.Label(volc_frame, text="翻译模型 ID:").grid(
            row=12, column=0, sticky=tk.W, padx=10, pady=6
        )
        volc_model_combo = ttk.Combobox(
            volc_frame,
            textvariable=self.volc_model_var,
            values=self._volc_model_options,
            state="normal",
            width=39,
        )
        volc_model_combo.grid(row=12, column=1, columnspan=2, padx=10, pady=6, sticky=tk.EW)
        self._form_validator.register_required_string(
            "volc_model", "火山引擎翻译模型 ID", self.volc_model_var, volc_model_combo
        )

        ttk.Label(volc_frame, text="火山引擎 API Key:").grid(
            row=13, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.volc_key_var = tk.StringVar(value=self.config_manager.get_volc_key())
        volc_key_entry = ttk.Entry(volc_frame, textvariable=self.volc_key_var, show="*", width=32)
        volc_key_entry.grid(row=13, column=1, columnspan=2, padx=10, pady=6, sticky=tk.EW)

        self.volc_test_btn = ttk.Button(
            volc_frame, text="测试 AI 图片翻译连接", command=self.test_volc_connection
        )
        self.volc_test_btn.grid(row=14, column=1, padx=10, pady=6, sticky=tk.W)
        self._test_buttons.append(self.volc_test_btn)
        fee_tip = ttk.Label(
            volc_frame,
            text="费用提示：AI 图片翻译将调用火山图生图服务并可能产生 API 费用。",
            font=("TkDefaultFont", 8),
            foreground=COLORS["danger"],
        )
        fee_tip.grid(row=15, column=0, columnspan=3, padx=10, pady=(0, 10), sticky=tk.EW)
        fee_tip.bind("<Configure>", lambda e: fee_tip.configure(wraplength=e.width - 4))

    def test_volc_connection(self):
        """测试插图翻译连接。

        P2-5：测试连接使用表单中输入的 Key 临时构造 ImageTranslator，**不再**
        调用 ``config_manager.save_volc_key`` 持久化。只有用户点击"确定/保存"
        时，``save_settings`` 才提交密钥。这避免用户在测试中发现 Key 错误后
        撤销设置，但错误的 Key 已经写入密钥环。
        """
        api_key = self.volc_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("测试失败", "请先输入火山引擎 API Key")
            return
        base_url = self.volc_base_url_var.get().strip()
        model = self.volc_model_var.get().strip()
        if not base_url or not model:
            messagebox.showwarning("测试失败", "请先输入火山引擎 API 地址和翻译模型 ID")
            return
        try:
            base_url = normalize_openai_base_url(base_url)
        except ValueError as exc:
            messagebox.showwarning("配置无效", f"火山引擎 API 地址无效：{exc}")
            return
        # P2-5：避免重复启动
        if not self._begin_test():
            return

        # P2-5：构造一个轻量 stub config_manager，只暴露 ImageTranslator 需要
        # 的两个方法（``get_volc_key`` 和 ``get_app_config``），用表单中的
        # Key 直接返回，不触发任何持久化。
        class _TemporaryVolcConfig:
            """仅用于测试连接的临时 config_manager stub。"""

            def __init__(self, real_config_manager, key: str, base_url: str, model: str):
                self._real = real_config_manager
                self._key = key
                self._base_url = base_url
                self._model = model

            def get_volc_key(self) -> str:
                return self._key

            def get_app_config(self):
                cfg = dict(self._real.get_app_config())
                img = dict(cfg.get("image_translation", {}))
                ai = dict(img.get("ai_volcengine", {}))
                ai["base_url"] = self._base_url
                ai["model"] = self._model
                img["ai_volcengine"] = ai
                cfg["image_translation"] = img
                return cfg

        temp_config = _TemporaryVolcConfig(self.config_manager, api_key, base_url, model)

        def worker():
            translator = None
            try:
                from ..core.image_translator import ImageTranslator

                translator = ImageTranslator(temp_config)
                success = translator.test_connection()
                error_detail = translator.last_error

                if success:
                    self._safe_after(
                        lambda: messagebox.showinfo("测试成功", "火山引擎插图翻译服务可用！")
                    )
                else:
                    self._safe_after(
                        lambda msg=error_detail: messagebox.showerror(
                            "测试失败",
                            msg or "连接失败或鉴权错误，请检查 API Key。",
                        )
                    )
            except Exception as exc:
                # BUG-002：在离开 except 块前绑定消息，避免 NameError
                error_message = str(exc)
                self._safe_after(
                    lambda msg=error_message: messagebox.showerror("测试错误", f"发生异常: {msg}")
                )
            finally:
                if translator is not None:
                    try:
                        translator.close()
                    except Exception:
                        pass
                # P2-5：测试结束，恢复测试按钮可用状态
                self._safe_after(self._end_test)

        threading.Thread(target=worker, daemon=True).start()

    def open_custom_api_dialog(self):
        """Collect and persist one user-defined OpenAI-compatible endpoint."""
        saved_config = self.config_manager.get_provider_config(OPENAI_COMPATIBLE_PROVIDER)

        dialog = tk.Toplevel(self.window)
        dialog.title("添加自定义 OpenAI 兼容服务")
        dialog.geometry("500x330")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - 500) // 2
        y = (dialog.winfo_screenheight() - 330) // 2
        dialog.geometry(f"500x330+{x}+{y}")

        content = ttk.Frame(dialog, padding=20)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(1, weight=1)

        ttk.Label(
            content,
            text="连接信息",
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 14))

        base_url_var = tk.StringVar(value=saved_config.get("base_url", ""))
        api_key_var = tk.StringVar(value=saved_config.get("api_key", ""))
        model_var = tk.StringVar(value=saved_config.get("model_name", ""))

        ttk.Label(content, text="Base URL:").grid(
            row=1, column=0, sticky=tk.W, padx=(0, 12), pady=6
        )
        base_url_entry = ttk.Entry(content, textvariable=base_url_var, width=42)
        base_url_entry.grid(row=1, column=1, sticky=tk.EW, pady=6)
        ttk.Label(
            content,
            text="例如：https://api.example.com/v1",
            foreground=COLORS["muted"],
        ).grid(row=2, column=1, sticky=tk.W, pady=(0, 6))

        ttk.Label(content, text="LLM API Key:").grid(
            row=3, column=0, sticky=tk.W, padx=(0, 12), pady=6
        )
        api_key_entry = ttk.Entry(content, textvariable=api_key_var, show="*", width=42)
        api_key_entry.grid(row=3, column=1, sticky=tk.EW, pady=6)

        show_key_var = tk.BooleanVar(value=False)

        def toggle_key_visibility():
            api_key_entry.configure(show="" if show_key_var.get() else "*")

        ttk.Checkbutton(
            content,
            text="显示 API Key",
            variable=show_key_var,
            command=toggle_key_visibility,
        ).grid(row=4, column=1, sticky=tk.W, pady=(0, 6))

        ttk.Label(content, text="模型名称:").grid(
            row=5, column=0, sticky=tk.W, padx=(0, 12), pady=6
        )
        ttk.Entry(content, textvariable=model_var, width=42).grid(
            row=5, column=1, sticky=tk.EW, pady=6
        )

        button_frame = ttk.Frame(content)
        button_frame.grid(row=6, column=0, columnspan=2, sticky=tk.E, pady=(18, 0))

        def save_custom_service():
            api_key = api_key_var.get().strip()
            model_name = model_var.get().strip()
            try:
                base_url = normalize_openai_base_url(base_url_var.get())
            except ValueError as exc:
                messagebox.showwarning("地址无效", str(exc), parent=dialog)
                base_url_entry.focus_set()
                return

            if not api_key:
                messagebox.showwarning("信息不完整", "请输入 LLM API Key", parent=dialog)
                api_key_entry.focus_set()
                return
            if not model_name:
                messagebox.showwarning("信息不完整", "请输入接口支持的模型名称", parent=dialog)
                return

            custom_config = self.api_config.copy()
            custom_config.pop("max_tokens", None)
            custom_config.update(
                {
                    "provider": OPENAI_COMPATIBLE_PROVIDER,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model_name": model_name,
                    "temperature": self.temperature_var.get(),
                }
            )
            result = self.config_manager.save_api_config(custom_config)
            if result.failed:
                # P1-2：FAILED 时不关闭窗口，显示具体错误
                messagebox.showerror(
                    "保存失败",
                    result.user_message or "自定义服务保存失败，请稍后重试",
                    parent=dialog,
                )
                return
            # P1-2：SESSION_ONLY 时提示用户密钥未持久化（不阻断流程）
            if result.session_only:
                messagebox.showinfo(
                    "提示",
                    result.user_message,
                    parent=dialog,
                )

            self.api_config = self.config_manager.get_api_config()
            self.provider_var.set(OPENAI_COMPATIBLE_PROVIDER)
            self.provider_display_var.set(self.provider_display_map[OPENAI_COMPATIBLE_PROVIDER])
            self.model_combo.configure(values=[], state="normal")
            self.base_url_var.set(base_url)
            self.api_key_var.set(api_key)
            self.model_var.set(model_name)
            if self.callback:
                self.callback()
            dialog.destroy()
            messagebox.showinfo("保存成功", "自定义服务已保存并设为当前服务", parent=self.window)

        ttk.Button(button_frame, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(button_frame, text="保存并使用", command=save_custom_service).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        dialog.bind("<Return>", lambda _event: save_custom_service())
        # P2-1：模态对话框补 Escape 关闭，关闭后焦点回父窗口
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind(
            "<Destroy>",
            lambda _event: self.window.focus_set() if self.window.winfo_exists() else None,
            add="+",
        )
        base_url_entry.focus_set()

    def on_provider_changed(self, event=None):
        """当提供商改变时更新模型列表、基础URL和API密钥"""
        provider = self.display_to_provider_map.get(self.provider_display_var.get(), "siliconflow")
        self.provider_var.set(provider)

        # 更新模型列表
        if provider == "deepseek":
            self.model_combo.configure(values=self.deepseek_models, state="readonly")
            self.model_var.set(self.deepseek_models[0])
            self.base_url_var.set(default_base_url_for_provider(provider))
        elif provider == OPENAI_COMPATIBLE_PROVIDER:
            custom_config = self.config_manager.get_provider_config(provider)
            self.model_combo.configure(values=[], state="normal")
            self.model_var.set(custom_config.get("model_name", ""))
            self.base_url_var.set(custom_config.get("base_url", ""))
        else:
            self.model_combo.configure(values=self.siliconflow_models, state="readonly")
            self.model_var.set(self.siliconflow_models[0])
            self.base_url_var.set(default_base_url_for_provider(provider))

        # BUG-009：从密钥环加载对应提供商的API密钥
        self.api_key_var.set(self.config_manager.get_provider_key(provider))

    def update_temperature_label(self, value):
        """更新温度标签"""
        self.temp_label.config(text=f"{float(value):.1f}")

    def reset_prompt(self):
        """重置提示词为默认值"""
        default_prompt = self.config_manager._get_default_prompt()
        self.prompt_text.delete(1.0, tk.END)
        self.prompt_text.insert(1.0, default_prompt)

    def _current_api_form_config(self):
        """Build and validate the API settings currently shown in the form."""
        display_model = self.model_var.get().strip()
        actual_model = self.display_to_model_map.get(display_model, display_model)
        provider = self.provider_var.get()
        base_url = self.base_url_var.get().strip()
        if provider == OPENAI_COMPATIBLE_PROVIDER:
            base_url = normalize_openai_base_url(base_url)
            if not actual_model:
                raise ValueError("请输入接口支持的模型名称")
            self.base_url_var.set(base_url)

        return {
            "provider": provider,
            "api_key": self.api_key_var.get().strip(),
            "base_url": base_url,
            "model_name": actual_model,
            "temperature": self.temperature_var.get(),
        }

    def test_connection(self):
        """测试API连接。

        P2-5：加入 testing busy 状态，避免重复点击触发多次测试。
        测试使用表单中的配置（``_current_api_form_config``），不调用
        ``config_manager.save_api_config``，因此不会持久化任何变更。
        """
        try:
            test_config = self._current_api_form_config()
        except (ValueError, tk.TclError) as exc:
            messagebox.showwarning("配置无效", str(exc), parent=self.window)
            return

        if not test_config["api_key"].strip():
            messagebox.showwarning("测试失败", "请先输入API密钥")
            return

        # P2-5：避免重复启动
        if not self._begin_test():
            return

        # 在新线程中测试连接
        test_thread = threading.Thread(target=self._test_connection_worker, args=(test_config,))
        test_thread.daemon = True
        test_thread.start()

    def _test_connection_worker(self, config):
        """测试连接工作线程"""
        api = None
        try:
            provider = config.get("provider", "siliconflow")

            if provider == "deepseek":
                from ..api.deepseek_api import DeepseekAPI

                api = DeepseekAPI(config)
            elif provider == OPENAI_COMPATIBLE_PROVIDER:
                from ..api.openai_compatible_api import OpenAICompatibleAPI

                api = OpenAICompatibleAPI(config)
            else:
                from ..api.siliconflow_api import SiliconFlowAPI

                api = SiliconFlowAPI(config)

            success = api.test_connection()

            if success:
                self._safe_after(lambda: messagebox.showinfo("测试成功", "API连接测试成功！"))
            else:
                self._safe_after(
                    lambda: messagebox.showerror("测试失败", "API连接测试失败，请检查配置")
                )

        except Exception as exc:
            # BUG-002：在离开 except 块前绑定消息，避免 NameError
            error_message = str(exc)
            self._safe_after(
                lambda msg=error_message: messagebox.showerror("测试错误", f"连接测试出错: {msg}")
            )
        finally:
            # BUG-005：临时连接测试使用后必须关闭，避免心跳线程和连接累积
            if api is not None:
                try:
                    api.close()
                except Exception:
                    pass
            # P2-5：测试结束，恢复测试按钮可用状态
            self._safe_after(self._end_test)

    def save_settings(self):
        """保存设置。

        P2-5：保存前先做整表校验。若有错误，弹提示并把焦点设到首个出错字段，
        避免用户在多个字段都非法时只能看到一个通用错误、却不知道在哪。
        """
        # P2-5：表单级校验 + 首错聚焦
        ok, first_message, first_widget = self._validate_form()
        if not ok:
            messagebox.showwarning("配置无效", first_message, parent=self.window)
            if first_widget is not None:
                try:
                    first_widget.focus_set()
                except (tk.TclError, AttributeError):
                    pass
            return

        try:
            # 更新API配置
            new_api_config = apply_text_translation_profile(self._current_api_form_config())
            volc_base_url = normalize_openai_base_url(self.volc_base_url_var.get().strip())
            self.volc_base_url_var.set(volc_base_url)
            ocr_base_url = self.ocr_base_url_var.get().strip()
            if ocr_base_url:
                ocr_base_url = normalize_openai_base_url(ocr_base_url)
                self.ocr_base_url_var.set(ocr_base_url)
                if not self.ocr_key_var.get().strip():
                    raise ValueError("使用自定义 OCR API 地址时，请输入自定义 OCR API Key")
            ocr_model = self.ocr_model_var.get().strip() or SILICONFLOW_OCR_DEFAULT_MODEL
            self.ocr_model_var.set(ocr_model)

            # 更新应用配置
            new_app_config = self.app_config.copy()
            new_app_config.pop("batch_max_input_tokens", None)
            new_app_config.pop("queue_batch_max_input_tokens", None)
            new_app_config.update(
                {
                    "target_language": self.target_lang_var.get(),
                    "batch_lines": self.batch_lines_var.get(),
                    "translation_concurrency": self.translation_concurrency_var.get(),
                    "auto_save": self.auto_save_var.get(),
                    "ui_font_size": self.ui_font_size_var.get(),
                    "translation_prompt": self.prompt_text.get(1.0, tk.END).strip(),
                    # 队列翻译并发优化阶段 1：保存 QueuePolicy 参数
                    "queue_max_in_flight_requests": self.queue_max_in_flight_var.get(),
                    "queue_hard_request_cap": self.queue_hard_cap_var.get(),
                    "queue_max_active_tasks": self.queue_max_active_var.get(),
                    "queue_per_task_soft_limit": self.queue_per_task_soft_var.get(),
                    "queue_batch_lines": self.queue_batch_lines_var.get(),
                    "queue_rpm_limit": self.queue_rpm_var.get(),
                    "queue_tpm_limit": self.queue_tpm_var.get(),
                    "queue_adaptive_concurrency": self.queue_adaptive_var.get(),
                    "vision_model_name": ocr_model,
                    "image_text_translation_enabled": self.image_text_enabled_var.get(),
                    "image_gen_provider": "volcengine",
                    "image_translation": {
                        "default_provider": "ai_volcengine",
                        "ocr": {
                            "base_url": ocr_base_url,
                            "model": ocr_model,
                        },
                        "ai_volcengine": {
                            "provider": "volcengine",
                            "base_url": volc_base_url,
                            "model": self.volc_model_var.get().strip(),
                        },
                    },
                }
            )

            # P1-2：保存配置，收集各部分的 SecretSaveResult / 布尔状态
            volc_result = self.config_manager.save_volc_key(self.volc_key_var.get())
            ocr_result = self.config_manager.save_ocr_key(self.ocr_key_var.get())
            api_result = self.config_manager.save_api_config(new_api_config)
            app_saved = self.config_manager.save_app_config(new_app_config)

            # P1-2：任一密钥 FAILED 都不关闭窗口，显示具体错误
            if volc_result.failed or ocr_result.failed or api_result.failed or not app_saved:
                # 优先显示密钥相关错误，否则显示通用错误
                failed_result = next(
                    (result for result in (volc_result, ocr_result, api_result) if result.failed),
                    None,
                )
                error_msg = (
                    failed_result.user_message
                    if failed_result is not None
                    else "应用配置保存失败，请重试"
                )
                messagebox.showerror("保存失败", error_msg, parent=self.window)
                return

            # P1-2：SESSION_ONLY 时提示密钥未持久化（不阻断流程，但仍保存成功）
            session_only_msgs = []
            if volc_result.session_only:
                session_only_msgs.append(volc_result.user_message)
            if ocr_result.session_only:
                session_only_msgs.append(ocr_result.user_message)
            if api_result.session_only:
                session_only_msgs.append(api_result.user_message)

            if session_only_msgs:
                messagebox.showinfo(
                    "保存成功",
                    "\n".join(session_only_msgs),
                    parent=self.window,
                )
            else:
                messagebox.showinfo("保存成功", "设置已保存")

            # 调用回调函数
            if self.callback:
                self.callback()

            self.window.destroy()

        except (ValueError, tk.TclError) as e:
            messagebox.showwarning("配置无效", str(e), parent=self.window)
        except Exception as e:
            messagebox.showerror("保存错误", f"保存设置时出错: {str(e)}")

    def save_api_preset(self):
        """保存API预设（ENG-2：使用 SecretSaveResult 区分三态）"""
        api_key = self.api_key_var.get().strip()
        display_model = self.model_var.get().strip()
        model_name = self.display_to_model_map.get(display_model, display_model)

        if not api_key or not model_name:
            messagebox.showwarning(
                "保存预设",
                "请先输入API密钥和模型名称",
                parent=self.window,
            )
            return

        # ENG-2：显式使用 simpledialog.askstring 并设置 parent
        # 不再依赖 filedialog 间接注册 simpledialog 的导入副作用
        preset_name = simpledialog.askstring(
            "保存预设",
            "请输入预设名称:",
            parent=self.window,
        )
        if not preset_name:
            return

        # ENG-2：返回 SecretSaveResult，按三态分支提示
        result = self.config_manager.save_api_and_model_preset(preset_name, api_key, model_name)
        if result.failed:
            messagebox.showerror(
                "保存失败",
                result.user_message,
                parent=self.window,
            )
        elif result.session_only:
            # SESSION_ONLY 必须明确提示重启后失效，不能写成永久保存成功
            messagebox.showinfo(
                "保存提示",
                f"API预设 '{preset_name}' 已保存，但密钥未持久化，重启后需重新输入",
                parent=self.window,
            )
        else:
            messagebox.showinfo(
                "保存成功",
                f"API预设 '{preset_name}' 已保存",
                parent=self.window,
            )

    def load_api_preset(self):
        """加载API预设"""
        presets = self.config_manager.load_api_presets()
        if not presets:
            messagebox.showinfo("加载预设", "没有找到已保存的预设")
            return

        # 创建预设选择窗口
        preset_window = tk.Toplevel(self.window)
        preset_window.title("选择API预设")
        preset_window.geometry("400x300")
        preset_window.transient(self.window)
        preset_window.grab_set()

        # 居中显示
        x = (preset_window.winfo_screenwidth() // 2) - (400 // 2)
        y = (preset_window.winfo_screenheight() // 2) - (300 // 2)
        preset_window.geometry(f"400x300+{x}+{y}")

        # 预设列表
        ttk.Label(preset_window, text="选择要加载的预设:").pack(pady=10)

        listbox_frame = ttk.Frame(preset_window)
        listbox_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        preset_listbox = tk.Listbox(listbox_frame)
        preset_scroll = ttk.Scrollbar(
            listbox_frame, orient=tk.VERTICAL, command=preset_listbox.yview
        )
        preset_listbox.configure(yscrollcommand=preset_scroll.set)

        preset_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        preset_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 填充预设列表
        for preset_name, preset_data in presets.items():
            display_text = f"{preset_name} ({preset_data.get('model_name', '未知模型')})"
            preset_listbox.insert(tk.END, display_text)

        # 按钮框架
        button_frame = ttk.Frame(preset_window)
        button_frame.pack(fill=tk.X, padx=20, pady=10)

        def load_selected():
            selection = preset_listbox.curselection()
            if not selection:
                messagebox.showwarning("选择预设", "请选择一个预设")
                return

            preset_name = list(presets.keys())[selection[0]]
            preset_data = presets[preset_name]

            # 加载预设数据
            self.api_key_var.set(preset_data.get("api_key", ""))
            model_name = preset_data.get("model_name", "")
            self.model_var.set(self.model_display_map.get(model_name, model_name))

            preset_window.destroy()
            messagebox.showinfo("加载成功", f"已加载预设 '{preset_name}'")

        def delete_selected():
            selection = preset_listbox.curselection()
            if not selection:
                messagebox.showwarning("删除预设", "请选择一个预设")
                return

            preset_name = list(presets.keys())[selection[0]]

            if messagebox.askyesno("确认删除", f"确定要删除预设 '{preset_name}' 吗？"):
                if self.config_manager.delete_api_preset(preset_name):
                    preset_listbox.delete(selection[0])
                    messagebox.showinfo("删除成功", f"预设 '{preset_name}' 已删除")
                else:
                    messagebox.showerror("删除失败", "删除预设失败")

        ttk.Button(button_frame, text="加载", command=load_selected).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(button_frame, text="删除", command=delete_selected).pack(
            side=tk.LEFT, padx=(0, 5)
        )
        ttk.Button(button_frame, text="取消", command=preset_window.destroy).pack(side=tk.RIGHT)
