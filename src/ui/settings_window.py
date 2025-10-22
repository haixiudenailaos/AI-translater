#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设置窗口模块
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading

class SettingsWindow:
    def __init__(self, parent, config_manager, callback=None):
        self.parent = parent
        self.config_manager = config_manager
        self.callback = callback
        
        # 模型名称映射：完整名称 -> 显示名称
        self.model_display_map = {
            # SiliconFlow 模型
            "deepseek-ai/DeepSeek-V3.2-Exp": "DeepSeek V3.2 Exp",
            "deepseek-ai/DeepSeek-V3.1-Terminus": "DeepSeek V3.1",
            "deepseek-ai/DeepSeek-V3": "DeepSeek V3",
            "moonshotai/Kimi-K2-Instruct-0905": "Kimi K2",
            "Qwen/Qwen3-Next-80B-A3B-Instruct": "Qwen3 Next 80B",
            # Deepseek 官方模型
            "deepseek-chat": "DeepSeek Chat",
            "deepseek-reasoner": "DeepSeek Reasoner",
            # 保持向后兼容
            "qwen-turbo": "Qwen Turbo",
            "glm-4-flash": "GLM-4 Flash"
        }
        
        # 反向映射：显示名称 -> 完整名称
        self.display_to_model_map = {v: k for k, v in self.model_display_map.items()}
        
        # 创建设置窗口
        self.window = tk.Toplevel(parent)
        self.window.title("设置")
        self.window.geometry("500x600")
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
        x = (self.window.winfo_screenwidth() // 2) - (500 // 2)
        y = (self.window.winfo_screenheight() // 2) - (600 // 2)
        self.window.geometry(f"500x600+{x}+{y}")
        
    def setup_ui(self):
        """设置界面"""
        # 创建笔记本控件
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # API配置页面
        self.create_api_tab(notebook)
        
        # 翻译设置页面
        self.create_translation_tab(notebook)
        
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
        self.provider_var = tk.StringVar(value=self.api_config.get("provider", "siliconflow"))
        provider_combo = ttk.Combobox(api_frame, textvariable=self.provider_var,
                                     values=["siliconflow", "deepseek"], state="readonly", width=30)
        provider_combo.grid(row=0, column=1, padx=10, pady=10)
        provider_combo.bind("<<ComboboxSelected>>", self.on_provider_changed)
        
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
        current_model = self.api_config.get("model_name", "deepseek-ai/DeepSeek-V3.1-Terminus")
        current_display = self.model_display_map.get(current_model, current_model)
        
        self.model_var = tk.StringVar(value=current_display)
        
        # 根据提供商设置模型选项
        self.siliconflow_models = [
            "DeepSeek V3.2 Exp",
            "DeepSeek V3.1",
            "DeepSeek V3",
            "Kimi K2",
            "Qwen3 Next 80B"
        ]
        
        self.deepseek_models = [
            "DeepSeek Chat",
            "DeepSeek Reasoner"
        ]
        
        # 根据当前提供商选择模型列表
        current_provider = self.provider_var.get()
        if current_provider == "deepseek":
            model_display_values = self.deepseek_models
        else:
            model_display_values = self.siliconflow_models
        
        self.model_combo = ttk.Combobox(api_frame, textvariable=self.model_var,
                                  values=model_display_values, width=30, state="readonly")
        self.model_combo.grid(row=3, column=1, padx=10, pady=10)
        
        # 最大令牌数
        ttk.Label(api_frame, text="最大令牌数:").grid(row=4, column=0, sticky=tk.W, padx=10, pady=10)
        self.max_tokens_var = tk.IntVar(value=self.api_config.get("max_tokens", 4000))
        max_tokens_spin = ttk.Spinbox(api_frame, from_=1000, to=8000, 
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
        
        # 上下文行数
        ttk.Label(trans_frame, text="上下文行数:").grid(row=2, column=0, sticky=tk.W, padx=10, pady=10)
        self.context_lines_var = tk.IntVar(value=self.app_config.get("context_lines", 2))
        context_spin = ttk.Spinbox(trans_frame, from_=0, to=5, 
                                  textvariable=self.context_lines_var, width=33)
        context_spin.grid(row=2, column=1, padx=10, pady=10)
        
        # 批次翻译行数
        ttk.Label(trans_frame, text="批次翻译行数:").grid(row=3, column=0, sticky=tk.W, padx=10, pady=10)
        self.batch_lines_var = tk.IntVar(value=self.app_config.get("batch_lines", 20))
        batch_spin = ttk.Spinbox(trans_frame, from_=5, to=50, increment=5,
                                textvariable=self.batch_lines_var, width=33)
        batch_spin.grid(row=3, column=1, padx=10, pady=10)
        
        # 批次行数提示
        batch_tip = ttk.Label(trans_frame, text="每批翻译的原文行数，使用流式输出提升体验", 
                             foreground="#666", font=('TkDefaultFont', 8))
        batch_tip.grid(row=4, column=1, sticky=tk.W, padx=10, pady=(0, 10))
        
        # 自动保存
        self.auto_save_var = tk.BooleanVar(value=self.app_config.get("auto_save", True))
        auto_save_check = ttk.Checkbutton(trans_frame, text="启用自动保存", 
                                         variable=self.auto_save_var)
        auto_save_check.grid(row=5, column=0, columnspan=2, sticky=tk.W, padx=10, pady=10)
        
        # 翻译提示词
        ttk.Label(trans_frame, text="翻译提示词:").grid(row=6, column=0, sticky=tk.NW, padx=10, pady=10)
        
        prompt_frame = ttk.Frame(trans_frame)
        prompt_frame.grid(row=6, column=1, padx=10, pady=10, sticky=tk.EW)
        
        self.prompt_text = tk.Text(prompt_frame, height=15, width=40, wrap=tk.WORD)
        prompt_scroll = ttk.Scrollbar(prompt_frame, orient=tk.VERTICAL, command=self.prompt_text.yview)
        self.prompt_text.configure(yscrollcommand=prompt_scroll.set)
        
        self.prompt_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        prompt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 插入当前提示词
        self.prompt_text.insert(1.0, self.app_config.get("translation_prompt", ""))
        
        # 重置按钮
        ttk.Button(trans_frame, text="重置为默认", 
                  command=self.reset_prompt).grid(row=7, column=1, padx=10, pady=10, sticky=tk.E)
        
    def on_provider_changed(self, event=None):
        """当提供商改变时更新模型列表、基础URL和API密钥"""
        provider = self.provider_var.get()
        
        # 更新模型列表
        if provider == "deepseek":
            self.model_combo['values'] = self.deepseek_models
            self.model_var.set(self.deepseek_models[0])  # 默认选择第一个
            self.base_url_var.set("https://api.deepseek.com/v1")
        else:  # siliconflow
            self.model_combo['values'] = self.siliconflow_models
            self.model_var.set(self.siliconflow_models[0])  # 默认选择第一个
            self.base_url_var.set("https://api.siliconflow.cn/v1")
        
        # 加载对应提供商的API密钥
        provider_keys = self.api_config.get("provider_keys", {})
        if provider in provider_keys:
            self.api_key_var.set(provider_keys[provider])
        else:
            self.api_key_var.set("")  # 如果没有保存的密钥，清空
    
    def update_temperature_label(self, value):
        """更新温度标签"""
        self.temp_label.config(text=f"{float(value):.1f}")
        
    def reset_prompt(self):
        """重置提示词为默认值"""
        default_prompt = self.config_manager._get_default_prompt()
        self.prompt_text.delete(1.0, tk.END)
        self.prompt_text.insert(1.0, default_prompt)
        
    def test_connection(self):
        """测试API连接"""
        # 将显示名称转换回完整模型名称
        display_model = self.model_var.get()
        actual_model = self.display_to_model_map.get(display_model, display_model)
        
        # 获取当前输入的配置
        test_config = {
            "provider": self.provider_var.get(),
            "api_key": self.api_key_var.get(),
            "base_url": self.base_url_var.get(),
            "model_name": actual_model,
            "max_tokens": self.max_tokens_var.get(),
            "temperature": self.temperature_var.get()
        }
        
        if not test_config["api_key"].strip():
            messagebox.showwarning("测试失败", "请先输入API密钥")
            return
            
        # 在新线程中测试连接
        test_thread = threading.Thread(target=self._test_connection_worker, args=(test_config,))
        test_thread.daemon = True
        test_thread.start()
        
    def _test_connection_worker(self, config):
        """测试连接工作线程"""
        try:
            provider = config.get("provider", "siliconflow")
            
            if provider == "deepseek":
                from ..api.deepseek_api import DeepseekAPI
                api = DeepseekAPI(config)
            else:
                from ..api.siliconflow_api import SiliconFlowAPI
                api = SiliconFlowAPI(config)
            
            success = api.test_connection()
            
            if success:
                self.window.after(0, lambda: messagebox.showinfo("测试成功", "API连接测试成功！"))
            else:
                self.window.after(0, lambda: messagebox.showerror("测试失败", "API连接测试失败，请检查配置"))
                
        except Exception as e:
            self.window.after(0, lambda: messagebox.showerror("测试错误", f"连接测试出错: {str(e)}"))
            
    def save_settings(self):
        """保存设置"""
        try:
            # 将显示名称转换回完整模型名称
            display_model = self.model_var.get()
            actual_model = self.display_to_model_map.get(display_model, display_model)
            
            # 更新API配置
            new_api_config = {
                "provider": self.provider_var.get(),
                "api_key": self.api_key_var.get(),
                "base_url": self.base_url_var.get(),
                "model_name": actual_model,
                "max_tokens": self.max_tokens_var.get(),
                "temperature": self.temperature_var.get()
            }
            
            # 更新应用配置
            new_app_config = {
                "target_language": self.target_lang_var.get(),
                "context_lines": self.context_lines_var.get(),
                "batch_lines": self.batch_lines_var.get(),
                "auto_save": self.auto_save_var.get(),
                "translation_prompt": self.prompt_text.get(1.0, tk.END).strip()
            }
            
            # 保存配置
            if self.config_manager.save_api_config(new_api_config) and \
               self.config_manager.save_app_config(new_app_config):
                messagebox.showinfo("保存成功", "设置已保存")
                
                # 调用回调函数
                if self.callback:
                    self.callback()
                    
                self.window.destroy()
            else:
                messagebox.showerror("保存失败", "设置保存失败")
                
        except Exception as e:
            messagebox.showerror("保存错误", f"保存设置时出错: {str(e)}")
            
    def save_api_preset(self):
        """保存API预设"""
        api_key = self.api_key_var.get().strip()
        model_name = self.model_var.get().strip()
        
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
            self.model_var.set(preset_data.get("model_name", ""))
            
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