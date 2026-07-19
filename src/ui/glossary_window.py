#!/usr/bin/env python3
"""
术语库管理窗口模块
"""

import copy
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..utils.file_handler import write_json_atomic


class GlossaryWindow:
    def __init__(self, parent, config_manager):
        self.parent = parent
        self.config_manager = config_manager

        # P1-7：搜索 debounce 的 after_id，避免每次按键都重建整个列表
        self._search_after_id = None
        # P2-4：术语索引 + 分块渲染状态。
        # _term_index 缓存 (source, target, category, source_lower, target_lower)，
        # 避免每次搜索都对 5000 术语重复 lower() 字符串。
        # _render_generation 用于取消过期的分块渲染任务（用户快速输入时）。
        self._term_index: list[tuple[str, str, str, str, str]] = []
        self._render_generation = 0
        self._render_after_id = None

        # 创建术语库窗口
        self.window = tk.Toplevel(parent)
        self.window.title("术语库管理")
        self.window.geometry("700x500")
        self.window.transient(parent)
        self.window.grab_set()

        # 居中显示
        self.center_window()

        # P1-7：使用 deepcopy 创建工作副本，避免用户编辑直接修改
        # ConfigManager 的内部状态（get_glossary 仅浅拷贝，嵌套 terms/categories
        # 仍是原始列表的引用）。所有修改操作在工作副本上进行，保存时原子提交。
        self.glossary_data = copy.deepcopy(config_manager.get_glossary())
        # P1-7：dirty 标记，关闭时据此弹 Save/Discard/Cancel
        self._dirty = False

        self.setup_ui()
        self.load_terms()

        # P1-7：拦截窗口关闭动作（Escape / WM_DELETE_WINDOW / 关闭按钮），
        # 有未保存修改时弹 Save/Discard/Cancel 三选一。
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window.bind("<Escape>", lambda _event: self._on_close())
        self.window.bind("<Destroy>", self._on_destroy_restore_focus, add="+")

    def _on_destroy_restore_focus(self, event: tk.Event) -> None:
        """P2-1：窗口销毁后把焦点还给父窗口。"""
        if event.widget is not self.window:
            return
        parent = self.parent
        try:
            if parent is not None and parent.winfo_exists():
                parent.focus_set()
        except tk.TclError:
            pass

    def center_window(self):
        """窗口居中显示"""
        self.window.update_idletasks()
        x = (self.window.winfo_screenwidth() // 2) - (700 // 2)
        y = (self.window.winfo_screenheight() // 2) - (500 // 2)
        self.window.geometry(f"700x500+{x}+{y}")

    def setup_ui(self):
        """设置界面"""
        # 主框架
        main_frame = ttk.Frame(self.window)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 工具栏
        self.create_toolbar(main_frame)

        # 术语列表
        self.create_term_list(main_frame)

        # 编辑区域
        self.create_edit_area(main_frame)

        # 按钮区域
        self.create_buttons(main_frame)

    def create_toolbar(self, parent):
        """创建工具栏"""
        toolbar_frame = ttk.Frame(parent)
        toolbar_frame.pack(fill=tk.X, pady=(0, 10))

        # 搜索框
        ttk.Label(toolbar_frame, text="搜索:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(toolbar_frame, textvariable=self.search_var, width=20)
        search_entry.pack(side=tk.LEFT, padx=(5, 10))
        search_entry.bind("<KeyRelease>", self.on_search)

        # 分类筛选
        ttk.Label(toolbar_frame, text="分类:").pack(side=tk.LEFT)
        self.category_var = tk.StringVar(value="全部")
        categories = ["全部"] + self.glossary_data.get("categories", [])
        category_combo = ttk.Combobox(
            toolbar_frame,
            textvariable=self.category_var,
            values=categories,
            state="readonly",
            width=10,
        )
        category_combo.pack(side=tk.LEFT, padx=(5, 10))
        category_combo.bind("<<ComboboxSelected>>", self.on_category_change)

        # 导入导出按钮
        ttk.Button(toolbar_frame, text="导入", command=self.import_terms).pack(
            side=tk.RIGHT, padx=(5, 0)
        )
        ttk.Button(toolbar_frame, text="导出", command=self.export_terms).pack(side=tk.RIGHT)

    def create_term_list(self, parent):
        """创建术语列表"""
        list_frame = ttk.LabelFrame(parent, text="术语列表", padding=5)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # 创建Treeview
        columns = ("source", "target", "category")
        self.term_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10)

        # 设置列标题
        self.term_tree.heading("source", text="原文术语")
        self.term_tree.heading("target", text="译文术语")
        self.term_tree.heading("category", text="分类")

        # 设置列宽
        self.term_tree.column("source", width=200)
        self.term_tree.column("target", width=200)
        self.term_tree.column("category", width=100)

        # 滚动条
        tree_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.term_tree.yview)
        self.term_tree.configure(yscrollcommand=tree_scroll.set)

        self.term_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 绑定选择事件
        self.term_tree.bind("<<TreeviewSelect>>", self.on_term_select)

    def create_edit_area(self, parent):
        """创建编辑区域"""
        edit_frame = ttk.LabelFrame(parent, text="术语编辑", padding=5)
        edit_frame.pack(fill=tk.X, pady=(0, 10))

        # 原文术语
        ttk.Label(edit_frame, text="原文术语:").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 10), pady=5
        )
        self.source_var = tk.StringVar()
        source_entry = ttk.Entry(edit_frame, textvariable=self.source_var, width=30)
        source_entry.grid(row=0, column=1, padx=(0, 20), pady=5)

        # 译文术语
        ttk.Label(edit_frame, text="译文术语:").grid(
            row=0, column=2, sticky=tk.W, padx=(0, 10), pady=5
        )
        self.target_var = tk.StringVar()
        target_entry = ttk.Entry(edit_frame, textvariable=self.target_var, width=30)
        target_entry.grid(row=0, column=3, pady=5)

        # 分类
        ttk.Label(edit_frame, text="分类:").grid(row=1, column=0, sticky=tk.W, padx=(0, 10), pady=5)
        self.edit_category_var = tk.StringVar(value="通用")
        category_combo = ttk.Combobox(
            edit_frame,
            textvariable=self.edit_category_var,
            values=self.glossary_data.get("categories", []),
            width=27,
        )
        category_combo.grid(row=1, column=1, padx=(0, 20), pady=5)

        # 操作按钮
        button_frame = ttk.Frame(edit_frame)
        button_frame.grid(row=1, column=2, columnspan=2, pady=5)

        ttk.Button(button_frame, text="添加", command=self.add_term).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(button_frame, text="更新", command=self.update_term).pack(
            side=tk.LEFT, padx=(0, 5)
        )
        ttk.Button(button_frame, text="删除", command=self.delete_term).pack(side=tk.LEFT)

    def create_buttons(self, parent):
        """创建底部按钮"""
        button_frame = ttk.Frame(parent)
        button_frame.pack(fill=tk.X)

        ttk.Button(button_frame, text="保存", command=self.save_glossary).pack(
            side=tk.RIGHT, padx=(5, 0)
        )
        # P1-7：关闭按钮走 _on_close，检查 dirty 后再决定是否关闭
        ttk.Button(button_frame, text="关闭", command=self._on_close).pack(side=tk.RIGHT)

    def load_terms(self):
        """加载术语到列表。

        P2-4：构建 _term_index 一次性预计算 lowered 字符串，搜索时不再
        对每个术语重复 lower()。同时触发一次全量渲染（分块）。
        """
        terms = self.glossary_data.get("terms", [])
        self._term_index = [
            (
                term.get("source", ""),
                term.get("target", ""),
                term.get("category", "通用"),
                term.get("source", "").lower(),
                term.get("target", "").lower(),
            )
            for term in terms
        ]
        # 全量渲染（无筛选）
        self._render_terms(self._term_index)

    def _render_terms(self, rows):
        """P2-4：分块渲染术语到 Treeview。

        ``rows`` 是 [(source, target, category, source_lower, target_lower), ...]。
        先清空 Treeview，再按 250 行/批插入，用 after(1) 让出主线程，
        避免 5000 术语一次性阻塞 UI。
        """
        # 取消上一个未完成的渲染任务
        if self._render_after_id is not None:
            try:
                self.window.after_cancel(self._render_after_id)
            except Exception:
                pass
            self._render_after_id = None
        self._render_generation += 1
        generation = self._render_generation

        # 清空现有项目
        for item in self.term_tree.get_children():
            self.term_tree.delete(item)

        self._render_chunk(rows, 0, generation)

    def _render_chunk(self, rows, start, generation):
        """P2-4：分块插入术语行。generation 不匹配时立即中止。"""
        if generation != self._render_generation or not self.window.winfo_exists():
            self._render_after_id = None
            return
        chunk_size = 250
        end = min(start + chunk_size, len(rows))
        for i in range(start, end):
            source, target, category, _src_lower, _tgt_lower = rows[i]
            self.term_tree.insert(
                "",
                tk.END,
                values=(source, target, category),
            )
        if end < len(rows):
            self._render_after_id = self.window.after(
                1, lambda: self._render_chunk(rows, end, generation)
            )
        else:
            self._render_after_id = None

    def on_search(self, event=None):
        """搜索术语。

        P1-7：使用 debounce（300ms）避免每次按键都重建整个列表。
        对于 5000 术语的场景，逐按键重建会导致明显卡顿。
        """
        # 取消前一个 debounce 定时器
        if self._search_after_id is not None:
            try:
                self.window.after_cancel(self._search_after_id)
            except Exception:
                pass
            self._search_after_id = None

        # 300ms 后执行实际搜索
        self._search_after_id = self.window.after(300, self._do_search)

    def _do_search(self):
        """实际执行搜索（由 debounce 定时器调用）。

        P2-4：使用 _term_index 预计算的 lowered 字符串，搜索时只做
        substring 检查，不再对 5000 术语重复 lower()。匹配结果交给
        _render_terms 分块渲染。
        """
        self._search_after_id = None
        search_text = self.search_var.get().lower()
        category = self.category_var.get()

        # 使用索引筛选
        matched = []
        for source, target, term_category, source_lower, target_lower in self._term_index:
            # 分类筛选
            if category != "全部" and term_category != category:
                continue
            # 搜索筛选
            if search_text and search_text not in source_lower and search_text not in target_lower:
                continue
            matched.append((source, target, term_category, source_lower, target_lower))

        self._render_terms(matched)

    def on_category_change(self, event=None):
        """分类改变事件"""
        self.on_search()

    def on_term_select(self, event=None):
        """术语选择事件"""
        selection = self.term_tree.selection()
        if selection:
            item = self.term_tree.item(selection[0])
            values = item["values"]

            self.source_var.set(values[0])
            self.target_var.set(values[1])
            self.edit_category_var.set(values[2])

    def add_term(self):
        """添加术语"""
        source = self.source_var.get().strip()
        target = self.target_var.get().strip()
        category = self.edit_category_var.get().strip()

        if not source or not target:
            messagebox.showwarning("输入错误", "请输入原文术语和译文术语")
            return

        # 检查是否已存在
        for term in self.glossary_data["terms"]:
            if term["source"] == source:
                messagebox.showwarning("术语已存在", "该原文术语已存在，请使用更新功能")
                return

        # 添加新术语
        new_term = {"source": source, "target": target, "category": category or "通用"}

        self.glossary_data["terms"].append(new_term)
        # P1-7：标记工作副本有未保存修改
        self._dirty = True

        # 刷新列表
        self.load_terms()

        # 清空输入框
        self.source_var.set("")
        self.target_var.set("")
        self.edit_category_var.set("通用")

        messagebox.showinfo("添加成功", "术语已添加")

    def update_term(self):
        """更新术语"""
        selection = self.term_tree.selection()
        if not selection:
            messagebox.showwarning("选择错误", "请先选择要更新的术语")
            return

        source = self.source_var.get().strip()
        target = self.target_var.get().strip()
        category = self.edit_category_var.get().strip()

        if not source or not target:
            messagebox.showwarning("输入错误", "请输入原文术语和译文术语")
            return

        # 获取原始术语
        item = self.term_tree.item(selection[0])
        original_source = item["values"][0]

        # 更新术语
        for term in self.glossary_data["terms"]:
            if term["source"] == original_source:
                term["source"] = source
                term["target"] = target
                term["category"] = category or "通用"
                # P1-7：标记工作副本有未保存修改
                self._dirty = True
                break

        # 刷新列表
        self.load_terms()
        messagebox.showinfo("更新成功", "术语已更新")

    def delete_term(self):
        """删除术语"""
        selection = self.term_tree.selection()
        if not selection:
            messagebox.showwarning("选择错误", "请先选择要删除的术语")
            return

        if messagebox.askyesno("确认删除", "确定要删除选中的术语吗？"):
            item = self.term_tree.item(selection[0])
            source_to_delete = item["values"][0]

            # 删除术语
            self.glossary_data["terms"] = [
                term for term in self.glossary_data["terms"] if term["source"] != source_to_delete
            ]
            # P1-7：标记工作副本有未保存修改
            self._dirty = True

            # 刷新列表
            self.load_terms()

            # 清空输入框
            self.source_var.set("")
            self.target_var.set("")
            self.edit_category_var.set("通用")

            messagebox.showinfo("删除成功", "术语已删除")

    def import_terms(self):
        """导入术语"""
        file_path = filedialog.askopenfilename(
            title="导入术语库", filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")]
        )

        if file_path:
            try:
                with open(file_path, encoding="utf-8") as f:
                    imported_data = json.load(f)

                if "terms" in imported_data:
                    # 合并术语
                    existing_sources = {term["source"] for term in self.glossary_data["terms"]}
                    new_terms = [
                        term
                        for term in imported_data["terms"]
                        if term.get("source") not in existing_sources
                    ]

                    self.glossary_data["terms"].extend(new_terms)

                    # 合并分类
                    if "categories" in imported_data:
                        existing_categories = set(self.glossary_data["categories"])
                        new_categories = [
                            cat
                            for cat in imported_data["categories"]
                            if cat not in existing_categories
                        ]
                        self.glossary_data["categories"].extend(new_categories)

                    # P1-7：标记工作副本有未保存修改
                    self._dirty = True

                    self.load_terms()
                    messagebox.showinfo("导入成功", f"成功导入 {len(new_terms)} 个术语")
                else:
                    messagebox.showerror("导入错误", "文件格式不正确")

            except Exception as e:
                messagebox.showerror("导入错误", f"导入失败: {str(e)}")

    def export_terms(self):
        """导出术语"""
        file_path = filedialog.asksaveasfilename(
            title="导出术语库",
            defaultextension=".json",
            filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")],
        )

        if file_path:
            try:
                # BUG-006：使用原子写入，失败时旧文件保持不变
                write_json_atomic(file_path, self.glossary_data)

                messagebox.showinfo("导出成功", f"术语库已导出到: {Path(file_path).name}")

            except Exception as e:
                messagebox.showerror("导出错误", f"导出失败: {str(e)}")

    def save_glossary(self):
        """保存术语库。

        P1-7：ConfigManager.save_glossary 内部走 write_json_atomic 原子写入，
        成功后将 self.glossary 替换为传入的工作副本——工作副本成为新的真相源，
        dirty 标记重置为 False。
        """
        if self.config_manager.save_glossary(self.glossary_data):
            # P1-7：原子提交成功，工作副本与磁盘一致
            self._dirty = False
            messagebox.showinfo("保存成功", "术语库已保存")
        else:
            messagebox.showerror("保存失败", "术语库保存失败")

    def _on_close(self) -> None:
        """P1-7：关闭窗口前的 dirty 检查。

        - 无未保存修改：直接销毁窗口。
        - 有未保存修改：弹三选一对话框
            * 保存：原子提交，成功后销毁；失败则保留窗口。
            * 不保存：丢弃修改，直接销毁。
            * 取消：保留窗口（焦点回编辑区）。
        """
        if not self._dirty:
            self.window.destroy()
            return

        choice = messagebox.askyesnocancel(
            "未保存的修改",
            "术语库有未保存的修改，是否保存？",
        )
        if choice is None:
            # 用户取消，保留窗口
            return
        if choice:
            # 保存
            if self.config_manager.save_glossary(self.glossary_data):
                self._dirty = False
                self.window.destroy()
            else:
                messagebox.showerror("保存失败", "术语库保存失败，未关闭窗口")
        else:
            # 不保存，丢弃修改
            self.window.destroy()
