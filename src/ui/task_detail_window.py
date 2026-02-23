#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务详情窗口
展示单个翻译任务的原文/译文对照表，支持双击编辑译文。
"""

import tkinter as tk
from tkinter import ttk


class TaskDetailWindow:
    """任务详情窗口：展示原文/译文对照，实时刷新进度"""

    STATUS_MAP = {
        "pending": "等待中",
        "running": "翻译中",
        "paused": "已暂停",
        "completed": "已完成",
        "cancelled": "已取消",
        "error": "出错",
    }

    def __init__(self, parent, manager, task_id):
        self.manager = manager
        self.task_id = task_id
        self.win = tk.Toplevel(parent)
        self.win.title("任务详情")
        self.win.geometry("900x600")
        self.win.minsize(700, 400)

        self.edit_entry = None
        self.editing_item = None
        self.editing_column = None

        self._build_ui()
        self._load_data()
        self._schedule_refresh()

    # ── UI构建 ──────────────────────────────────────
    def _build_ui(self):
        main = ttk.Frame(self.win, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # 顶部信息栏
        info_frame = ttk.Frame(main)
        info_frame.pack(fill=tk.X, pady=(0, 6))

        self.file_label = ttk.Label(info_frame, text="", font=("微软雅黑", 11, "bold"))
        self.file_label.pack(side=tk.LEFT)

        self.status_label = ttk.Label(info_frame, text="", font=("微软雅黑", 10))
        self.status_label.pack(side=tk.LEFT, padx=(20, 0))

        # 控制按钮
        btn_frame = ttk.Frame(info_frame)
        btn_frame.pack(side=tk.RIGHT)

        self.start_btn = ttk.Button(btn_frame, text="▶ 开始", command=self._on_start)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        self.pause_btn = ttk.Button(btn_frame, text="⏸ 暂停", command=self._on_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=2)
        self.cancel_btn = ttk.Button(btn_frame, text="✖ 取消", command=self._on_cancel)
        self.cancel_btn.pack(side=tk.LEFT, padx=2)

        # 进度条
        prog_frame = ttk.Frame(main)
        prog_frame.pack(fill=tk.X, pady=(0, 6))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.progress_label = ttk.Label(prog_frame, text="0%", width=6)
        self.progress_label.pack(side=tk.LEFT, padx=(6, 0))

        # 表格
        table_frame = ttk.Frame(main)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("line_number", "source_text", "target_text")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        self.table.heading("line_number", text="行号")
        self.table.heading("source_text", text="原文")
        self.table.heading("target_text", text="译文")
        self.table.column("line_number", width=60, minwidth=50, anchor="center", stretch=False)
        self.table.column("source_text", width=380, minwidth=150, anchor="w")
        self.table.column("target_text", width=380, minwidth=150, anchor="w")

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=vsb.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        self.table.bind("<Double-Button-1>", self._on_cell_double_click)
    # ── 数据加载与刷新 ─────────────────────────────
    def _load_data(self):
        task = self.manager.get_task(self.task_id)
        if not task:
            return
        self.file_label.config(text=task.file_name)
        self.table.delete(*self.table.get_children())
        for i, src in enumerate(task.source_lines):
            tgt = task.target_lines[i] if i < len(task.target_lines) else ""
            self.table.insert("", "end", values=(i + 1, src, tgt))
        self._refresh_status()

    def _schedule_refresh(self):
        if not self.win.winfo_exists():
            return
        self._refresh_status()
        self._refresh_translations()
        self.win.after(500, self._schedule_refresh)

    def _refresh_status(self):
        task = self.manager.get_task(self.task_id)
        if not task:
            return
        status_text = self.STATUS_MAP.get(task.status, task.status)
        self.status_label.config(text=f"状态: {status_text}")
        self.progress_var.set(task.progress)
        self.progress_label.config(text=f"{task.progress:.0f}%")

        is_running = task.status == "running"
        is_paused = task.status == "paused"
        is_pending = task.status == "pending"
        is_done = task.status in ("completed", "cancelled", "error")

        self.start_btn.config(state=tk.NORMAL if (is_pending or is_paused) else tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL if is_running else tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL if not is_done else tk.DISABLED)

    def _refresh_translations(self):
        """增量刷新译文列"""
        task = self.manager.get_task(self.task_id)
        if not task:
            return
        items = self.table.get_children()
        for i, item in enumerate(items):
            if i < len(task.target_lines):
                old_vals = self.table.item(item)["values"]
                new_tgt = task.target_lines[i]
                if len(old_vals) > 2 and str(old_vals[2]) != str(new_tgt):
                    self.table.item(item, values=(old_vals[0], old_vals[1], new_tgt))

    # ── 控制按钮 ─────────────────────────────────────
    def _on_start(self):
        task = self.manager.get_task(self.task_id)
        if not task:
            return
        if task.status == "paused":
            self.manager.resume_task(self.task_id)
        else:
            self.manager.start_task(self.task_id)

    def _on_pause(self):
        self.manager.pause_task(self.task_id)

    def _on_cancel(self):
        self.manager.cancel_task(self.task_id)

    # ── 双击编辑译文 ─────────────────────────────────
    def _on_cell_double_click(self, event):
        if self.edit_entry:
            self._save_edit()
        region = self.table.identify_region(event.x, event.y)
        if region != "cell":
            return
        column = self.table.identify_column(event.x)
        col_idx = int(column.replace("#", "")) - 1
        if col_idx != 2:  # 只允许编辑译文列
            return
        item = self.table.identify_row(event.y)
        if not item:
            return
        values = self.table.item(item)["values"]
        if not values:
            return
        bbox = self.table.bbox(item, column)
        if not bbox:
            return
        self.editing_item = item
        self.editing_column = col_idx
        self.edit_entry = tk.Entry(self.table, font=("微软雅黑", 10), relief=tk.SOLID, borderwidth=1)
        self.edit_entry.insert(0, values[col_idx])
        self.edit_entry.select_range(0, tk.END)
        self.edit_entry.focus_set()
        self.edit_entry.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        self.edit_entry.bind("<Return>", lambda e: self._save_edit())
        self.edit_entry.bind("<Escape>", lambda e: self._cancel_edit())
        self.edit_entry.bind("<FocusOut>", lambda e: self._save_edit())

    def _save_edit(self):
        if not self.edit_entry or not self.editing_item:
            return
        new_val = self.edit_entry.get()
        values = list(self.table.item(self.editing_item)["values"])
        values[self.editing_column] = new_val
        self.table.item(self.editing_item, values=values)
        # 同步回task数据
        row_idx = int(values[0]) - 1
        task = self.manager.get_task(self.task_id)
        if task and row_idx < len(task.target_lines):
            task.target_lines[row_idx] = new_val
        self.edit_entry.destroy()
        self.edit_entry = None
        self.editing_item = None

    def _cancel_edit(self):
        if self.edit_entry:
            self.edit_entry.destroy()
            self.edit_entry = None
            self.editing_item = None
