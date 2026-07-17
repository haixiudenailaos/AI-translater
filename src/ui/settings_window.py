#!/usr/bin/env python3
"""
设置窗口模块
"""

import hmac
import threading
import tkinter as tk
from tkinter import messagebox, ttk

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
    DEFAULT_QUEUE_TRANSLATION_INPUT_TOKENS,
    DEFAULT_TRANSLATION_BATCH_LINES,
    DEFAULT_TRANSLATION_CONCURRENCY,
    DEFAULT_TRANSLATION_INPUT_TOKENS,
    MAX_QUEUE_HARD_REQUEST_CAP,
    MAX_QUEUE_MAX_ACTIVE_TASKS,
    MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS,
    MAX_QUEUE_PER_TASK_SOFT_LIMIT,
    MAX_QUEUE_RPM_LIMIT,
    MAX_QUEUE_TPM_LIMIT,
    MAX_QUEUE_TRANSLATION_BATCH_LINES,
    MAX_QUEUE_TRANSLATION_INPUT_TOKENS,
    MAX_STABLE_TRANSLATION_BATCH_LINES,
    MAX_STABLE_TRANSLATION_INPUT_TOKENS,
    OPENAI_COMPATIBLE_PROVIDER,
    SILICONFLOW_DEEPSEEK_V32_MODEL,
    apply_text_translation_profile,
    default_base_url_for_provider,
    normalize_openai_base_url,
)
from .ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox


