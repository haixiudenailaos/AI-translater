#!/usr/bin/env python3
"""
任务详情窗口
展示单个翻译任务的原文/译文对照表，支持双击编辑译文。
"""

import tkinter as tk
from tkinter import messagebox, ttk

from .table_editor import TableCellEditor


class TaskDetailWindow:
    """任务详情窗口：展示原文/译文对照，实时刷新进度"""

    # 队列翻译并发优化阶段 4：覆盖新状态机的全部状态
    STATUS_MAP = {
        "pending": "等待中",
        "ready": "准备中",
        "running": "翻译中",
        "pause_requested": "暂停中",
        "paused": "已暂停",
        "completed": "已完成",
        "partial": "部分完成",
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

        # P1-7：增量刷新缓存，避免每 500ms 扫描全部行
        # _row_metadata 存 (行号, 原文) 避免每次刷新都读 table.item()
        # _last_target_snapshot 存上次写入表格的译文，用于 diff 只更新变化行
        self._row_metadata: list[tuple[int, str]] = []
        self._last_target_snapshot: list[str] = []

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
        self.table = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
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

        # 使用 TableCellEditor 组件（只允许编辑译文列）
        self._cell_editor = TableCellEditor(
            self.table,
            editable_columns={2},
            on_save=self._on_cell_edited,
        )
        self.table.bind("<Double-Button-1>", self._cell_editor.on_double_click)

    # ── 数据加载与刷新 ─────────────────────────────
    def _load_data(self):
        task = self.manager.get_task(self.task_id)
        if not task:
            return
        self.file_label.config(text=task.file_name)
        self.table.delete(*self.table.get_children())
        # P1-7：初始化增量刷新缓存，保持快照与表格内容一致
        self._row_metadata = []
        self._last_target_snapshot = []
        for i, src in enumerate(task.source_lines):
            tgt = task.target_lines[i] if i < len(task.target_lines) else ""
            self.table.insert("", "end", values=(i + 1, src, tgt))
            self._row_metadata.append((i + 1, src))
            self._last_target_snapshot.append(tgt)
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
        is_partial = task.status == "partial"
        # R2-BUG-011：partial 状态允许重新开始以继续翻译失败行
        self.start_btn.config(
            state=tk.NORMAL if (is_pending or is_paused or is_partial) else tk.DISABLED
        )
        self.pause_btn.config(state=tk.NORMAL if is_running else tk.DISABLED)
        can_cancel = task.status not in ("completed", "cancelled")
        self.cancel_btn.config(state=tk.NORMAL if can_cancel else tk.DISABLED)

    def _refresh_translations(self):
        task = self.manager.get_task(self.task_id)
        if not task:
            return
        targets = task.target_lines
        snapshot = self._last_target_snapshot
        items = self.table.get_children()
        n_items = len(items)
        n_snap = len(snapshot)
        n_tgt = len(targets)
        # P1-7：增量 diff——只更新发生变化的行，避免对 10,000 行文档每 500ms 全量扫描。
        # 不再调用 table.item(item)["values"] 读取，直接用缓存的行号/原文构造新值。
        upper = min(n_items, n_snap, n_tgt)
        for i in range(upper):
            tgt = targets[i]
            if snapshot[i] != tgt:
                line_no, src = self._row_metadata[i]
                self.table.item(items[i], values=(line_no, src, tgt))
                snapshot[i] = tgt
        # 处理 target_lines 增长（理论上 _load_data 已对齐，此处兜底）
        if n_tgt > n_snap and n_items >= n_tgt:
            for i in range(n_snap, n_tgt):
                line_no, src = self._row_metadata[i]
                self.table.item(items[i], values=(line_no, src, targets[i]))
                snapshot.append(targets[i])

    # ── 操作 ────────────────────────────────────────
    def _on_start(self):
        task = self.manager.get_task(self.task_id)
        if task and task.status == "paused":
            self.manager.resume_task(self.task_id)
        else:
            self.manager.start_task(self.task_id)

    def _on_pause(self):
        self.manager.pause_task(self.task_id)

    def _on_cancel(self):
        self.manager.cancel_task(self.task_id)

    def _on_cell_edited(self, item_id, col_idx, old_value, new_value):
        """P0-2：TableCellEditor 回调——更新 Treeview、Coordinator 内部数据并持久化。

        不再修改 get_task 返回的快照副本（那是不可变视图），
        而是通过 manager.update_task_line 直接更新 Coordinator 内部 _TaskSlot，
        确保 save_task_now 持久化的是用户编辑后的新值。
        """
        values = self.table.item(item_id)["values"]
        row_idx = int(values[0]) - 1
        task = self.manager.get_task(self.task_id)
        if not task or row_idx >= len(task.target_lines):
            return
        # R2-BUG-015：运行中任务不允许编辑，避免进度回调覆盖用户编辑
        if task.status == "running":
            messagebox.showwarning(
                "编辑受限",
                "任务正在翻译中，请先暂停或取消后再编辑译文。",
                parent=self.win,
            )
            return
        # P0-2：更新 Treeview（TableCellEditor 不再直接修改树）
        vals = list(self.table.item(item_id)["values"])
        vals[col_idx] = new_value
        self.table.item(item_id, values=vals)
        # P0-2：通过 manager 更新 Coordinator 内部数据，而非修改快照副本
        if not self.manager.update_task_line(self.task_id, row_idx, new_value):
            # 更新失败，恢复旧值
            vals[col_idx] = old_value
            self.table.item(item_id, values=vals)
            messagebox.showwarning(
                "更新失败",
                "译文更新失败，任务可能已被清理。已恢复原值。",
                parent=self.win,
            )
            return
        # P0-2：持久化到文件，失败时恢复旧值
        if not self.manager.save_task(self.task_id):
            self.manager.update_task_line(self.task_id, row_idx, old_value)
            vals[col_idx] = old_value
            self.table.item(item_id, values=vals)
            messagebox.showwarning(
                "保存失败",
                "译文保存失败，已恢复原值。请检查文件权限或磁盘空间。",
                parent=self.win,
            )
            return
        # P1-7：同步增量刷新快照，避免下次 500ms 刷新冗余重写用户刚编辑的行
        if 0 <= row_idx < len(self._last_target_snapshot):
            self._last_target_snapshot[row_idx] = new_value
