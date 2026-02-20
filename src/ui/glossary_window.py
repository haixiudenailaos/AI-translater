#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
术语库管理窗口模块
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json
from pathlib import Path

class GlossaryWindow:
    def __init__(self, parent, config_manager):
        self.parent = parent
        self.config_manager = config_manager
        
        # 创建术语库窗口
        self.window = tk.Toplevel(parent)
        self.window.title("术语库管理")
        self.window.geometry("700x500")
        self.window.transient(parent)
        self.window.grab_set()
        
        # 居中显示
        self.center_window()
        
        # 加载术语库数据
        self.glossary_data = config_manager.get_glossary()
        
        self.setup_ui()
        self.load_terms()
        
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
        search_entry.bind('<KeyRelease>', self.on_search)
        
        # 分类筛选
        ttk.Label(toolbar_frame, text="分类:").pack(side=tk.LEFT)
        self.category_var = tk.StringVar(value="全部")
        categories = ["全部"] + self.glossary_data.get("categories", [])
        category_combo = ttk.Combobox(toolbar_frame, textvariable=self.category_var,
                                     values=categories, state="readonly", width=10)
        category_combo.pack(side=tk.LEFT, padx=(5, 10))
        category_combo.bind('<<ComboboxSelected>>', self.on_category_change)
        
        # 导入导出按钮
        ttk.Button(toolbar_frame, text="导入", 
                  command=self.import_terms).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(toolbar_frame, text="导出", 
                  command=self.export_terms).pack(side=tk.RIGHT)
        
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
        self.term_tree.bind('<<TreeviewSelect>>', self.on_term_select)
        
    def create_edit_area(self, parent):
        """创建编辑区域"""
        edit_frame = ttk.LabelFrame(parent, text="术语编辑", padding=5)
        edit_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 原文术语
        ttk.Label(edit_frame, text="原文术语:").grid(row=0, column=0, sticky=tk.W, padx=(0, 10), pady=5)
        self.source_var = tk.StringVar()
        source_entry = ttk.Entry(edit_frame, textvariable=self.source_var, width=30)
        source_entry.grid(row=0, column=1, padx=(0, 20), pady=5)
        
        # 译文术语
        ttk.Label(edit_frame, text="译文术语:").grid(row=0, column=2, sticky=tk.W, padx=(0, 10), pady=5)
        self.target_var = tk.StringVar()
        target_entry = ttk.Entry(edit_frame, textvariable=self.target_var, width=30)
        target_entry.grid(row=0, column=3, pady=5)
        
        # 分类
        ttk.Label(edit_frame, text="分类:").grid(row=1, column=0, sticky=tk.W, padx=(0, 10), pady=5)
        self.edit_category_var = tk.StringVar(value="通用")
        category_combo = ttk.Combobox(edit_frame, textvariable=self.edit_category_var,
                                     values=self.glossary_data.get("categories", []), width=27)
        category_combo.grid(row=1, column=1, padx=(0, 20), pady=5)
        
        # 操作按钮
        button_frame = ttk.Frame(edit_frame)
        button_frame.grid(row=1, column=2, columnspan=2, pady=5)
        
        ttk.Button(button_frame, text="添加", 
                  command=self.add_term).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(button_frame, text="更新", 
                  command=self.update_term).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(button_frame, text="删除", 
                  command=self.delete_term).pack(side=tk.LEFT)
        
    def create_buttons(self, parent):
        """创建底部按钮"""
        button_frame = ttk.Frame(parent)
        button_frame.pack(fill=tk.X)
        
        ttk.Button(button_frame, text="保存", 
                  command=self.save_glossary).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(button_frame, text="关闭", 
                  command=self.window.destroy).pack(side=tk.RIGHT)
        
    def load_terms(self):
        """加载术语到列表"""
        # 清空现有项目
        for item in self.term_tree.get_children():
            self.term_tree.delete(item)
            
        # 添加术语
        terms = self.glossary_data.get("terms", [])
        for term in terms:
            self.term_tree.insert("", tk.END, values=(
                term.get("source", ""),
                term.get("target", ""),
                term.get("category", "通用")
            ))
            
    def on_search(self, event=None):
        """搜索术语"""
        search_text = self.search_var.get().lower()
        category = self.category_var.get()
        
        # 清空现有项目
        for item in self.term_tree.get_children():
            self.term_tree.delete(item)
            
        # 筛选并添加术语
        terms = self.glossary_data.get("terms", [])
        for term in terms:
            source = term.get("source", "").lower()
            target = term.get("target", "").lower()
            term_category = term.get("category", "通用")
            
            # 分类筛选
            if category != "全部" and term_category != category:
                continue
                
            # 搜索筛选
            if search_text and search_text not in source and search_text not in target:
                continue
                
            self.term_tree.insert("", tk.END, values=(
                term.get("source", ""),
                term.get("target", ""),
                term_category
            ))
            
    def on_category_change(self, event=None):
        """分类改变事件"""
        self.on_search()
        
    def on_term_select(self, event=None):
        """术语选择事件"""
        selection = self.term_tree.selection()
        if selection:
            item = self.term_tree.item(selection[0])
            values = item['values']
            
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
        new_term = {
            "source": source,
            "target": target,
            "category": category or "通用"
        }
        
        self.glossary_data["terms"].append(new_term)
        
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
        original_source = item['values'][0]
        
        # 更新术语
        for term in self.glossary_data["terms"]:
            if term["source"] == original_source:
                term["source"] = source
                term["target"] = target
                term["category"] = category or "通用"
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
            source_to_delete = item['values'][0]
            
            # 删除术语
            self.glossary_data["terms"] = [
                term for term in self.glossary_data["terms"]
                if term["source"] != source_to_delete
            ]
            
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
            title="导入术语库",
            filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")]
        )
        
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    imported_data = json.load(f)
                    
                if "terms" in imported_data:
                    # 合并术语
                    existing_sources = {term["source"] for term in self.glossary_data["terms"]}
                    new_terms = [
                        term for term in imported_data["terms"]
                        if term.get("source") not in existing_sources
                    ]
                    
                    self.glossary_data["terms"].extend(new_terms)
                    
                    # 合并分类
                    if "categories" in imported_data:
                        existing_categories = set(self.glossary_data["categories"])
                        new_categories = [
                            cat for cat in imported_data["categories"]
                            if cat not in existing_categories
                        ]
                        self.glossary_data["categories"].extend(new_categories)
                    
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
            filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")]
        )
        
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(self.glossary_data, f, ensure_ascii=False, indent=2)
                    
                messagebox.showinfo("导出成功", f"术语库已导出到: {Path(file_path).name}")
                
            except Exception as e:
                messagebox.showerror("导出错误", f"导出失败: {str(e)}")
                
    def save_glossary(self):
        """保存术语库"""
        if self.config_manager.save_glossary(self.glossary_data):
            messagebox.showinfo("保存成功", "术语库已保存")
        else:
            messagebox.showerror("保存失败", "术语库保存失败")