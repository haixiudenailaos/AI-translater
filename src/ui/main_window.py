#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主窗口界面模块
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import os
from pathlib import Path

from .settings_window import SettingsWindow
from .glossary_window import GlossaryWindow
from ..core.translator import TranslatorEngine
from ..utils.file_handler import FileHandler

class MainWindow:
    def __init__(self, root, config_manager):
        self.root = root
        self.config_manager = config_manager
        self.translator = TranslatorEngine(config_manager)
        self.file_handler = FileHandler()
        
        # 当前会话的原文/译文文件路径
        self.current_source_path = None
        self.current_target_path = None

        # 续写状态标记：用于首片段的前导换行桥接
        self._continuing_mode = False
        self._continuing_first_insert = False

        # 界面变量
        self.translation_mode = tk.StringVar(value="快速模式")
        self.is_translating = False
        
        self.setup_ui()
        self.setup_bindings()
        
    def setup_ui(self):
        """设置主界面"""
        # 创建主框架
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # 顶部工具栏
        self.create_toolbar(main_frame)
        
        # 中央工作区
        self.create_work_area(main_frame)
        
        # 底部控制面板
        self.create_control_panel(main_frame)
        
        # 状态栏
        self.create_status_bar(main_frame)
        # 初始化API状态显示
        self._on_settings_updated()
        
    def create_toolbar(self, parent):
        """创建顶部工具栏"""
        toolbar_frame = ttk.Frame(parent)
        toolbar_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 左侧按钮组
        left_frame = ttk.Frame(toolbar_frame)
        left_frame.pack(side=tk.LEFT)
        
        ttk.Button(left_frame, text="📁 文件导入", 
                  command=self.import_file).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(left_frame, text="📋 剪贴板导入", 
                  command=self.import_clipboard).pack(side=tk.LEFT, padx=(0, 5))
        
        # 中间模式选择
        middle_frame = ttk.Frame(toolbar_frame)
        middle_frame.pack(side=tk.LEFT, expand=True)
        
        ttk.Label(middle_frame, text="翻译模式:").pack(side=tk.LEFT, padx=(20, 5))
        mode_combo = ttk.Combobox(middle_frame, textvariable=self.translation_mode,
                                 values=["逐行模式", "快速模式"], state="readonly", width=10)
        mode_combo.pack(side=tk.LEFT)
        
        # 右侧按钮组
        right_frame = ttk.Frame(toolbar_frame)
        right_frame.pack(side=tk.RIGHT)
        
        ttk.Button(right_frame, text="📚 术语库", 
                  command=self.open_glossary).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_frame, text="⚙️ 设置", 
                  command=self.open_settings).pack(side=tk.RIGHT, padx=(5, 0))
        
    def create_work_area(self, parent):
        """创建中央工作区 - 世界级UI标准，带行号和同步滚动"""
        work_frame = ttk.Frame(parent)
        work_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # 创建左右面板
        paned_window = ttk.PanedWindow(work_frame, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True)
        
        # 左侧原文面板
        left_frame = ttk.LabelFrame(paned_window, text="原文", padding=2)
        paned_window.add(left_frame, weight=1)
        
        # 创建原文编辑区域（带行号）
        self.create_text_area_with_line_numbers(left_frame, "source")
        
        # 右侧译文面板
        right_frame = ttk.LabelFrame(paned_window, text="译文", padding=2)
        paned_window.add(right_frame, weight=1)
        
        # 创建译文编辑区域（带行号）
        self.create_text_area_with_line_numbers(right_frame, "target")
        
        # 设置双向同步滚动
        self.setup_synchronized_scrolling()
        # 软换行与窗口/布局变化时进行重对齐（防抖）
        self.setup_softwrap_resize_realign()
        
    def create_text_area_with_line_numbers(self, parent, text_type):
        """创建带行号的文本区域"""
        # 创建容器框架
        container = ttk.Frame(parent)
        container.pack(fill=tk.BOTH, expand=True)
        
        # 行号区域
        line_number_frame = tk.Frame(container, width=50, bg="#f0f0f0", relief=tk.SUNKEN, bd=1)
        line_number_frame.pack(side=tk.LEFT, fill=tk.Y)
        line_number_frame.pack_propagate(False)
        
        # 行号文本框
        line_numbers = tk.Text(line_number_frame, width=4, padx=3, pady=5,
                              font=("微软雅黑", 10), bg="#f0f0f0", fg="#666666",
                              state=tk.DISABLED, wrap=tk.NONE, cursor="arrow",
                              takefocus=0, border=0, highlightthickness=0)
        line_numbers.pack(fill=tk.BOTH, expand=True)
        
        # 主文本区域框架
        text_frame = ttk.Frame(container)
        text_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # 主文本框
        text_widget = tk.Text(text_frame, wrap=tk.WORD, font=("微软雅黑", 10),
                             undo=True, maxundo=50, padx=5, pady=5,
                             selectbackground="#316AC5", selectforeground="white",
                             insertbackground="#000000", insertwidth=2)
        
        # 滚动条
        scrollbar = ttk.Scrollbar(text_frame, orient=tk.VERTICAL)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # 配置滚动
        text_widget.configure(yscrollcommand=scrollbar.set)
        scrollbar.configure(command=text_widget.yview)
        
        # 存储引用
        if text_type == "source":
            self.source_text = text_widget
            self.source_line_numbers = line_numbers
            self.source_scrollbar = scrollbar
        else:
            self.target_text = text_widget
            self.target_line_numbers = line_numbers
            self.target_scrollbar = scrollbar
            
        # 绑定事件
        def _on_key_release(event, tt=text_type):
            self.update_line_numbers(tt)
            if tt == "target":
                # 译文输入变更时，触发防抖实时保存
                self._schedule_save_to_target()
        text_widget.bind('<KeyRelease>', _on_key_release)
        text_widget.bind('<Button-1>', lambda e: self.update_line_numbers(text_type))
        text_widget.bind('<MouseWheel>', lambda e: self.on_mousewheel(e, text_type))
        # 行号栏滚轮联动对应文本并触发对齐（阻止行号栏自身滚动）
        def _line_numbers_wheel(event, tt=text_type):
            self.on_mousewheel(event, tt)
            return "break"
        line_numbers.bind('<MouseWheel>', _line_numbers_wheel)
        
        # 初始化行号
        self.update_line_numbers(text_type)
        
    def setup_synchronized_scrolling(self):
        """设置双向同步滚动：以首可视逻辑行号驱动对齐（不以 fraction 直接驱动）"""
        def sync_from_source():
            # 以原文首可视逻辑行作为锚点
            L0 = int(self.source_text.index('@0,0').split('.')[0])
            # 将译文滚动到同一逻辑行
            self.target_text.see(f'{L0}.0')
            # 刷新滚动条与行号视图（仅反馈）
            s_top, s_bottom = self.source_text.yview()
            t_top, t_bottom = self.target_text.yview()
            self.source_scrollbar.set(s_top, s_bottom)
            self.target_scrollbar.set(t_top, t_bottom)
            self.source_line_numbers.yview_moveto(s_top)
            self.target_line_numbers.yview_moveto(t_top)

        def sync_from_target():
            # 以译文首可视逻辑行作为锚点
            L0 = int(self.target_text.index('@0,0').split('.')[0])
            # 将原文滚动到同一逻辑行
            self.source_text.see(f'{L0}.0')
            # 刷新滚动条与行号视图（仅反馈）
            s_top, s_bottom = self.source_text.yview()
            t_top, t_bottom = self.target_text.yview()
            self.source_scrollbar.set(s_top, s_bottom)
            self.target_scrollbar.set(t_top, t_bottom)
            self.source_line_numbers.yview_moveto(s_top)
            self.target_line_numbers.yview_moveto(t_top)

        # 暴露为实例方法，便于其他事件触发重对齐
        self._sync_from_source = sync_from_source
        self._sync_from_target = sync_from_target

        def on_source_scroll(*_args):
            if getattr(self, '_syncing', False):
                # 在同步期间仅更新滚动条反馈，避免递归
                s_top, s_bottom = self.source_text.yview()
                self.source_scrollbar.set(s_top, s_bottom)
                return
            self._syncing = True
            try:
                sync_from_source()
            finally:
                self._syncing = False

        def on_target_scroll(*_args):
            if getattr(self, '_syncing', False):
                t_top, t_bottom = self.target_text.yview()
                self.target_scrollbar.set(t_top, t_bottom)
                return
            self._syncing = True
            try:
                sync_from_target()
            finally:
                self._syncing = False

        # 清除双向同步滚动：仅各自滚动条反馈，不进行跨区域对齐
        self._sync_from_source = lambda: None
        self._sync_from_target = lambda: None
        self.source_text.configure(yscrollcommand=self.source_scrollbar.set)
        self.target_text.configure(yscrollcommand=self.target_scrollbar.set)
        
    def update_line_numbers(self, text_type):
        """更新行号显示"""
        if text_type == "source":
            text_widget = self.source_text
            line_numbers = self.source_line_numbers
        else:
            text_widget = self.target_text
            line_numbers = self.target_line_numbers
            
        # 获取文本内容
        content = text_widget.get(1.0, tk.END)
        lines = content.split('\n')
        line_count = len(lines) - 1  # 减去最后的空行
        
        # 生成行号
        line_numbers_text = '\n'.join(str(i) for i in range(1, line_count + 1))
        
        # 更新行号显示
        line_numbers.configure(state=tk.NORMAL)
        line_numbers.delete(1.0, tk.END)
        line_numbers.insert(1.0, line_numbers_text)
        line_numbers.configure(state=tk.DISABLED)
        
        # 同步行号滚动
        line_numbers.yview_moveto(text_widget.yview()[0])
        
    def on_mousewheel(self, event, text_type):
        """处理鼠标滚轮事件"""
        if text_type == "source":
            self.source_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
            self.update_line_numbers("source")

        else:
            self.target_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
            self.update_line_numbers("target")

        
    def create_control_panel(self, parent):
        """创建底部控制面板"""
        control_frame = ttk.Frame(parent)
        control_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 左侧翻译控制
        left_control = ttk.Frame(control_frame)
        left_control.pack(side=tk.LEFT)
        
        self.translate_btn = ttk.Button(left_control, text="🚀 开始翻译", 
                                       command=self.start_translation)
        self.translate_btn.pack(side=tk.LEFT, padx=(0, 5))

        # 新增：继续翻译（不清空译文，续写到末尾）
        self.continue_btn = ttk.Button(left_control, text="➡️ 继续翻译",
                                       command=self.continue_translation)
        self.continue_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        self.pause_btn = ttk.Button(left_control, text="⏸️ 暂停", 
                                   command=self.pause_translation, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        self.stop_btn = ttk.Button(left_control, text="⏹️ 停止", 
                                  command=self.stop_translation, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        # 右侧文件操作
        right_control = ttk.Frame(control_frame)
        right_control.pack(side=tk.RIGHT)
        
        ttk.Button(right_control, text="💾 保存译文", 
                  command=self.save_translation).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(right_control, text="📄 导出对照", 
                  command=self.export_comparison).pack(side=tk.RIGHT, padx=(5, 0))
        
        # 中间进度条
        middle_control = ttk.Frame(control_frame)
        middle_control.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(20, 20))
        
        ttk.Label(middle_control, text="翻译进度:").pack(side=tk.LEFT)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(middle_control, variable=self.progress_var, 
                                           maximum=100, length=200)
        self.progress_bar.pack(side=tk.LEFT, padx=(5, 0), fill=tk.X, expand=True)
        
        # 底部居中“支持作者”按钮
        support_center = ttk.Frame(parent)
        support_center.pack(fill=tk.X)
        ttk.Button(support_center, text="❤️ 支持作者", 
                   command=self.open_support_dialog).pack(anchor='center', pady=(4, 0))
        
    def create_status_bar(self, parent):
        """创建状态栏"""
        status_frame = ttk.Frame(parent)
        status_frame.pack(fill=tk.X)
        
        # 状态标签
        self.status_label = ttk.Label(status_frame, text="就绪", relief=tk.SUNKEN)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # API状态
        self.api_status_label = ttk.Label(status_frame, text="API: 未配置", relief=tk.SUNKEN)
        self.api_status_label.pack(side=tk.RIGHT, padx=(5, 0))
        
    def setup_bindings(self):
        """设置事件绑定"""
        # 快捷键绑定
        self.root.bind('<Control-o>', lambda e: self.import_file())
        self.root.bind('<Control-v>', lambda e: self.import_clipboard())
        self.root.bind('<Control-s>', lambda e: self.save_translation())
        self.root.bind('<F5>', lambda e: self.start_translation())
        # 快捷键：继续翻译
        self.root.bind('<F6>', lambda e: self.continue_translation())
        # 失焦自动保存：当窗口失去焦点时，触发一次译文保存（立即执行）
        self.root.bind('<FocusOut>', lambda e: self._schedule_save_to_target(0))
        
    def import_file(self):
        """导入文件并自动检测/创建对应译文文件，加载译文并定位到末尾"""
        try:
            file_path = filedialog.askopenfilename(
                title="选择要翻译的文件",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )
            
            if file_path:
                # 记录原文路径
                src_path = Path(file_path)
                self.current_source_path = src_path

                # 加载原文
                content = self.file_handler.read_file(str(src_path))
                self.source_text.delete(1.0, tk.END)
                self.source_text.insert(1.0, content)
                self.update_line_numbers("source")

                # 派生译文路径：同目录、同名加“_译文.txt”
                tgt_path = src_path.with_name(f"{src_path.stem}_译文.txt")
                self.current_target_path = tgt_path

                # 检测并加载/创建译文文件
                if tgt_path.exists():
                    try:
                        tgt_content = self.file_handler.read_file(str(tgt_path))
                    except Exception:
                        tgt_content = ""
                else:
                    # 不存在则创建空文件，确保后续实时保存有目标
                    try:
                        self.file_handler.save_file(str(tgt_path), "")
                    except Exception:
                        # 创建失败不阻塞后续UI填充
                        pass
                    tgt_content = ""

                # 填充译文输入框并光标定位到末尾（续写规则：始终追加到末尾）
                self.target_text.delete(1.0, tk.END)
                if tgt_content:
                    self.target_text.insert(1.0, tgt_content)
                # 将插入点与视图移至结尾
                self.target_text.mark_set(tk.INSERT, tk.END)
                self.target_text.see(tk.END)
                self.update_line_numbers("target")

                # 状态更新
                self.update_status(f"已导入文件: {src_path.name}（译文文件：{tgt_path.name}）")
                
        except Exception as e:
            messagebox.showerror("导入错误", f"文件导入失败: {str(e)}")
            
    def import_clipboard(self):
        """导入剪贴板内容"""
        try:
            clipboard_content = self.root.clipboard_get()
            if clipboard_content.strip():
                self.source_text.delete(1.0, tk.END)
                self.source_text.insert(1.0, clipboard_content)
                # 更新行号
                self.update_line_numbers("source")
                self.update_status("已导入剪贴板内容")
            else:
                messagebox.showwarning("导入警告", "剪贴板内容为空")
                
        except tk.TclError:
            messagebox.showerror("导入错误", "无法获取剪贴板内容")
            
    def start_translation(self):
        """开始翻译"""
        # 非续写模式：复位续写标记
        self._continuing_mode = False
        self._continuing_first_insert = False

        source_content = self.source_text.get(1.0, tk.END).strip()
        if not source_content:
            messagebox.showwarning("翻译警告", "请先输入要翻译的文本")
            return
            
        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return
            
        # 更新界面状态
        self.is_translating = True
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL)
        
        # 清空译文区域
        self.target_text.delete(1.0, tk.END)
        
        # 在新线程中执行翻译
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(source_content, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()

    def continue_translation(self):
        """继续翻译：不清空译文，按行号对齐从原文剩余部分开始续译并在末尾追加"""
        # 启用续写模式标记（首片段桥接）
        self._continuing_mode = True
        self._continuing_first_insert = True

        # 若当前为暂停状态且未停止，优先恢复原翻译线程，不新开线程
        try:
            if self.is_translating and getattr(self.translator, 'is_paused', False) and not getattr(self.translator, 'is_stopped', False):
                self.translator.resume()
                # 恢复后避免重复点击
                self.continue_btn.config(state=tk.DISABLED)
                self.update_status("继续翻译（恢复暂停）")
                return
        except Exception:
            # 若无法判断状态，则走正常续译流程
            pass

        # 在继续前，若末尾存在显式换行/空行，删除一个以避免续写开头出现多余空行
        try:
            last_char = self.target_text.get("end-2c", "end-1c")
            if last_char == "\n":
                self.target_text.delete("end-2c", "end-1c")
        except Exception:
            pass

        # 原文/译文内容
        source_content = self.source_text.get(1.0, tk.END)
        target_content = self.target_text.get(1.0, tk.END)

        src_lines = source_content.split('\n')
        tgt_lines = target_content.split('\n')

        # 计算未翻译起点：以译文当前行数为锚（严格按行号）
        start_idx = len(tgt_lines) - 1  # Text末尾有空行，减去1
        if start_idx < 0:
            start_idx = 0

        # 取剩余原文
        remaining_lines = src_lines[start_idx:] if start_idx < len(src_lines) else []
        remaining_content = '\n'.join(remaining_lines).strip()

        if not remaining_content:
            messagebox.showinfo("提示", "当前无可继续的原文内容，已全部翻译或原文为空。")
            return

        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return

        # 更新界面状态（不清空译文）
        self.is_translating = True
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL)

        # 光标置于末尾以便续写显示
        self.target_text.mark_set(tk.INSERT, tk.END)
        self.target_text.see(tk.END)

        # 在新线程中执行翻译，对剩余内容进行
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(remaining_content, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()
    def _translate_worker(self, content, mode):
        """翻译工作线程"""
        try:
            self.update_status("正在翻译...")
            
            if mode == "逐行模式":
                self.translator.translate_line_by_line(
                    content, 
                    self._on_translation_progress,
                    self._on_translation_complete
                )
            else:
                self.translator.translate_fast_mode(
                    content,
                    self._on_translation_progress,
                    self._on_translation_complete
                )
                
        except Exception as e:
            self.root.after(0, lambda: self._on_translation_error(str(e)))
            
    def _on_translation_progress(self, progress, translated_text):
        """翻译进度回调"""
        def update_ui():
            self.progress_var.set(progress)
            if translated_text:
                insert_text = translated_text
                # 续写模式首片段桥接：目标末尾已是换行且新片段也以换行开头，则去掉一个前导换行
                if self._continuing_mode and self._continuing_first_insert:
                    try:
                        current_tail = self.target_text.get("end-2c", "end-1c")
                        if current_tail == "\n" and insert_text.startswith("\n"):
                            insert_text = insert_text[1:]
                    except Exception:
                        pass
                    # 首次处理后关闭标记
                    self._continuing_first_insert = False

                self.target_text.insert(tk.END, insert_text)
                self.target_text.see(tk.END)
                # 更新行号
                self.update_line_numbers("target")
                # 实时保存（防抖）
                self._schedule_save_to_target()

                
        self.root.after(0, update_ui)
        
    def _on_translation_complete(self):
        """翻译完成回调"""
        def update_ui():
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.pause_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.DISABLED)
            self.progress_var.set(100)
            self.update_status("翻译完成")
            # 复位续写标记
            self._continuing_mode = False
            self._continuing_first_insert = False
            
        self.root.after(0, update_ui)
        
    def _on_translation_error(self, error_msg):
        """翻译错误回调"""
        self.is_translating = False
        self.translate_btn.config(state=tk.NORMAL)
        self.pause_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)
        self.update_status("翻译失败")
        messagebox.showerror("翻译错误", f"翻译过程中出现错误: {error_msg}")
        
    def pause_translation(self):
        """暂停翻译"""
        if self.is_translating:
            self.translator.pause()
            self.update_status("翻译已暂停")
            # 暂停后允许“继续翻译”
            self.continue_btn.config(state=tk.NORMAL)
            
    def stop_translation(self):
        """停止翻译"""
        if self.is_translating:
            # 立即停止翻译并取消API请求
            self.translator.stop()
            self.is_translating = False
            
            # 更新UI状态
            self.translate_btn.config(state=tk.NORMAL)
            self.pause_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.DISABLED)
            # 停止后允许“继续翻译”
            self.continue_btn.config(state=tk.NORMAL)
            self.update_status("翻译已停止")
            
            # 重置进度条
            self.progress_var.set(0)
            
    def save_translation(self):
        """保存译文"""
        try:
            translated_content = self.target_text.get(1.0, tk.END).strip()
            if not translated_content:
                messagebox.showwarning("保存警告", "没有可保存的译文")
                return
                
            file_path = filedialog.asksaveasfilename(
                title="保存译文",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )
            
            if file_path:
                self.file_handler.save_file(file_path, translated_content)
                self.update_status(f"译文已保存: {Path(file_path).name}")
                
        except Exception as e:
            messagebox.showerror("保存错误", f"保存译文失败: {str(e)}")
            
    def export_comparison(self):
        """导出对照文件"""
        try:
            source_content = self.source_text.get(1.0, tk.END).strip()
            target_content = self.target_text.get(1.0, tk.END).strip()
            
            if not source_content or not target_content:
                messagebox.showwarning("导出警告", "原文或译文为空")
                return
                
            file_path = filedialog.asksaveasfilename(
                title="导出对照文件",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )
            
            if file_path:
                comparison_content = self.file_handler.create_comparison_file(
                    source_content, target_content
                )
                self.file_handler.save_file(file_path, comparison_content)
                self.update_status(f"对照文件已导出: {Path(file_path).name}")
                
        except Exception as e:
            messagebox.showerror("导出错误", f"导出对照文件失败: {str(e)}")
            
    def open_settings(self):
        """打开设置窗口"""
        SettingsWindow(self.root, self.config_manager, self._on_settings_updated)
        
    def open_glossary(self):
        """打开术语库窗口"""
        GlossaryWindow(self.root, self.config_manager)

    def open_support_dialog(self):
        """支持作者弹窗：展示邮箱与捐助二维码（微信/支付宝）"""
        win = tk.Toplevel(self.root)
        win.title("支持作者")
        win.transient(self.root)
        win.resizable(False, False)

        container = ttk.Frame(win, padding=10)
        container.pack(fill=tk.BOTH, expand=True)

        # 邮箱行
        email_frame = ttk.Frame(container)
        email_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(email_frame, text="作者邮箱：", font=("微软雅黑", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(email_frame, text="996043050@qq.com", font=("微软雅黑", 10)).pack(side=tk.LEFT, padx=(5, 10))

        def copy_email():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append("996043050@qq.com")
                messagebox.showinfo("复制成功", "邮箱已复制到剪贴板")
            except Exception as _:
                messagebox.showwarning("提示", "复制失败，请手动复制")

        ttk.Button(email_frame, text="复制邮箱", command=copy_email).pack(side=tk.LEFT)

        # 二维码区域
        qr_frame = ttk.Frame(container)
        qr_frame.pack(fill=tk.BOTH, expand=True)

        # 保持图片引用避免被GC
        if not hasattr(self, "_donate_images"):
            self._donate_images = {}

        def add_qr(panel_title, img_path: Path, caption: str, key: str):
            panel = ttk.Labelframe(qr_frame, text=panel_title, padding=8)
            panel.pack(side=tk.LEFT, padx=8, pady=4, fill=tk.BOTH, expand=True)

            # 图片
            try:
                if img_path.exists():
                    self._donate_images[key] = tk.PhotoImage(file=str(img_path))
                    tk.Label(panel, image=self._donate_images[key]).pack()
                else:
                    ttk.Label(panel, text="未找到图片：" + str(img_path), foreground="#AA0000").pack()
            except Exception as e:
                ttk.Label(panel, text=f"图片加载失败：{e}", foreground="#AA0000").pack()

            # 说明文案
            ttk.Label(panel, text=caption, font=("微软雅黑", 9)).pack(pady=(6, 0))

        wechat_path = Path("assets") / "donate_wechat.png"
        alipay_path = Path("assets") / "donate_alipay.png"

        add_qr("微信", wechat_path, "捐助作者方式：微信", "wechat")
        add_qr("支付宝", alipay_path, "捐助作者方式：支付宝", "alipay")

        # 关闭按钮
        ttk.Frame(container).pack(pady=(8, 0))
        ttk.Button(container, text="关闭", command=win.destroy).pack()

    def _on_settings_updated(self):
        """设置更新回调"""
        # 刷新翻译引擎的API配置
        self.translator.refresh_api()
        
        # 更新API状态显示
        if self.config_manager.is_api_configured():
            api_config = self.config_manager.get_api_config()
            model_name = api_config.get("model_name", "未知模型")
            self.api_status_label.config(text=f"API: 已配置 ({model_name})")
        else:
            self.api_status_label.config(text="API: 未配置")
            
    def update_status(self, message):
        """更新状态栏"""
        self.status_label.config(text=message)

    # 实时保存：防抖调度 + 原子写入到当前译文文件
    def _schedule_save_to_target(self, delay_ms: int = 400):
        """为译文内容变更安排一次防抖保存"""
        # 若无当前译文文件路径，跳过
        if not getattr(self, 'current_target_path', None):
            return
        # 使用统一防抖器，避免频繁IO
        self._debounce('save_tgt', delay_ms, self._atomic_save_target)

    def _atomic_save_target(self):
        """将译文框内容原子性写入当前译文文件"""
        try:
            tgt_path = getattr(self, 'current_target_path', None)
            if not tgt_path:
                return
            # 获取当前译文内容（保留末尾换行由Text控件决定）
            content = self.target_text.get(1.0, tk.END)
            # 临时文件与原子替换
            tmp_path = tgt_path.with_suffix(tgt_path.suffix + '.tmp')
            # 先写临时文件
            self.file_handler.write_file(str(tmp_path), content)
            # 原子替换目标文件
            os.replace(str(tmp_path), str(tgt_path))
        except Exception:
            # 静默容错，不打断用户操作
            pass

    # Step6: 软换行与窗口变化重对齐 + 防抖
    def _debounce(self, key, delay_ms, func):
        """简单防抖：避免高频事件导致卡顿"""
        if not hasattr(self, '_debouncers'):
            self._debouncers = {}
        # 取消已有的延迟任务
        if key in self._debouncers:
            try:
                self.root.after_cancel(self._debouncers[key])
            except Exception:
                pass
        # 安排新的任务
        self._debouncers[key] = self.root.after(delay_ms, func)

    def setup_softwrap_resize_realign(self):
        """在布局/尺寸变化（<Configure>）时触发重对齐，以首可视逻辑行号为锚点"""
        # 原文区域布局变化时，用原文首可视行驱动整体对齐
        self.source_text.bind(
            '<Configure>',
            lambda e: self._debounce('src_cfg', 80, getattr(self, '_sync_from_source', lambda: None))
        )
        # 译文区域布局变化时，用译文首可视行驱动整体对齐
        self.target_text.bind(
            '<Configure>',
            lambda e: self._debounce('tgt_cfg', 80, getattr(self, '_sync_from_target', lambda: None))
        )
        # 可选：根窗口变化时也进行一次重对齐（默认注释）
        # self.root.bind('<Configure>', lambda e: self._debounce('root_cfg', 100, getattr(self, '_sync_from_source', lambda: None)))