class SettingsWindow:
    WINDOW_WIDTH = 600
    WINDOW_HEIGHT = 560
    SCREEN_MARGIN = 32

    def __init__(self, parent, config_manager, callback=None):
        self.parent = parent
        self.config_manager = config_manager
        self.callback = callback

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
        self.window.minsize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        self.window.transient(parent)
        self.window.grab_set()

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

        # 加载当前配置
        self.api_config = config_manager.get_api_config()
        self.app_config = config_manager.get_app_config()

        self.setup_ui()

    def _on_window_destroy(self, event):
        """P1-1：窗口销毁时关闭 UI 回调事件泵。幂等。"""
        if event.widget is self.window:
            self.close_ui_pump()

    def close_ui_pump(self):
        """P1-1：关闭 UI 回调事件泵，释放 ``after`` 调度。幂等。"""
        pump = getattr(self, "_ui_pump", None)
        if pump is not None:
            pump.close()
            self._ui_pump = None

    def center_window(self):
        """窗口居中显示"""
        self.window.update_idletasks()
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        width = min(self.WINDOW_WIDTH, max(1, screen_width - self.SCREEN_MARGIN * 2))
        height = min(self.WINDOW_HEIGHT, max(1, screen_height - self.SCREEN_MARGIN * 2))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _safe_after(self, func):
        """P1-1：通过 UI 回调邮箱提交回调，不在工作线程调用 Tk API。

        回调执行时若窗口已销毁，pump 的异常处理器会捕获并记录
        ``TclError``，不会中断后续回调。
        """
        self._ui_mailbox.submit(func)

    def setup_ui(self):
        """设置界面"""
        # 先预留底部操作区，避免高 DPI 或较小屏幕把确认按钮挤出窗口。
        button_frame = ttk.Frame(self.window)
        button_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 12))

        ttk.Button(button_frame, text="测试连接", command=self.test_connection).pack(side=tk.LEFT)
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
        """创建固定视口的设置页，内容过长时仅在页内滚动。"""
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

        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(content_window, width=event.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return content

    def create_api_tab(self, notebook):
        """创建API配置页面"""
        # P1-7：使用页内滚动容器，避免高 DPI 或缩放后内容被裁切
        api_frame = self._create_scrollable_tab(notebook, "API配置")

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
        provider_combo.grid(row=0, column=1, padx=10, pady=10, sticky=tk.W)
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
        api_key_entry.grid(row=1, column=1, padx=10, pady=10)

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
        base_url_entry.grid(row=2, column=1, padx=10, pady=10)

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
        self.model_combo.grid(row=3, column=1, padx=10, pady=10)

        # 最大令牌数
        ttk.Label(api_frame, text="最大令牌数:").grid(
            row=4, column=0, sticky=tk.W, padx=10, pady=10
        )
        self.max_tokens_var = tk.IntVar(value=self.api_config.get("max_tokens", 4000))
        max_tokens_spin = ttk.Spinbox(
            api_frame, from_=1000, to=32768, textvariable=self.max_tokens_var, width=33
        )
        max_tokens_spin.grid(row=4, column=1, padx=10, pady=10)

        # 温度参数
        ttk.Label(api_frame, text="温度参数:").grid(row=5, column=0, sticky=tk.W, padx=10, pady=10)
        self.temperature_var = tk.DoubleVar(value=self.api_config.get("temperature", 0.3))
        temperature_scale = ttk.Scale(
            api_frame,
            from_=0.0,
            to=1.0,
            variable=self.temperature_var,
            orient=tk.HORIZONTAL,
            length=200,
        )
        temperature_scale.grid(row=5, column=1, padx=10, pady=10)

        # 温度值显示
        self.temp_label = ttk.Label(api_frame, text=f"{self.temperature_var.get():.1f}")
        self.temp_label.grid(row=5, column=2, padx=5, pady=10)
        temperature_scale.configure(command=self.update_temperature_label)

    def create_translation_tab(self, notebook):
        """创建翻译设置页面"""
        trans_frame = self._create_scrollable_tab(notebook, "翻译设置")

        # 添加提示信息
        tip_label = ttk.Label(
            trans_frame,
            text="建议使用文本库功能确定人名、地名等专有名词，这将显著提升翻译效果！",
            foreground="#0066CC",
            font=("TkDefaultFont", 9),
        )
        tip_label.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(10, 5))

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
        lang_combo.grid(row=1, column=1, padx=10, pady=10)

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
        batch_spin.grid(row=2, column=1, padx=10, pady=10)

        # 批次行数提示
        batch_tip = ttk.Label(
            trans_frame,
            text="同时受下方 token 预算限制",
            foreground="#666",
            font=("TkDefaultFont", 8),
        )
        batch_tip.grid(row=3, column=1, sticky=tk.W, padx=10, pady=(0, 10))

        ttk.Label(trans_frame, text="批次输入预算:").grid(
            row=4, column=0, sticky=tk.W, padx=10, pady=8
        )
        self.batch_token_budget_var = tk.IntVar(
            value=self.app_config.get("batch_max_input_tokens", DEFAULT_TRANSLATION_INPUT_TOKENS)
        )
        ttk.Spinbox(
            trans_frame,
            from_=1000,
            to=MAX_STABLE_TRANSLATION_INPUT_TOKENS,
            increment=500,
            textvariable=self.batch_token_budget_var,
            width=33,
        ).grid(row=4, column=1, padx=10, pady=8)

        ttk.Label(trans_frame, text="主界面局部并发:").grid(
            row=5, column=0, sticky=tk.W, padx=10, pady=8
        )
        self.translation_concurrency_var = tk.IntVar(
            value=self.app_config.get("translation_concurrency", DEFAULT_TRANSLATION_CONCURRENCY)
        )
        ttk.Spinbox(
            trans_frame,
            from_=1,
            to=8,
            textvariable=self.translation_concurrency_var,
            width=33,
        ).grid(row=5, column=1, padx=10, pady=8)

        # 自动保存
        self.auto_save_var = tk.BooleanVar(value=self.app_config.get("auto_save", True))
        auto_save_check = ttk.Checkbutton(
            trans_frame, text="启用自动保存", variable=self.auto_save_var
        )
        auto_save_check.grid(row=6, column=0, columnspan=2, sticky=tk.W, padx=10, pady=8)

        ttk.Label(trans_frame, text="界面字号:").grid(row=7, column=0, sticky=tk.W, padx=10, pady=8)
        self.ui_font_size_var = tk.IntVar(value=self.app_config.get("ui_font_size", 10))
        ttk.Spinbox(trans_frame, from_=8, to=18, textvariable=self.ui_font_size_var, width=33).grid(
            row=7, column=1, padx=10, pady=8
        )

        # 翻译提示词
        ttk.Label(trans_frame, text="翻译提示词:").grid(
            row=8, column=0, sticky=tk.NW, padx=10, pady=8
        )

        prompt_frame = ttk.Frame(trans_frame)
        prompt_frame.grid(row=8, column=1, padx=10, pady=8, sticky=tk.EW)

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
            row=9, column=1, padx=10, pady=8, sticky=tk.E
        )

        # 队列翻译并发优化阶段 1：队列翻译调度参数（QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §3）
        # 这些参数构造 QueuePolicy，影响全局公平调度器和共享 ProviderLimiter。
        queue_sep = ttk.Separator(trans_frame, orient=tk.HORIZONTAL)
        queue_sep.grid(row=10, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=(10, 4))
        ttk.Label(
            trans_frame,
            text="文本翻译全局调度（主界面 + 后台队列）",
            font=("TkDefaultFont", 9, "bold"),
        ).grid(row=11, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(0, 4))

        # 最大在途请求数（ProviderLimiter configured_max）
        ttk.Label(trans_frame, text="全局最大在途请求:").grid(
            row=12, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_max_in_flight_var = tk.IntVar(
            value=self.app_config.get(
                "queue_max_in_flight_requests", DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS
            )
        )
        ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS,
            increment=1,
            textvariable=self.queue_max_in_flight_var,
            width=33,
        ).grid(row=12, column=1, padx=10, pady=4)

        # 硬上限（ThreadPoolExecutor max_workers）
        ttk.Label(trans_frame, text="硬并发上限:").grid(
            row=13, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_hard_cap_var = tk.IntVar(
            value=self.app_config.get("queue_hard_request_cap", DEFAULT_QUEUE_HARD_REQUEST_CAP)
        )
        ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_HARD_REQUEST_CAP,
            increment=1,
            textvariable=self.queue_hard_cap_var,
            width=33,
        ).grid(row=13, column=1, padx=10, pady=4)

        # 最大活跃任务数
        ttk.Label(trans_frame, text="最大活跃任务:").grid(
            row=14, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_max_active_var = tk.IntVar(
            value=self.app_config.get("queue_max_active_tasks", DEFAULT_QUEUE_MAX_ACTIVE_TASKS)
        )
        ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_MAX_ACTIVE_TASKS,
            increment=1,
            textvariable=self.queue_max_active_var,
            width=33,
        ).grid(row=14, column=1, padx=10, pady=4)

        # 每任务软上限（round-robin 第一轮）
        ttk.Label(trans_frame, text="每任务软上限:").grid(
            row=15, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_per_task_soft_var = tk.IntVar(
            value=self.app_config.get(
                "queue_per_task_soft_limit", DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT
            )
        )
        ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_PER_TASK_SOFT_LIMIT,
            increment=1,
            textvariable=self.queue_per_task_soft_var,
            width=33,
        ).grid(row=15, column=1, padx=10, pady=4)

        # 队列批次行数
        ttk.Label(trans_frame, text="队列批次行数:").grid(
            row=16, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_batch_lines_var = tk.IntVar(
            value=self.app_config.get("queue_batch_lines", DEFAULT_QUEUE_TRANSLATION_BATCH_LINES)
        )
        ttk.Spinbox(
            trans_frame,
            from_=1,
            to=MAX_QUEUE_TRANSLATION_BATCH_LINES,
            increment=1,
            textvariable=self.queue_batch_lines_var,
            width=33,
        ).grid(row=16, column=1, padx=10, pady=4)

        # 队列批次输入预算
        ttk.Label(trans_frame, text="队列批次输入预算:").grid(
            row=17, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_batch_tokens_var = tk.IntVar(
            value=self.app_config.get(
                "queue_batch_max_input_tokens", DEFAULT_QUEUE_TRANSLATION_INPUT_TOKENS
            )
        )
        ttk.Spinbox(
            trans_frame,
            from_=512,
            to=MAX_QUEUE_TRANSLATION_INPUT_TOKENS,
            increment=500,
            textvariable=self.queue_batch_tokens_var,
            width=33,
        ).grid(row=17, column=1, padx=10, pady=4)

        # RPM 限制
        ttk.Label(trans_frame, text="RPM 限制 (0=不限):").grid(
            row=18, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_rpm_var = tk.IntVar(
            value=self.app_config.get("queue_rpm_limit", DEFAULT_QUEUE_RPM_LIMIT)
        )
        ttk.Spinbox(
            trans_frame,
            from_=0,
            to=MAX_QUEUE_RPM_LIMIT,
            increment=10,
            textvariable=self.queue_rpm_var,
            width=33,
        ).grid(row=18, column=1, padx=10, pady=4)

        # TPM 限制
        ttk.Label(trans_frame, text="TPM 限制 (0=不限):").grid(
            row=19, column=0, sticky=tk.W, padx=10, pady=4
        )
        self.queue_tpm_var = tk.IntVar(
            value=self.app_config.get("queue_tpm_limit", DEFAULT_QUEUE_TPM_LIMIT)
        )
        ttk.Spinbox(
            trans_frame,
            from_=0,
            to=MAX_QUEUE_TPM_LIMIT,
            increment=1000,
            textvariable=self.queue_tpm_var,
            width=33,
        ).grid(row=19, column=1, padx=10, pady=4)

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
        ).grid(row=20, column=0, columnspan=2, sticky=tk.W, padx=10, pady=4)

    def create_volc_tab(self, notebook):
        """创建图片翻译设置页面

        分区设计：
        - 上半：Manga 默认模块设置（设备/质量预设/模型目录/模型状态）
        - 下半：AI 图片翻译（火山引擎）设置（API Key/测试连接/费用提示）
        两者独立——Manga 不依赖火山 Key；火山入口只能由用户显式触发。
        """
        volc_frame = self._create_scrollable_tab(notebook, "图片翻译设置")

        img_config = self.config_manager.get_image_translation_config()
        manga_cfg = img_config.get("manga", {})
        ai_volc_cfg = img_config.get("ai_volcengine", {})

        # ── Manga 默认模块分区 ──────────────────────────────
        manga_header = ttk.Label(
            volc_frame,
            text="Manga 默认模块（默认图片翻译实现）",
            font=("TkDefaultFont", 10, "bold"),
        )
        manga_header.grid(row=0, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(10, 4))

        manga_desc = ttk.Label(
            volc_frame,
            text="使用 manga-image-translator 流水线（检测/OCR/翻译/擦除/渲染）。\n"
            "翻译图片文字复用上方 API 配置，无需单独配置火山 Key。",
            wraplength=480,
            foreground="#666",
            justify=tk.LEFT,
        )
        manga_desc.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 8), sticky=tk.W)

        # 默认模块（只读）
        ttk.Label(volc_frame, text="默认模块:").grid(row=2, column=0, sticky=tk.W, padx=10, pady=6)
        ttk.Label(
            volc_frame, text="Manga (manga-image-translator)", font=("TkDefaultFont", 9, "bold")
        ).grid(row=2, column=1, columnspan=2, padx=10, pady=6, sticky=tk.W)

        # 设备
        ttk.Label(volc_frame, text="推理设备:").grid(row=3, column=0, sticky=tk.W, padx=10, pady=6)
        self.manga_device_var = tk.StringVar(value=manga_cfg.get("device", "auto"))
        ttk.Combobox(
            volc_frame,
            textvariable=self.manga_device_var,
            values=["auto", "cpu", "cuda"],
            state="readonly",
            width=20,
        ).grid(row=3, column=1, padx=10, pady=6, sticky=tk.W)

        # 质量预设
        ttk.Label(volc_frame, text="质量预设:").grid(row=4, column=0, sticky=tk.W, padx=10, pady=6)
        self.manga_quality_var = tk.StringVar(value=manga_cfg.get("quality_preset", "standard"))
        ttk.Combobox(
            volc_frame,
            textvariable=self.manga_quality_var,
            values=["standard", "high_quality", "low_memory"],
            state="readonly",
            width=20,
        ).grid(row=4, column=1, padx=10, pady=6, sticky=tk.W)

        # 模型目录
        ttk.Label(volc_frame, text="模型目录:").grid(row=5, column=0, sticky=tk.W, padx=10, pady=6)
        self.manga_model_dir_var = tk.StringVar(value=manga_cfg.get("model_dir", ""))
        model_dir_entry = ttk.Entry(volc_frame, textvariable=self.manga_model_dir_var, width=32)
        model_dir_entry.grid(row=5, column=1, padx=10, pady=6, sticky=tk.W)
        ttk.Button(volc_frame, text="浏览...", command=self._browse_manga_model_dir).grid(
            row=5, column=2, padx=(0, 10), pady=6, sticky=tk.W
        )

        # 模型状态
        ttk.Label(volc_frame, text="模型状态:").grid(row=6, column=0, sticky=tk.W, padx=10, pady=6)
        self.manga_status_label = ttk.Label(volc_frame, text="未检测", foreground="#666")
        self.manga_status_label.grid(row=6, column=1, padx=10, pady=6, sticky=tk.W)
        ttk.Button(volc_frame, text="检测可用性", command=self._check_manga_status).grid(
            row=6, column=2, padx=(0, 10), pady=6, sticky=tk.W
        )

        # 分隔线
        ttk.Separator(volc_frame, orient=tk.HORIZONTAL).grid(
            row=7, column=0, columnspan=3, sticky=tk.EW, padx=10, pady=10
        )

        # ── AI 图片翻译（火山引擎）分区 ──────────────────────
        ai_header = ttk.Label(
            volc_frame,
            text="AI 图片翻译（火山引擎 Doubao-SeeDream）",
            font=("TkDefaultFont", 10, "bold"),
        )
        ai_header.grid(row=8, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(0, 4))

        ai_desc = ttk.Label(
            volc_frame,
            text="AI 图片翻译为生成式图生图，会产生 API 费用并对图片做较大修改，"
            "仅由用户显式选择「AI 图片翻译...」时调用，不会作为默认模块。",
            wraplength=480,
            foreground="#666",
            justify=tk.LEFT,
        )
        ai_desc.grid(row=9, column=0, columnspan=3, padx=10, pady=(0, 8), sticky=tk.W)

        # 图片翻译模型选择（高质量 Pro 较贵 / 经济版较便宜）
        from ..core.image_translator import ImageTranslator

        self._volc_model_options = [
            (ImageTranslator.MODEL_HIGH_QUALITY, "高质量 Pro（效果更好，费用较高）"),
            (ImageTranslator.MODEL_ECONOMY, "经济版（效果一般，费用较低）"),
        ]
        self._volc_model_display_to_name = {disp: name for name, disp in self._volc_model_options}
        self._volc_model_name_to_display = {name: disp for name, disp in self._volc_model_options}
        current_volc_model = ai_volc_cfg.get("model", ImageTranslator.DEFAULT_MODEL)
        self.volc_model_var = tk.StringVar(
            value=self._volc_model_name_to_display.get(
                current_volc_model,
                self._volc_model_name_to_display[ImageTranslator.DEFAULT_MODEL],
            )
        )
        ttk.Label(volc_frame, text="翻译模型:").grid(row=10, column=0, sticky=tk.W, padx=10, pady=6)
        ttk.Combobox(
            volc_frame,
            textvariable=self.volc_model_var,
            values=[disp for _, disp in self._volc_model_options],
            state="readonly",
            width=32,
        ).grid(row=10, column=1, columnspan=2, padx=10, pady=6, sticky=tk.W)

        # 火山引擎 API Key
        ttk.Label(volc_frame, text="火山引擎 API Key:").grid(
            row=11, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.volc_key_var = tk.StringVar(value=self.config_manager.get_volc_key())
        volc_key_entry = ttk.Entry(volc_frame, textvariable=self.volc_key_var, show="*", width=32)
        volc_key_entry.grid(row=11, column=1, columnspan=2, padx=10, pady=6, sticky=tk.W)

        # 测试按钮 + 费用提示
        ttk.Button(volc_frame, text="测试 AI 图片翻译连接", command=self.test_volc_connection).grid(
            row=12, column=1, padx=10, pady=6, sticky=tk.W
        )
        fee_tip = ttk.Label(
            volc_frame,
            text="费用提示：AI 图片翻译将调用火山图生图服务并可能产生 API 费用。",
            font=("TkDefaultFont", 8),
            foreground="#a00",
        )
        fee_tip.grid(row=13, column=0, columnspan=3, padx=10, pady=(0, 10), sticky=tk.W)

    def _browse_manga_model_dir(self):
        """选择 Manga 模型目录。"""
        from tkinter import filedialog

        path = filedialog.askdirectory(title="选择 Manga 模型目录", parent=self.window)
        if path:
            self.manga_model_dir_var.set(path)

    def _check_manga_status(self):
        """检测 Manga 引擎可用性（惰性导入，未安装时只更新状态文本）。

        不依赖具体 mapping_dir，只检查：
        1. 引擎依赖（torch + manga_translator）是否可导入
        2. API 配置（base_url / api_key / model_name）
        3. 当前目标语言是否受 Manga 支持
        """
        self.manga_status_label.config(text="正在检测...", foreground="#666")

        def worker():
            try:
                import importlib

                from ..infrastructure.image_translation.language_codes import (
                    to_manga_lang,
                )

                errors = []

                # 1. 引擎依赖检查（惰性）
                torch_spec = importlib.util.find_spec("torch")
                manga_spec = importlib.util.find_spec("manga_translator")
                if torch_spec is None or manga_spec is None:
                    errors.append("未安装 manga-image-translator 引擎或 torch 依赖")

                # 2. 语言映射检查
                target_lang = self.app_config.get("target_language", "中文")
                if to_manga_lang(target_lang) is None:
                    errors.append(f"目标语言 {target_lang} 暂不支持")

                # 3. API 配置检查（external_llm 复用文本翻译 API）
                api_config = self.config_manager.get_api_config()
                if not api_config.get("base_url"):
                    errors.append("缺少 API base_url")
                if not api_config.get("api_key"):
                    errors.append("缺少 API Key")
                if not api_config.get("model_name"):
                    errors.append("缺少模型名称")

                if errors:
                    msg = "; ".join(errors)
                    self._safe_after(
                        lambda m=msg: self.manga_status_label.config(text=m, foreground="#a00")
                    )
                else:
                    self._safe_after(
                        lambda: self.manga_status_label.config(text="可用", foreground="#0a0")
                    )
            except Exception as exc:
                error_message = str(exc)[:80]
                self._safe_after(
                    lambda m=error_message: self.manga_status_label.config(
                        text=f"检测失败: {m}", foreground="#a00"
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def test_volc_connection(self):
        """测试插图翻译连接"""
        api_key = self.volc_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("测试失败", "请先输入火山引擎 API Key")
            return
        save_result = self.config_manager.save_volc_key(api_key)
        if not save_result:
            messagebox.showerror(
                "保存失败",
                save_result.error_message or "火山引擎 API Key 保存失败",
            )
            return
        if not hmac.compare_digest(self.config_manager.get_volc_key(), api_key):
            messagebox.showerror(
                "保存失败",
                "火山引擎 API Key 保存后回读不一致，请重新输入。",
            )
            return

        def worker():
            from ..core.image_translator import ImageTranslator

            translator = ImageTranslator(self.config_manager)
            try:
                success = translator.test_connection()
                error_detail = translator.last_error
                translator.close()

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
            foreground="#666",
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
            custom_config.update(
                {
                    "provider": OPENAI_COMPATIBLE_PROVIDER,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model_name": model_name,
                    "max_tokens": self.max_tokens_var.get(),
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
            "max_tokens": self.max_tokens_var.get(),
            "temperature": self.temperature_var.get(),
        }

    def test_connection(self):
        """测试API连接"""
        try:
            test_config = self._current_api_form_config()
        except (ValueError, tk.TclError) as exc:
            messagebox.showwarning("配置无效", str(exc), parent=self.window)
            return

        if not test_config["api_key"].strip():
            messagebox.showwarning("测试失败", "请先输入API密钥")
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

    def save_settings(self):
        """保存设置"""
        try:
            from ..core.image_translator import ImageTranslator

            # 更新API配置
            new_api_config = apply_text_translation_profile(self._current_api_form_config())

            # 更新应用配置
            new_app_config = self.app_config.copy()
            new_app_config.update(
                {
                    "target_language": self.target_lang_var.get(),
                    "batch_lines": self.batch_lines_var.get(),
                    "batch_max_input_tokens": self.batch_token_budget_var.get(),
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
                    "queue_batch_max_input_tokens": self.queue_batch_tokens_var.get(),
                    "queue_rpm_limit": self.queue_rpm_var.get(),
                    "queue_tpm_limit": self.queue_tpm_var.get(),
                    "queue_adaptive_concurrency": self.queue_adaptive_var.get(),
                    # 旧字段保留为常量以兼容读取侧（迁移逻辑会忽略这些值）
                    "image_text_translation_enabled": False,
                    "image_gen_provider": "volcengine",
                    # 图片翻译配置：Manga 默认模块 + AI Provider
                    "image_translation": {
                        "default_provider": "manga",
                        "manga": {
                            "quality_preset": self.manga_quality_var.get(),
                            "device": self.manga_device_var.get(),
                            "model_dir": self.manga_model_dir_var.get().strip(),
                            "batch_size": 1,
                        },
                        "ai_volcengine": {
                            "provider": "volcengine",
                            "model": self._volc_model_display_to_name.get(
                                self.volc_model_var.get(),
                                ImageTranslator.DEFAULT_MODEL,
                            ),
                        },
                    },
                }
            )

            # P1-2：保存配置，收集各部分的 SecretSaveResult / 布尔状态
            volc_result = self.config_manager.save_volc_key(self.volc_key_var.get())
            api_result = self.config_manager.save_api_config(new_api_config)
            app_saved = self.config_manager.save_app_config(new_app_config)

            # P1-2：任一密钥 FAILED 都不关闭窗口，显示具体错误
            if volc_result.failed or api_result.failed or not app_saved:
                # 优先显示密钥相关错误，否则显示通用错误
                failed_result = volc_result if volc_result.failed else api_result
                error_msg = (
                    failed_result.user_message
                    if (volc_result.failed or api_result.failed)
                    else "应用配置保存失败，请重试"
                )
                messagebox.showerror("保存失败", error_msg, parent=self.window)
                return

            # P1-2：SESSION_ONLY 时提示密钥未持久化（不阻断流程，但仍保存成功）
            session_only_msgs = []
            if volc_result.session_only:
                session_only_msgs.append(volc_result.user_message)
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
        """保存API预设"""
        api_key = self.api_key_var.get().strip()
        display_model = self.model_var.get().strip()
        model_name = self.display_to_model_map.get(display_model, display_model)

        if not api_key or not model_name:
            messagebox.showwarning("保存预设", "请先输入API密钥和模型名称")
            return

        # 弹出对话框让用户输入预设名称
        preset_name = tk.simpledialog.askstring("保存预设", "请输入预设名称:")
        if not preset_name:
            return

        if self.config_manager.save_api_and_model_preset(preset_name, api_key, model_name):
            messagebox.showinfo("保存成功", f"API预设 '{preset_name}' 已保存")
        else:
            messagebox.showerror("保存失败", "保存API预设失败")

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
