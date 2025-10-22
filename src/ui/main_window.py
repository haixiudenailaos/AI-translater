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
from ..core.epub_processor import EPUBProcessor

class MainWindow:
    def __init__(self, root, config_manager):
        self.root = root
        self.config_manager = config_manager
        self.translator = TranslatorEngine(config_manager)
        self.file_handler = FileHandler()
        self.epub_processor = EPUBProcessor()
        
        # 当前会话的原文/译文文件路径
        self.current_source_path = None
        self.current_target_path = None
        # EPUB映射相关
        self.current_mapping_dir = None
        self.current_mapping_keys = []

        # 续写状态标记：用于首片段的前导换行桥接
        self._continuing_mode = False
        self._continuing_first_insert = False
        
        # 翻译累积缓冲区：用于收集流式翻译的片段
        self._translation_buffer = []  # 累积当前翻译的所有片段
        self._continue_start_line = 0  # 续翻起始行

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
        """创建中央工作区 - 表格形式展示行号、原文、译文，支持双击单元格编辑"""
        work_frame = ttk.LabelFrame(parent, text="翻译内容对照表", padding=5)
        work_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # 创建表格容器
        table_container = ttk.Frame(work_frame)
        table_container.pack(fill=tk.BOTH, expand=True)
        
        # 创建Treeview表格
        columns = ('line_number', 'source_text', 'target_text')
        self.translation_table = ttk.Treeview(
            table_container,
            columns=columns,
            show='headings',
            selectmode='extended'  # ✅ 修改为extended模式，支持多选
        )
        
        # 设置列标题
        self.translation_table.heading('line_number', text='行号')
        self.translation_table.heading('source_text', text='原文')
        self.translation_table.heading('target_text', text='译文')
        
        # 设置列宽
        self.translation_table.column('line_number', width=60, minwidth=50, anchor='center', stretch=False)
        self.translation_table.column('source_text', width=400, minwidth=200, anchor='w')
        self.translation_table.column('target_text', width=400, minwidth=200, anchor='w')
        
        # 添加垂直滚动条
        vsb = ttk.Scrollbar(table_container, orient="vertical", command=self.translation_table.yview)
        self.translation_table.configure(yscrollcommand=vsb.set)
        
        # 添加水平滚动条
        hsb = ttk.Scrollbar(table_container, orient="horizontal", command=self.translation_table.xview)
        self.translation_table.configure(xscrollcommand=hsb.set)
        
        # 布局表格和滚动条
        self.translation_table.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        
        # 配置网格权重
        table_container.grid_rowconfigure(0, weight=1)
        table_container.grid_columnconfigure(0, weight=1)
        
        # 绑定双击编辑事件
        self.translation_table.bind('<Double-Button-1>', self.on_cell_double_click)
        
        # ✅ 新增：绑定右键菜单事件
        self.translation_table.bind('<Button-3>', self.show_context_menu)
        
        # ✅ 新增：创建右键菜单
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="翻译选中行", command=self.translate_selected_rows)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="取消", command=lambda: self.context_menu.unpost())
        
        # 初始化表格样式
        self.setup_table_styles()
        
        # 初始化编辑控件（隐藏的Entry，用于单元格编辑）
        self.edit_entry = None
        self.editing_item = None
        self.editing_column = None
        
    def setup_table_styles(self):
        """设置表格样式"""
        style = ttk.Style()
        
        # 配置Treeview样式
        style.configure(
            "Treeview",
            font=('微软雅黑', 10),
            rowheight=30,
            background="white"
        )
        
        style.configure(
            "Treeview.Heading",
            font=('微软雅黑', 10, 'bold'),
            background="#e0e0e0",
            foreground="#333"
        )
        
        # 配置选中行颜色
        style.map('Treeview', 
                  background=[('selected', '#0078D7')],
                  foreground=[('selected', 'white')])
    
    def on_cell_double_click(self, event):
        """双击单元格进行编辑"""
        # 如果正在编辑，先保存
        if self.edit_entry:
            self.save_cell_edit()
        
        # 获取点击的区域
        region = self.translation_table.identify_region(event.x, event.y)
        if region != "cell":
            return
        
        # 获取点击的列
        column = self.translation_table.identify_column(event.x)
        # column返回值如 '#1', '#2', '#3'
        column_index = int(column.replace('#', '')) - 1
        
        # 只允许编辑原文列(1)和译文列(2)
        if column_index not in [1, 2]:
            return
        
        # 获取点击的行
        item = self.translation_table.identify_row(event.y)
        if not item:
            return
        
        # 获取单元格的值和位置
        values = self.translation_table.item(item)['values']
        if not values or column_index >= len(values):
            return
        
        cell_value = values[column_index]
        
        # 获取单元格的边界框
        bbox = self.translation_table.bbox(item, column)
        if not bbox:
            return
        
        # 创建编辑Entry
        self.editing_item = item
        self.editing_column = column_index
        
        self.edit_entry = tk.Entry(
            self.translation_table,
            font=('微软雅黑', 10),
            relief=tk.SOLID,
            borderwidth=1
        )
        self.edit_entry.insert(0, cell_value)
        self.edit_entry.select_range(0, tk.END)
        self.edit_entry.focus_set()
        
        # 放置Entry到单元格位置
        self.edit_entry.place(
            x=bbox[0],
            y=bbox[1],
            width=bbox[2],
            height=bbox[3]
        )
        
        # 绑定事件
        self.edit_entry.bind('<Return>', lambda e: self.save_cell_edit())
        self.edit_entry.bind('<Escape>', lambda e: self.cancel_cell_edit())
        self.edit_entry.bind('<FocusOut>', lambda e: self.save_cell_edit())
    
    def save_cell_edit(self):
        """保存单元格编辑（按line_number严格对齐，并更新translated_at时间戳）"""
        if not self.edit_entry or not self.editing_item:
            return
        
        # 获取新值
        new_value = self.edit_entry.get()
        
        # 更新表格
        values = list(self.translation_table.item(self.editing_item)['values'])
        old_value = values[self.editing_column]
        values[self.editing_column] = new_value
        self.translation_table.item(self.editing_item, values=values)
        
        # 销毁编辑控件
        self.edit_entry.destroy()
        self.edit_entry = None
        self.editing_item = None
        self.editing_column = None
        
        # 只有当内容真正改变时才触发保存
        if old_value != new_value:
            # 触发保存（包含时间戳更新）
            self._schedule_save_to_target()
    
    def cancel_cell_edit(self):
        """取消单元格编辑"""
        if self.edit_entry:
            self.edit_entry.destroy()
            self.edit_entry = None
            self.editing_item = None
            self.editing_column = None
        
    def load_data_to_table(self, source_lines, target_lines=None):
        """加载数据到表格（严格按行号对齐，从1开始）
        
        改进说明：
        1. 移除source_lines末尾的空行
        2. 确保译文行数与原文一致（补齐或截断）
        3. 行号从1开始，与每个条目的line_number一致
        4. 未翻译的行用空字符串表示
        """
        # 清空现有数据
        for item in self.translation_table.get_children():
            self.translation_table.delete(item)
        
        # 过滤空行，确保数据一致性
        # 移除source_lines末尾的空行
        while source_lines and not source_lines[-1].strip():
            source_lines.pop()
        
        # 确保译文行数与原文一致
        if target_lines is None:
            target_lines = []
        else:
            # 移除target_lines末尾多余的空行
            while len(target_lines) > len(source_lines) and not target_lines[-1].strip():
                target_lines.pop()
        
        # 补齐译文行数到与原文一致（关键：严格按行号对齐）
        while len(target_lines) < len(source_lines):
            target_lines.append("")
        
        # 截断多余的译文行
        if len(target_lines) > len(source_lines):
            target_lines = target_lines[:len(source_lines)]
        
        # 插入数据到表格（行号从1开始）
        for i, source in enumerate(source_lines):
            line_num = i + 1  # 行号从1开始，与line_number保持一致
            target = target_lines[i] if i < len(target_lines) else ""
            
            self.translation_table.insert(
                '',
                'end',
                values=(line_num, source, target),
                tags=('evenrow' if i % 2 == 0 else 'oddrow',)
            )
        
        # 设置交替行颜色
        self.translation_table.tag_configure('evenrow', background='#f9f9f9')
        self.translation_table.tag_configure('oddrow', background='white')
    
    def get_table_data(self):
        """从表格获取所有数据"""
        source_lines = []
        target_lines = []
        
        for item in self.translation_table.get_children():
            values = self.translation_table.item(item)['values']
            if values:
                source_lines.append(values[1])
                target_lines.append(values[2] if len(values) > 2 else "")
        
        return source_lines, target_lines
        
    def refresh_table_display(self):
        """刷新表格显示（在数据更新后调用）"""
        # 保存当前选中项
        selection = self.translation_table.selection()
        selected_index = None
        if selection:
            selected_index = self.translation_table.index(selection[0])
        
        # 重新应用样式
        for i, item in enumerate(self.translation_table.get_children()):
            tag = 'evenrow' if i % 2 == 0 else 'oddrow'
            self.translation_table.item(item, tags=(tag,))
        
        # 恢复选中项
        if selected_index is not None:
            items = self.translation_table.get_children()
            if selected_index < len(items):
                self.translation_table.selection_set(items[selected_index])
                self.translation_table.focus(items[selected_index])
                self.translation_table.see(items[selected_index])
        
    def update_table_row(self, line_number, source_text=None, target_text=None):
        """更新表格中指定行的数据"""
        items = self.translation_table.get_children()
        if line_number - 1 < len(items):
            item = items[line_number - 1]
            values = list(self.translation_table.item(item)['values'])
            
            if source_text is not None:
                values[1] = source_text
            if target_text is not None:
                values[2] = target_text
            
            self.translation_table.item(item, values=values)

        
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
        ttk.Button(right_control, text="📦 导出为EPUB", 
                  command=self.export_epub_file).pack(side=tk.RIGHT, padx=(5, 0))
        
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
                filetypes=[
                    ("文本文件", "*.txt"), 
                    ("EPUB电子书", "*.epub"),
                    ("所有文件", "*.*")
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

                # 派生译文路径：同目录、同名加“_译文.txt”
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
                        self.file_handler.save_file(str(tgt_path), "")
                    except Exception:
                        pass

                # 加载数据到表格
                self.load_data_to_table(source_lines, target_lines)

                # 状态更新
                self.update_status(f"已导入文件: {src_path.name}（译文文件：{tgt_path.name}）")
                
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
                self.root.after(0, lambda: status_label.config(text="正在解析EPUB结构..."))
                
                # 生成/刷新mapping
                mapping_info = self.epub_processor.import_epub(str(src_path))
                
                self.root.after(0, lambda: status_label.config(text="正在加载内容映射..."))
                
                # 存储映射目录
                self.current_mapping_dir = Path(mapping_info["mapping_dir"])
                
                # 加载原文和译文列表（完全重构：直接返回原文和译文）
                originals, translations = self.epub_processor.load_content_mapping(str(self.current_mapping_dir))
                
                self.root.after(0, lambda: status_label.config(text="正在填充原文区域..."))
                
                # 派生译文路径（保留向后兼容）
                tgt_path = src_path.with_name(f"{src_path.stem}_译文.txt")
                self.current_target_path = tgt_path
                
                # 在主线程中更新UI
                def update_ui():
                    # 加载数据到表格（严格按行号对齐）
                    self.load_data_to_table(originals, translations)
                    
                    # 显示映射目录名称（相对路径）
                    mapping_display = self.current_mapping_dir.relative_to(src_path.parent)
                    self.update_status(f"已导入EPUB: {src_path.name}（映射目录：{mapping_display}）")
                
                self.root.after(0, update_ui)
                result_container['success'] = True
                
            except Exception as e:
                result_container['error'] = str(e)
            finally:
                # 关闭弹窗并恢复UI
                self.root.after(0, lambda: self._finish_epub_import(progress_dialog, result_container, src_path.name))
        
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
        
        # 特别处理翻译控制按钮
        try:
            if hasattr(self, 'is_translating') and self.is_translating:
                self.translate_btn.config(state=tk.DISABLED)
                self.continue_btn.config(state=tk.DISABLED)
            else:
                self.translate_btn.config(state=tk.NORMAL)
                self.continue_btn.config(state=tk.NORMAL)
                self.stop_btn.config(state=tk.DISABLED)
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
            
    def import_clipboard(self):
        """导入剪贴板内容"""
        try:
            clipboard_content = self.root.clipboard_get()
            if clipboard_content.strip():
                source_lines = clipboard_content.splitlines()
                self.load_data_to_table(source_lines, [])
                self.update_status("已导入剪贴板内容")
            else:
                messagebox.showwarning("导入警告", "剪贴板内容为空")
                
        except tk.TclError:
            messagebox.showerror("导入错误", "无法获取剪贴板内容")
            
    def start_translation(self):
        """开始翻译（完全重构：分批翻译机制）"""
        # 从表格获取原文
        source_lines, _ = self.get_table_data()
        if not source_lines or not any(line.strip() for line in source_lines):
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
        self.stop_btn.config(state=tk.NORMAL)
        
        # ✅ 重置续翻起始行（开始翻译时从0开始）
        self._continue_start_line = 0
        
        # 清空译文列
        for item in self.translation_table.get_children():
            values = list(self.translation_table.item(item)['values'])
            values[2] = ""  # 清空译文
            self.translation_table.item(item, values=values)
        
        # 在新线程中执行翻译
        source_content = "\n".join(source_lines)
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(source_content, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()

    def continue_translation(self):
        """继续翻译（修复：智能检查空译文行，确保完整翻译）
        
        核心逻辑：
        1. 检查所有行，找出需要翻译的行（原文不为空但译文为空）
        2. 如果存在需要翻译的行，则进入翻译流程
        3. 只有当所有原文行都有对应的非空译文时，才提示翻译完成
        4. 不清除任何已有的译文，保持已翻译内容不变
        """
        # 获取原文和译文
        source_lines, target_lines = self.get_table_data()

        # ✅ 新逻辑：检查所有行，找出需要翻译的行
        need_translation_indices = []
        for i in range(len(source_lines)):
            source_text = source_lines[i].strip() if i < len(source_lines) else ""
            target_text = target_lines[i].strip() if i < len(target_lines) else ""
            
            # 如果原文不为空但译文为空，则需要翻译
            if source_text and not target_text:
                need_translation_indices.append(i)
        
        # 如果没有需要翻译的行，说明全部翻译完成
        if not need_translation_indices:
            messagebox.showinfo("提示", "所有内容已翻译完成。")
            return
        
        # ✅ 找到第一个需要翻译的行作为起始位置
        start_idx = need_translation_indices[0]
        
        # 取剩余原文（从第一个需要翻译的行开始）
        remaining_lines = source_lines[start_idx:] if start_idx < len(source_lines) else []
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
        self.stop_btn.config(state=tk.NORMAL)

        # 记录续译起始行（用于回调中计算绝对位置）
        self._continue_start_line = start_idx

        # 在新线程中执行翻译，对剩余内容进行
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(remaining_content, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()
    
    def show_context_menu(self, event):
        """显示右键菜单
        
        新增功能：当用户右键点击表格时，显示上下文菜单
        支持多选行进行翻译
        """
        # 获取点击位置的行
        item = self.translation_table.identify_row(event.y)
        
        if item:
            # 如果点击的行不在选中列表中，则选中该行
            selection = self.translation_table.selection()
            if item not in selection:
                self.translation_table.selection_set(item)
            
            # 显示菜单
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()
    
    def translate_selected_rows(self):
        """翻译选中的行
        
        新墟功能：
        1. 获取用户选中的所有行
        2. 提取这些行的原文
        3. 单独翻译这些行
        4. 将翻译结果写回对应的译文栏
        """
        # 获取选中的行
        selection = self.translation_table.selection()
        if not selection:
            messagebox.showwarning("翻译警告", "请先选中要翻译的行")
            return
        
        # 检查API配置
        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return
        
        # 提取选中行的原文和位置信息
        selected_data = []
        for item in selection:
            values = self.translation_table.item(item)['values']
            if values:
                line_num = values[0]  # 行号
                source_text = values[1]  # 原文
                selected_data.append({
                    'item': item,
                    'line_num': line_num,
                    'source_text': source_text
                })
        
        if not selected_data:
            messagebox.showwarning("翻译警告", "选中的行没有内容")
            return
        
        # 更新界面状态
        self.is_translating = True
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        
        # 合并原文用于翻译
        source_texts = [data['source_text'] for data in selected_data]
        combined_source = '\n'.join(source_texts)
        
        # 记录选中的数据，用于回调中写回结果
        self._selected_translation_data = selected_data
        
        # 在新线程中执行翻译
        translation_thread = threading.Thread(
            target=self._translate_selected_worker,
            args=(combined_source, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()
        
        self.update_status(f"正在翻译选中的 {len(selected_data)} 行...")
    
    def _translate_selected_worker(self, content, mode):
        """翻译选中行的工作线程"""
        try:
            # 使用快速模式进行翻译
            self.translator.translate_fast_mode(
                content,
                self._on_selected_translation_progress,
                self._on_selected_translation_complete
            )
        except Exception as e:
            self.root.after(0, lambda: self._on_translation_error(str(e)))
    
    def _on_selected_translation_progress(self, progress, batch_data):
        """选中行翻译进度回调"""
        def update_ui():
            # 更新进度条
            if progress >= 0:
                self.progress_var.set(progress)
                self.progress_bar.update_idletasks()
            
            if not batch_data or not isinstance(batch_data, dict):
                return
            
            # 获取选中的数据
            selected_data = getattr(self, '_selected_translation_data', [])
            if not selected_data:
                return
            
            batch_start = batch_data.get('batch_start', 0)
            is_streaming = batch_data.get('streaming', False)
            
            if is_streaming:
                # 流式输出模式：实时显示当前翻译结果
                current_text = batch_data.get('current_text', '')
                expected_lines = batch_data.get('expected_lines', 1)
                
                # ✅ 彻底过滤空行：将连续的多个空行合并为一个，避免大量空行堆积
                all_lines = current_text.split('\n')
                streaming_lines = []
                prev_empty = False
                for line in all_lines:
                    if line.strip():  # 有内容的行
                        streaming_lines.append(line)
                        prev_empty = False
                    else:  # 空行
                        # 只在前一行不是空行时才保留一个空行
                        if not prev_empty and streaming_lines:  # 且不是第一行
                            streaming_lines.append('')
                            prev_empty = True
                        # 否则跳过这个空行
                
                # 实时显示：不超过预期行数
                for i, line in enumerate(streaming_lines[:expected_lines]):
                    row_index = batch_start + i
                    if row_index < len(selected_data):
                        item = selected_data[row_index]['item']
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = line.strip()  # 实时更新译文栏
                        self.translation_table.item(item, values=values)
                        
                        # 滚动到当前行
                        self.translation_table.see(item)
            else:
                # 批次完成模式：写入最终结果
                translated_lines = batch_data.get('translated_lines', [])
                
                # 将翻译结果写回对应的行
                for i, translated_line in enumerate(translated_lines):
                    row_index = batch_start + i
                    if row_index < len(selected_data):
                        item = selected_data[row_index]['item']
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = translated_line.strip()  # 更新译文
                        self.translation_table.item(item, values=values)
                
                # 触发保存
                self._schedule_save_to_target()
        
        self.root.after(0, update_ui)
    
    def _on_selected_translation_complete(self):
        """选中行翻译完成回调"""
        def complete_ui():
            # 恢复界面状态
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            
            # 设置进度为100%
            self.progress_var.set(100)
            
            # 获取翻译的行数
            selected_count = len(getattr(self, '_selected_translation_data', []))
            
            # 清理临时数据
            if hasattr(self, '_selected_translation_data'):
                delattr(self, '_selected_translation_data')
            
            # 立即保存
            self._schedule_save_to_target(delay_ms=0)
            
            self.update_status(f"选中的 {selected_count} 行翻译完成")
            messagebox.showinfo("翻译完成", f"已完成 {selected_count} 行的翻译")
        
        self.root.after(0, complete_ui)
    
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
            
    def _on_translation_progress(self, progress, batch_data):
        """翻译进度回调（修复：确保进度条持续可见，避免多余空行）
        
        参数：
            progress: 进度百分比
            batch_data: {
                'batch_start': 起始行号（0-based）,
                'streaming': True/False,  # 是否为流式输出
                'current_text': '当前流式文本',  # streaming=True时有效
                'expected_lines': 预期行数,  # streaming=True时有效
                'translated_lines': 译文列表  # streaming=False时有效
            }
        
        核心逻辑：
        1. 如果streaming=True，实时显示流式翻译结果
        2. 如果streaming=False，将批次翻译完成的结果写入表格
        3. 不依赖换行符拆分，只按行号对应
        """
        def update_ui():
            # ✅ 修复1：确保进度条始终更新并可见
            if progress >= 0:
                self.progress_var.set(progress)
                # 强制刷新进度条显示
                self.progress_bar.update_idletasks()
            
            if not batch_data or not isinstance(batch_data, dict):
                return
            
            batch_start = batch_data.get('batch_start', 0)
            is_streaming = batch_data.get('streaming', False)
            
            # 获取所有表格项
            items = self.translation_table.get_children()
            if not items:
                return
            
            # 计算绝对位置：如果是续翻，需要加上续翻起始偏移
            continue_offset = getattr(self, '_continue_start_line', 0)
            absolute_start = continue_offset + batch_start
            
            if is_streaming:
                # 流式输出模式：实时显示当前翻译结果
                current_text = batch_data.get('current_text', '')
                expected_lines = batch_data.get('expected_lines', 1)
                
                # ✅ 修复2：过滤空行，避免多余空行
                # 将当前文本按换行符拆分（仅用于实时显示）
                streaming_lines = [line for line in current_text.split('\n') if line.strip()]
                
                # 实时显示：不超过预期行数
                for i, line in enumerate(streaming_lines[:expected_lines]):
                    row_index = absolute_start + i
                    if row_index < len(items):
                        item = items[row_index]
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = line.strip()  # 实时更新译文栏
                        self.translation_table.item(item, values=values)
                
                # 滚动到最后更新的行
                last_row = absolute_start + min(len(streaming_lines), expected_lines) - 1
                if 0 <= last_row < len(items):
                    self.translation_table.see(items[last_row])
                    
            else:
                # 批次完成模式：写入最终结果
                translated_lines = batch_data.get('translated_lines', [])
                
                # ✅ 关键：严格按行号写入译文
                for i, translated_line in enumerate(translated_lines):
                    row_index = absolute_start + i
                    if row_index < len(items):
                        item = items[row_index]
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = translated_line.strip()
                        self.translation_table.item(item, values=values)
                
                # 滚动到最后更新的行
                last_row = absolute_start + len(translated_lines) - 1
                if 0 <= last_row < len(items):
                    self.translation_table.see(items[last_row])
                
                # 实时保存（防抖）
                self._schedule_save_to_target()
                    
        self.root.after(0, update_ui)
        
    def _on_translation_complete(self):
        """翻译完成回调（新增：自动翻译查漏机制）"""
        def update_ui():
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.progress_var.set(100)
            self.update_status("翻译完成")
            # 复位续写标记
            self._continuing_mode = False
            self._continuing_first_insert = False
            
            # ✅ 新增：启动翻译查漏机制
            self.root.after(500, self._start_missing_translation_check)
            
        self.root.after(0, update_ui)
        
    def _on_translation_error(self, error_msg):
        """翻译错误回调"""
        self.is_translating = False
        self.translate_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.update_status("翻译失败")
        messagebox.showerror("翻译错误", f"翻译过程中出现错误: {error_msg}")
        
    def stop_translation(self):
        """停止翻译（修复：保留已翻译内容，不删除未翻译原文）。
        
        核心逻辑：
        1. 立即停止翻译并取消API请求
        2. 保留已翻译的内容和对应的原文
        3. 仅清除未翻译部分的译文（将译文框设为空）
        4. 确保实时保存机制正确更新txt译文文件和EPUB映射文件
        """
        if self.is_translating:
            # 立即停止翻译并取消API请求
            self.translator.stop()
            self.is_translating = False
            
            # ✅ 修复3：不删除未翻译的行，只清空未翻译部分的译文
            # 不做任何删除操作，保留所有原文和已翻译的译文
            
            # 更新UI状态
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.update_status("翻译已停止，已翻译内容已保留")
            
            # 重置进度条为当前实际进度
            items = self.translation_table.get_children()
            if items:
                # 计算实际翻译进度
                total = len(items)
                translated = 0
                for item in items:
                    values = self.translation_table.item(item)['values']
                    if len(values) > 2 and values[2].strip():  # 译文不为空
                        translated += 1
                actual_progress = (translated / total) * 100 if total > 0 else 0
                self.progress_var.set(actual_progress)
            
            # ✅ 关键：立即触发保存（保存当前已翻译部分）
            self._schedule_save_to_target(delay_ms=0)  # 立即保存，不延迟

    def save_translation(self):
        """保存译文。
        
        修复说明：确保译文与原文按行号严格对齐。
        """
        try:
            # 从表格获取译文
            _, target_lines = self.get_table_data()
            translated_content = "\n".join(target_lines)
            
            if not translated_content.strip():
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
                
                # 若存在EPUB映射，则同步更新映射键值对
                if self.current_mapping_dir and self.current_mapping_keys:
                    try:
                        self.epub_processor.save_translations(
                            str(self.current_mapping_dir), 
                            self.current_mapping_keys, 
                            target_lines
                        )
                    except Exception as e:
                        print(f"同步EPUB映射失败: {e}")
                
        except Exception as e:
            messagebox.showerror("保存错误", f"保存译文失败: {str(e)}")
            
    def export_comparison(self):
        """导出对照文件"""
        try:
            source_lines, target_lines = self.get_table_data()
            source_content = "\n".join(source_lines)
            target_content = "\n".join(target_lines)
            
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

    def export_epub_file(self):
        """基于mapping将译文写回并导出为EPUB文件（新增：自动命名为"原文_译文"）"""
        try:
            if not self.current_mapping_dir:
                messagebox.showwarning("导出警告", "当前会话并非EPUB映射，无法导出EPUB")
                return
            
            # 先同步一次映射（使用当前表格内容）
            _, target_lines = self.get_table_data()
            
            try:
                self.epub_processor.save_translations(
                    str(self.current_mapping_dir), 
                    target_lines
                )
            except Exception:
                pass

            # ✅ 新增：自动生成默认文件名为"原文_译文"
            default_filename = ""
            if self.current_source_path:
                source_stem = self.current_source_path.stem
                default_filename = f"{source_stem}_译文.epub"
            
            out_path = filedialog.asksaveasfilename(
                title="导出为EPUB文件",
                defaultextension=".epub",
                initialfile=default_filename,
                filetypes=[("EPUB电子书", "*.epub"), ("所有文件", "*.*")]
            )
            if not out_path:
                return

            try:
                result_path = self.epub_processor.export_epub(str(self.current_mapping_dir), out_path)
                self.update_status(f"EPUB已导出: {Path(result_path).name}")
                messagebox.showinfo("导出成功", f"已导出EPUB文件: {Path(result_path).name}")
            except Exception as e:
                messagebox.showerror("导出错误", f"EPUB导出失败: {str(e)}")
        except Exception as e:
            messagebox.showerror("导出错误", f"导出EPUB操作失败: {str(e)}")
            
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
        """为译文内容变更安排一次防抖保存
        
        ✅ 修复：当禁用自动保存时，不执行保存操作
        """
        # ✅ 关键修复：检查是否禁用了自动保存
        if getattr(self, '_disable_auto_save', False):
            return
        
        # 若无当前译文文件路径，跳过
        if not getattr(self, 'current_target_path', None):
            return
        # 使用统一防抖器，避免频繁IO
        self._debounce('save_tgt', delay_ms, self._atomic_save_target)

    def _atomic_save_target(self):
        """将译文框内容原子性写入当前译文文件。
        
        完全重构：简化为纯行号对齐机制
        - 严格按行号对齐（从1开始）
        - 自动更新translated_at时间戳
        - 保证未翻译行的translated_text为空字符串
        """
        try:
            tgt_path = getattr(self, 'current_target_path', None)
            if not tgt_path:
                return
            
            # 从表格获取译文（严格按行号顺序）
            _, target_lines = self.get_table_data()
            content = "\n".join(target_lines)
            
            # 临时文件与原子替换
            tmp_path = tgt_path.with_suffix(tgt_path.suffix + '.tmp')
            # 先写临时文件
            self.file_handler.write_file(str(tmp_path), content)
            # 原子替换目标文件
            os.replace(str(tmp_path), str(tgt_path))
            
            # 同步EPUB映射（若存在）：按line_number严格对齐并更新时间戳
            if self.current_mapping_dir:
                try:
                    self.epub_processor.save_translations(
                        str(self.current_mapping_dir), 
                        target_lines
                    )
                except Exception as e:
                    # 记录错误但不中断保存
                    print(f"同步EPUB映射失败: {e}")
        except Exception as e:
            # 静默容错，不打断用户操作
            print(f"保存译文失败: {e}")

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

    # 移除原有的setup_softwrap_resize_realign方法，因为表格不需要同步滚动
    
    def _start_missing_translation_check(self):
        """启动翻译查漏机制：检查译文中的空行并自动翻译
        
        核心逻辑：
        1. 检查译文框中是否有空行（原文不为空但译文为空）
        2. 如果有空行，提示用户正在进行翻译查漏
        3. 只翻译空行内容，一次最多20个空行
        4. 循环执行直到没有空行为止
        """
        # 如果正在翻译，跳过
        if self.is_translating:
            return
        
        # 获取原文和译文
        source_lines, target_lines = self.get_table_data()
        
        # 查找空行（原文不为空但译文为空）
        empty_indices = []
        for i in range(len(source_lines)):
            if source_lines[i].strip() and (i >= len(target_lines) or not target_lines[i].strip()):
                empty_indices.append(i)
        
        # 如果没有空行，正常结束
        if not empty_indices:
            self.update_status("翻译完成，无需查漏")
            messagebox.showinfo("翻译完成", "所有内容已翻译完成！")
            return
        
        # 有空行，开始翻译查漏
        total_empty = len(empty_indices)
        self.update_status(f"正在进行翻译查漏：发现 {total_empty} 个空行")
        
        # 一次最多翻译20个空行
        batch_size = 20
        current_batch_indices = empty_indices[:batch_size]
        
        # 提取这些空行的原文
        empty_source_lines = [source_lines[i] for i in current_batch_indices]
        
        # 记录空行位置
        self._missing_translation_indices = current_batch_indices
        
        # 标记为翻译查漏模式
        self._is_missing_check = True
        
        # 开始翻译
        self.is_translating = True
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        
        # 在新线程中执行翻译
        combined_source = '\n'.join(empty_source_lines)
        translation_thread = threading.Thread(
            target=self._translate_missing_worker,
            args=(combined_source, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()
    
    def _translate_missing_worker(self, content, mode):
        """翻译查漏工作线程"""
        try:
            self.translator.translate_fast_mode(
                content,
                self._on_missing_translation_progress,
                self._on_missing_translation_complete
            )
        except Exception as e:
            self.root.after(0, lambda: self._on_translation_error(str(e)))
    
    def _on_missing_translation_progress(self, progress, batch_data):
        """翻译查漏进度回调"""
        def update_ui():
            # 更新进度条
            if progress >= 0:
                self.progress_var.set(progress)
                self.progress_bar.update_idletasks()
            
            if not batch_data or not isinstance(batch_data, dict):
                return
            
            # 获取空行位置
            missing_indices = getattr(self, '_missing_translation_indices', [])
            if not missing_indices:
                return
            
            batch_start = batch_data.get('batch_start', 0)
            is_streaming = batch_data.get('streaming', False)
            items = self.translation_table.get_children()
            
            if is_streaming:
                # 流式输出模式
                current_text = batch_data.get('current_text', '')
                expected_lines = batch_data.get('expected_lines', 1)
                streaming_lines = [line for line in current_text.split('\n') if line.strip()]
                
                for i, line in enumerate(streaming_lines[:expected_lines]):
                    relative_index = batch_start + i
                    if relative_index < len(missing_indices):
                        row_index = missing_indices[relative_index]
                        if row_index < len(items):
                            item = items[row_index]
                            values = list(self.translation_table.item(item)['values'])
                            values[2] = line.strip()
                            self.translation_table.item(item, values=values)
                            self.translation_table.see(item)
            else:
                # 批次完成模式
                translated_lines = batch_data.get('translated_lines', [])
                
                for i, translated_line in enumerate(translated_lines):
                    relative_index = batch_start + i
                    if relative_index < len(missing_indices):
                        row_index = missing_indices[relative_index]
                        if row_index < len(items):
                            item = items[row_index]
                            values = list(self.translation_table.item(item)['values'])
                            values[2] = translated_line.strip()
                            self.translation_table.item(item, values=values)
                
                # 保存
                self._schedule_save_to_target()
        
        self.root.after(0, update_ui)
    
    def _on_missing_translation_complete(self):
        """翻译查漏完成回调：继续检查是否还有空行"""
        def update_ui():
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            
            # 清理临时数据
            if hasattr(self, '_missing_translation_indices'):
                delattr(self, '_missing_translation_indices')
            if hasattr(self, '_is_missing_check'):
                delattr(self, '_is_missing_check')
            
            # 立即保存
            self._schedule_save_to_target(delay_ms=0)
            
            # 继续检查是否还有空行（循环执行）
            self.root.after(1000, self._start_missing_translation_check)
        
        self.root.after(0, update_ui)