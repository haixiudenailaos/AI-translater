#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设置窗口模块
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading

from ..config.translation_profile import (
    DEFAULT_TRANSLATION_BATCH_LINES,
    DEFAULT_TRANSLATION_CONCURRENCY,
    DEFAULT_TRANSLATION_INPUT_TOKENS,
    MAX_STABLE_TRANSLATION_BATCH_LINES,
    MAX_STABLE_TRANSLATION_INPUT_TOKENS,
    DEEPSEEK_V4_FLASH_MODEL,
    OPENAI_COMPATIBLE_PROVIDER,
    SILICONFLOW_DEEPSEEK_V32_MODEL,
    apply_text_translation_profile,
    default_base_url_for_provider,
    normalize_openai_base_url,
)

class SettingsWindow:
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
        self.window.geometry("560x720")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()
        
        # 居中显示
        self.center_window()
        
        # 加载当前配置
        self.api_config = config_manager.get_api_config()
        self.app_config = config_manager.get_app_config()
        
        self.setup_ui()
        
    def center_window(self):
        """窗口居中显示"""
        self.window.update_idletasks()
        width, height = 560, 720
        x = (self.window.winfo_screenwidth() // 2) - (width // 2)
        y = (self.window.winfo_screenheight() // 2) - (height // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _safe_after(self, func):
        """R2-BUG-026：安全地在主线程调度回调，窗口已销毁时丢弃事件。"""
        try:
            if self.window.winfo_exists():
                self.window.after(0, func)
        except Exception:
            pass
        
    def setup_ui(self):
        """设置界面"""
        # 创建笔记本控件
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # API配置页面
        self.create_api_tab(notebook)
        
        # 翻译设置页面
        self.create_translation_tab(notebook)
        
        # 图生图配置页面 -> 插图翻译配置页面
        self.create_volc_tab(notebook)
        
        # 按钮框架
        button_frame = ttk.Frame(self.window)
        button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        
        ttk.Button(button_frame, text="测试连接", 
                  command=self.test_connection).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="保存", 
                  command=self.save_settings).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(button_frame, text="取消", 
                  command=self.window.destroy).pack(side=tk.RIGHT)
        
    def create_api_tab(self, notebook):
        """创建API配置页面"""
        api_frame = ttk.Frame(notebook)
        notebook.add(api_frame, text="API配置")
        
        # API提供商
        ttk.Label(api_frame, text="API提供商:").grid(row=0, column=0, sticky=tk.W, padx=10, pady=10)
        current_provider = self.api_config.get("provider", "siliconflow")
        if current_provider not in self.provider_display_map:
            current_provider = "siliconflow"
        self.provider_var = tk.StringVar(value=current_provider)
        self.provider_display_var = tk.StringVar(
            value=self.provider_display_map[current_provider]
        )
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
        api_key_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, 
                                 show="*", width=25)
        api_key_entry.grid(row=1, column=1, padx=10, pady=10)
        
        # API预设管理按钮
        preset_frame = ttk.Frame(api_frame)
        preset_frame.grid(row=1, column=2, padx=5, pady=10)
        ttk.Button(preset_frame, text="保存预设", width=8,
                  command=self.save_api_preset).pack(side=tk.TOP, pady=(0, 2))
        ttk.Button(preset_frame, text="加载预设", width=8,
                  command=self.load_api_preset).pack(side=tk.TOP)
        
        # 基础URL
        ttk.Label(api_frame, text="基础URL:").grid(row=2, column=0, sticky=tk.W, padx=10, pady=10)
        self.base_url_var = tk.StringVar(value=self.api_config.get("base_url", "https://api.siliconflow.cn/v1"))
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
        
        self.model_combo = ttk.Combobox(api_frame, textvariable=self.model_var,
                                  values=model_display_values, width=30, state=model_state)
        self.model_combo.grid(row=3, column=1, padx=10, pady=10)
        
        # 最大令牌数
        ttk.Label(api_frame, text="最大令牌数:").grid(row=4, column=0, sticky=tk.W, padx=10, pady=10)
        self.max_tokens_var = tk.IntVar(value=self.api_config.get("max_tokens", 4000))
        max_tokens_spin = ttk.Spinbox(api_frame, from_=1000, to=32768,
                                     textvariable=self.max_tokens_var, width=33)
        max_tokens_spin.grid(row=4, column=1, padx=10, pady=10)
        
        # 温度参数
        ttk.Label(api_frame, text="温度参数:").grid(row=5, column=0, sticky=tk.W, padx=10, pady=10)
        self.temperature_var = tk.DoubleVar(value=self.api_config.get("temperature", 0.3))
        temperature_scale = ttk.Scale(api_frame, from_=0.0, to=1.0, 
                                     variable=self.temperature_var, orient=tk.HORIZONTAL, length=200)
        temperature_scale.grid(row=5, column=1, padx=10, pady=10)
        
        # 温度值显示
        self.temp_label = ttk.Label(api_frame, text=f"{self.temperature_var.get():.1f}")
        self.temp_label.grid(row=5, column=2, padx=5, pady=10)
        temperature_scale.configure(command=self.update_temperature_label)
        
    def create_translation_tab(self, notebook):
        """创建翻译设置页面"""
        trans_frame = ttk.Frame(notebook)
        notebook.add(trans_frame, text="翻译设置")
        
        # 添加提示信息
        tip_label = ttk.Label(trans_frame, text="建议使用文本库功能确定人名、地名等专有名词，这将显著提升翻译效果！", 
                             foreground="#0066CC", font=('TkDefaultFont', 9))
        tip_label.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(10, 5))
        
        # 目标语言
        ttk.Label(trans_frame, text="目标语言:").grid(row=1, column=0, sticky=tk.W, padx=10, pady=10)
        self.target_lang_var = tk.StringVar(value=self.app_config.get("target_language", "中文"))
        lang_combo = ttk.Combobox(trans_frame, textvariable=self.target_lang_var,
                                 values=["中文", "英文", "日文", "韩文", "法文", "德文", "西班牙文"],
                                 width=30)
        lang_combo.grid(row=1, column=1, padx=10, pady=10)

        # 批次翻译行数
        ttk.Label(trans_frame, text="批次翻译行数:").grid(row=2, column=0, sticky=tk.W, padx=10, pady=10)
        self.batch_lines_var = tk.IntVar(value=self.app_config.get(
            "batch_lines", DEFAULT_TRANSLATION_BATCH_LINES
        ))
        batch_spin = ttk.Spinbox(
            trans_frame, from_=1, to=MAX_STABLE_TRANSLATION_BATCH_LINES, increment=1,
                                textvariable=self.batch_lines_var, width=33)
        batch_spin.grid(row=2, column=1, padx=10, pady=10)

        # 批次行数提示
        batch_tip = ttk.Label(trans_frame, text="同时受下方 token 预算限制",
                              foreground="#666", font=('TkDefaultFont', 8))
        batch_tip.grid(row=3, column=1, sticky=tk.W, padx=10, pady=(0, 10))

        ttk.Label(trans_frame, text="批次输入预算:").grid(row=4, column=0, sticky=tk.W, padx=10, pady=8)
        self.batch_token_budget_var = tk.IntVar(
            value=self.app_config.get(
                "batch_max_input_tokens", DEFAULT_TRANSLATION_INPUT_TOKENS
            )
        )
        ttk.Spinbox(
            trans_frame, from_=1000, to=MAX_STABLE_TRANSLATION_INPUT_TOKENS,
            increment=500,
            textvariable=self.batch_token_budget_var, width=33,
        ).grid(row=4, column=1, padx=10, pady=8)

        ttk.Label(trans_frame, text="并发请求数:").grid(row=5, column=0, sticky=tk.W, padx=10, pady=8)
        self.translation_concurrency_var = tk.IntVar(
            value=self.app_config.get(
                "translation_concurrency", DEFAULT_TRANSLATION_CONCURRENCY
            )
        )
        ttk.Spinbox(
            trans_frame, from_=1, to=8,
            textvariable=self.translation_concurrency_var, width=33,
        ).grid(row=5, column=1, padx=10, pady=8)

        # 自动保存
        self.auto_save_var = tk.BooleanVar(value=self.app_config.get("auto_save", True))
        auto_save_check = ttk.Checkbutton(trans_frame, text="启用自动保存",
                                         variable=self.auto_save_var)
        auto_save_check.grid(row=6, column=0, columnspan=2, sticky=tk.W, padx=10, pady=8)

        ttk.Label(trans_frame, text="界面字号:").grid(row=7, column=0, sticky=tk.W, padx=10, pady=8)
        self.ui_font_size_var = tk.IntVar(value=self.app_config.get("ui_font_size", 10))
        ttk.Spinbox(trans_frame, from_=8, to=18, textvariable=self.ui_font_size_var,
                    width=33).grid(row=7, column=1, padx=10, pady=8)

        # 翻译提示词
        ttk.Label(trans_frame, text="翻译提示词:").grid(row=8, column=0, sticky=tk.NW, padx=10, pady=8)

        prompt_frame = ttk.Frame(trans_frame)
        prompt_frame.grid(row=8, column=1, padx=10, pady=8, sticky=tk.EW)
        
        self.prompt_text = tk.Text(prompt_frame, height=10, width=40, wrap=tk.WORD)
        prompt_scroll = ttk.Scrollbar(prompt_frame, orient=tk.VERTICAL, command=self.prompt_text.yview)
        self.prompt_text.configure(yscrollcommand=prompt_scroll.set)
        
        self.prompt_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        prompt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 插入当前提示词
        self.prompt_text.insert(1.0, self.app_config.get("translation_prompt", ""))
        
        # 重置按钮
        ttk.Button(trans_frame, text="重置为默认",
                  command=self.reset_prompt).grid(row=9, column=1, padx=10, pady=8, sticky=tk.E)

    def create_volc_tab(self, notebook):
        """创建图片翻译设置页面

        分区设计：
        - 上半：Manga 默认模块设置（设备/质量预设/模型目录/模型状态）
        - 下半：AI 图片翻译（火山引擎）设置（API Key/测试连接/费用提示）
        两者独立——Manga 不依赖火山 Key；火山入口只能由用户显式触发。
        """
        volc_frame = ttk.Frame(notebook)
        notebook.add(volc_frame, text="图片翻译设置")

        img_config = self.config_manager.get_image_translation_config()
        manga_cfg = img_config.get("manga", {})

        # ── Manga 默认模块分区 ──────────────────────────────
        manga_header = ttk.Label(
            volc_frame,
            text="Manga 默认模块（默认图片翻译实现）",
            font=('TkDefaultFont', 10, 'bold'),
        )
        manga_header.grid(row=0, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(10, 4))

        manga_desc = ttk.Label(
            volc_frame,
            text="使用 manga-image-translator 流水线（检测/OCR/翻译/擦除/渲染）。\n"
                 "翻译图片文字复用上方 API 配置，无需单独配置火山 Key。",
            wraplength=480, foreground="#666", justify=tk.LEFT,
        )
        manga_desc.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 8), sticky=tk.W)

        # 默认模块（只读）
        ttk.Label(volc_frame, text="默认模块:").grid(
            row=2, column=0, sticky=tk.W, padx=10, pady=6
        )
        ttk.Label(volc_frame, text="Manga (manga-image-translator)",
                  font=('TkDefaultFont', 9, 'bold')).grid(
            row=2, column=1, columnspan=2, padx=10, pady=6, sticky=tk.W
        )

        # 设备
        ttk.Label(volc_frame, text="推理设备:").grid(
            row=3, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.manga_device_var = tk.StringVar(
            value=manga_cfg.get("device", "auto")
        )
        ttk.Combobox(
            volc_frame,
            textvariable=self.manga_device_var,
            values=["auto", "cpu", "cuda"],
            state="readonly",
            width=20,
        ).grid(row=3, column=1, padx=10, pady=6, sticky=tk.W)

        # 质量预设
        ttk.Label(volc_frame, text="质量预设:").grid(
            row=4, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.manga_quality_var = tk.StringVar(
            value=manga_cfg.get("quality_preset", "standard")
        )
        ttk.Combobox(
            volc_frame,
            textvariable=self.manga_quality_var,
            values=["standard", "high_quality", "low_memory"],
            state="readonly",
            width=20,
        ).grid(row=4, column=1, padx=10, pady=6, sticky=tk.W)

        # 模型目录
        ttk.Label(volc_frame, text="模型目录:").grid(
            row=5, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.manga_model_dir_var = tk.StringVar(
            value=manga_cfg.get("model_dir", "")
        )
        model_dir_entry = ttk.Entry(
            volc_frame, textvariable=self.manga_model_dir_var, width=32
        )
        model_dir_entry.grid(row=5, column=1, padx=10, pady=6, sticky=tk.W)
        ttk.Button(
            volc_frame, text="浏览...", command=self._browse_manga_model_dir
        ).grid(row=5, column=2, padx=(0, 10), pady=6, sticky=tk.W)

        # 模型状态
        ttk.Label(volc_frame, text="模型状态:").grid(
            row=6, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.manga_status_label = ttk.Label(volc_frame, text="未检测", foreground="#666")
        self.manga_status_label.grid(row=6, column=1, padx=10, pady=6, sticky=tk.W)
        ttk.Button(
            volc_frame, text="检测可用性", command=self._check_manga_status
        ).grid(row=6, column=2, padx=(0, 10), pady=6, sticky=tk.W)

        # 分隔线
        ttk.Separator(volc_frame, orient=tk.HORIZONTAL).grid(
            row=7, column=0, columnspan=3, sticky=tk.EW, padx=10, pady=10
        )

        # ── AI 图片翻译（火山引擎）分区 ──────────────────────
        ai_header = ttk.Label(
            volc_frame,
            text="AI 图片翻译（火山引擎 Doubao-SeeDream）",
            font=('TkDefaultFont', 10, 'bold'),
        )
        ai_header.grid(row=8, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(0, 4))

        ai_desc = ttk.Label(
            volc_frame,
            text="AI 图片翻译为生成式图生图，会产生 API 费用并对图片做较大修改，"
                 "仅由用户显式选择「AI 图片翻译...」时调用，不会作为默认模块。",
            wraplength=480, foreground="#666", justify=tk.LEFT,
        )
        ai_desc.grid(row=9, column=0, columnspan=3, padx=10, pady=(0, 8), sticky=tk.W)

        # 火山引擎 API Key
        ttk.Label(volc_frame, text="火山引擎 API Key:").grid(
            row=10, column=0, sticky=tk.W, padx=10, pady=6
        )
        self.volc_key_var = tk.StringVar(value=self.config_manager.get_volc_key())
        volc_key_entry = ttk.Entry(
            volc_frame, textvariable=self.volc_key_var, show="*", width=32
        )
        volc_key_entry.grid(row=10, column=1, columnspan=2, padx=10, pady=6, sticky=tk.W)

        # 测试按钮 + 费用提示
        ttk.Button(
            volc_frame, text="测试 AI 图片翻译连接", command=self.test_volc_connection
        ).grid(row=11, column=1, padx=10, pady=6, sticky=tk.W)
        fee_tip = ttk.Label(
            volc_frame,
            text="费用提示：AI 图片翻译将调用火山图生图服务并可能产生 API 费用。",
            font=('TkDefaultFont', 8), foreground="#a00",
        )
        fee_tip.grid(row=12, column=0, columnspan=3, padx=10, pady=(0, 10), sticky=tk.W)

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
                    errors.append(
                        "未安装 manga-image-translator 引擎或 torch 依赖"
                    )

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
                        lambda m=msg: self.manga_status_label.config(
                            text=m, foreground="#a00"
                        )
                    )
                else:
                    self._safe_after(
                        lambda: self.manga_status_label.config(
                            text="可用", foreground="#0a0"
                        )
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
        self.config_manager.save_volc_key(api_key)

        def worker():
            from ..core.image_translator import ImageTranslator

            translator = ImageTranslator(self.config_manager)
            try:
                success = translator.test_connection()
                translator.close()

                if success:
                    self._safe_after(lambda: messagebox.showinfo("测试成功", "火山引擎插图翻译服务可用！"))
                else:
                    self._safe_after(lambda: messagebox.showerror("测试失败", f"连接失败或鉴权错误，请检查 API Key。"))
            except Exception as exc:
                # BUG-002：在离开 except 块前绑定消息，避免 NameError
                error_message = str(exc)
                self._safe_after(lambda msg=error_message: messagebox.showerror("测试错误", f"发生异常: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

        
    def open_custom_api_dialog(self):
        """Collect and persist one user-defined OpenAI-compatible endpoint."""
        saved_config = self.config_manager.get_provider_config(
            OPENAI_COMPATIBLE_PROVIDER
        )

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
        api_key_entry = ttk.Entry(
            content, textvariable=api_key_var, show="*", width=42
        )
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
                messagebox.showwarning(
                    "信息不完整", "请输入 LLM API Key", parent=dialog
                )
                api_key_entry.focus_set()
                return
            if not model_name:
                messagebox.showwarning(
                    "信息不完整", "请输入接口支持的模型名称", parent=dialog
                )
                return

            custom_config = self.api_config.copy()
            custom_config.update({
                "provider": OPENAI_COMPATIBLE_PROVIDER,
                "api_key": api_key,
                "base_url": base_url,
                "model_name": model_name,
                "max_tokens": self.max_tokens_var.get(),
                "temperature": self.temperature_var.get(),
            })
            if not self.config_manager.save_api_config(custom_config):
                messagebox.showerror(
                    "保存失败", "自定义服务保存失败，请稍后重试", parent=dialog
                )
                return

            self.api_config = self.config_manager.get_api_config()
            self.provider_var.set(OPENAI_COMPATIBLE_PROVIDER)
            self.provider_display_var.set(
                self.provider_display_map[OPENAI_COMPATIBLE_PROVIDER]
            )
            self.model_combo.configure(values=[], state="normal")
            self.base_url_var.set(base_url)
            self.api_key_var.set(api_key)
            self.model_var.set(model_name)
            if self.callback:
                self.callback()
            dialog.destroy()
            messagebox.showinfo(
                "保存成功", "自定义服务已保存并设为当前服务", parent=self.window
            )

        ttk.Button(button_frame, text="取消", command=dialog.destroy).pack(
            side=tk.RIGHT
        )
        ttk.Button(
            button_frame, text="保存并使用", command=save_custom_service
        ).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind("<Return>", lambda _event: save_custom_service())
        base_url_entry.focus_set()

    def on_provider_changed(self, event=None):
        """当提供商改变时更新模型列表、基础URL和API密钥"""
        provider = self.display_to_provider_map.get(
            self.provider_display_var.get(), "siliconflow"
        )
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
                self._safe_after(lambda: messagebox.showerror("测试失败", "API连接测试失败，请检查配置"))

        except Exception as exc:
            # BUG-002：在离开 except 块前绑定消息，避免 NameError
            error_message = str(exc)
            self._safe_after(lambda msg=error_message: messagebox.showerror("测试错误", f"连接测试出错: {msg}"))
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
            # 更新API配置
            new_api_config = apply_text_translation_profile(
                self._current_api_form_config()
            )

            # 更新应用配置
            new_app_config = self.app_config.copy()
            new_app_config.update({
                "target_language": self.target_lang_var.get(),
                "batch_lines": self.batch_lines_var.get(),
                "batch_max_input_tokens": self.batch_token_budget_var.get(),
                "translation_concurrency": self.translation_concurrency_var.get(),
                "auto_save": self.auto_save_var.get(),
                "ui_font_size": self.ui_font_size_var.get(),
                "translation_prompt": self.prompt_text.get(1.0, tk.END).strip(),
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
                    },
                },
            })

            # 保存配置
            volc_saved = self.config_manager.save_volc_key(self.volc_key_var.get())

            if self.config_manager.save_api_config(new_api_config) and \
               self.config_manager.save_app_config(new_app_config) and volc_saved:
                messagebox.showinfo("保存成功", "设置已保存")

                # 调用回调函数
                if self.callback:
                    self.callback()

                self.window.destroy()
            else:
                messagebox.showerror("保存失败", "设置保存失败")

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
        preset_scroll = ttk.Scrollbar(listbox_frame, orient=tk.VERTICAL, command=preset_listbox.yview)
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
        ttk.Button(button_frame, text="删除", command=delete_selected).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(button_frame, text="取消", command=preset_window.destroy).pack(side=tk.RIGHT)
