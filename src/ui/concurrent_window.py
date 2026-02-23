#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并发翻译管理窗口
展示任务列表，支持添加/开始/暂停/取消/查看详情。
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from ..core.concurrent_manager import ConcurrentTranslationManager
from .task_detail_window import TaskDetailWindow


class ConcurrentWindow:
    """并发翻译管理窗口"""

    STATUS_MAP = {
        "pending": "等待中",
        "running": "翻译中",
        "paused": "已暂停",
        "completed": "已完成",
        "cancelled": "已取消",
        "error": "出错",
    }

    def __init__(self, parent, config_manager):
        self.parent = parent
        self.config_manager = config_manager
        self.manager = ConcurrentTranslationManager(config_manager)
        self.manager.set_progress_callback(self._on_task_update)
        self._detail_windows: dict = {}

        self.win = tk.Toplevel(parent)
        self.win.title("并发翻译管理")
        self.win.geometry("780x480")
        self.win.minsize(640, 360)

        self._build_ui()
        self._schedule_refresh()

    # ── UI构建 ──────────────────────────────────────
    def _build_ui(self):
        main = ttk.Frame(self.win, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # 工具栏
        toolbar = ttk.Frame(main)
        toolbar.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(toolbar, text="➕ 添加文件", command=self._add_files).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="▶ 全部开始", command=self._start_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="⏸ 全部暂停", command=self._pause_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="✖ 全部取消", command=self._cancel_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="🗑 移除已完成", command=self._remove_finished).pack(side=tk.LEFT, padx=(0, 4))

        # 任务列表
        cols = ("task_id", "file_name", "status", "progress")
        self.tree = ttk.Treeview(main, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("task_id", text="ID")
        self.tree.heading("file_name", text="文件名")
        self.tree.heading("status", text="状态")
        self.tree.heading("progress", text="进度")
        self.tree.column("task_id", width=70, minwidth=60, anchor="center", stretch=False)
        self.tree.column("file_name", width=340, minwidth=150, anchor="w")
        self.tree.column("status", width=80, minwidth=60, anchor="center", stretch=False)
        self.tree.column("progress", width=100, minwidth=80, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        # 双击打开详情
        self.tree.bind("<Double-Button-1>", self._on_double_click)
        # 右键菜单
        self.ctx_menu = tk.Menu(self.win, tearoff=0)
        self.ctx_menu.add_command(label="▶ 开始/恢复", command=self._ctx_start)
        self.ctx_menu.add_command(label="⏸ 暂停", command=self._ctx_pause)
        self.ctx_menu.add_command(label="✖ 取消", command=self._ctx_cancel)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="🔍 查看详情", command=self._ctx_detail)
        self.ctx_menu.add_command(label="🗑 移除", command=self._ctx_remove)
        self.tree.bind("<Button-3>", self._on_right_click)
    # ── 刷新 ────────────────────────────────────────
    def _schedule_refresh(self):
        if not self.win.winfo_exists():
            return
        self._refresh_tree()
        self.win.after(500, self._schedule_refresh)

    def _on_task_update(self, task_id: str):
        """来自manager的回调（可能在子线程），调度到主线程"""
        try:
            self.win.after(0, self._refresh_tree)
        except Exception:
            pass

    def _refresh_tree(self):
        tasks = self.manager.get_all_tasks()
        existing = {self.tree.item(iid)["values"][0]: iid for iid in self.tree.get_children()}

        seen_ids = set()
        for task in tasks:
            seen_ids.add(task.task_id)
            status_text = self.STATUS_MAP.get(task.status, task.status)
            progress_text = f"{task.progress:.0f}%"
            vals = (task.task_id, task.file_name, status_text, progress_text)

            if task.task_id in existing:
                self.tree.item(existing[task.task_id], values=vals)
            else:
                self.tree.insert("", "end", values=vals)

        # 移除已删除的任务
        for tid, iid in existing.items():
            if tid not in seen_ids:
                self.tree.delete(iid)

    # ── 工具栏操作 ──────────────────────────────────
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择要翻译的文件",
            filetypes=[("支持的文件", "*.txt *.epub"), ("文本文件", "*.txt"), ("EPUB电子书", "*.epub"), ("所有文件", "*.*")],
            parent=self.win,
        )
        if not paths:
            return
        for p in paths:
            try:
                self.manager.add_task(p)
            except Exception as e:
                messagebox.showerror("添加失败", f"无法添加文件:\n{p}\n\n{e}", parent=self.win)
        self._refresh_tree()

    def _start_all(self):
        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置", parent=self.win)
            return
        self.manager.start_all()

    def _pause_all(self):
        self.manager.pause_all()

    def _cancel_all(self):
        self.manager.cancel_all()

    def _remove_finished(self):
        for task in list(self.manager.get_all_tasks()):
            if task.status in ("completed", "cancelled", "error"):
                self.manager.remove_task(task.task_id)
        self._refresh_tree()

    # ── 双击详情 ────────────────────────────────────
    def _on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        vals = self.tree.item(item)["values"]
        if vals:
            self._open_detail(str(vals[0]))

    def _open_detail(self, task_id: str):
        if task_id in self._detail_windows:
            w = self._detail_windows[task_id]
            if w.win.winfo_exists():
                w.win.lift()
                return
        dw = TaskDetailWindow(self.win, self.manager, task_id)
        self._detail_windows[task_id] = dw

    # ── 右键菜单 ────────────────────────────────────
    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            try:
                self.ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.ctx_menu.grab_release()

    def _selected_task_id(self):
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0])["values"]
        return str(vals[0]) if vals else None

    def _ctx_start(self):
        tid = self._selected_task_id()
        if not tid:
            return
        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置", parent=self.win)
            return
        task = self.manager.get_task(tid)
        if task and task.status == "paused":
            self.manager.resume_task(tid)
        else:
            self.manager.start_task(tid)

    def _ctx_pause(self):
        tid = self._selected_task_id()
        if tid:
            self.manager.pause_task(tid)

    def _ctx_cancel(self):
        tid = self._selected_task_id()
        if tid:
            self.manager.cancel_task(tid)

    def _ctx_detail(self):
        tid = self._selected_task_id()
        if tid:
            self._open_detail(tid)

    def _ctx_remove(self):
        tid = self._selected_task_id()
        if not tid:
            return
        task = self.manager.get_task(tid)
        if task and task.status == "running":
            messagebox.showwarning("提示", "请先暂停或取消正在运行的任务", parent=self.win)
            return
        self.manager.remove_task(tid)
        self._refresh_tree()
