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

        # P1-2：增量刷新缓存，避免每 500ms 扫描全部行
        # _row_metadata 存 (行号, 原文) 避免每次刷新都读 table.item()
        # _last_target_snapshot 存上次写入表格的译文，用于 diff 只更新变化行
        self._row_metadata: list[tuple[int, str]] = []
        self._last_target_snapshot: list[str] = []
        # P2-4：分块加载状态。_load_generation 用于取消过期的分块任务
        # （用户切换任务或关闭窗口时，旧任务不应继续插入行）。
        self._load_generation = 0
        self._load_in_progress = False
        # P1-UX-3：error_frame 可见性标志，避免每次 refresh 都重新 pack
        self._error_frame_visible = False

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

        self.start_btn = ttk.Button(btn_frame, text="开始", command=self._on_start)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        self.pause_btn = ttk.Button(btn_frame, text="暂停", command=self._on_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=2)
        self.cancel_btn = ttk.Button(btn_frame, text="取消", command=self._on_cancel)
        self.cancel_btn.pack(side=tk.LEFT, padx=2)

        # 进度条
        prog_frame = ttk.Frame(main)
        prog_frame.pack(fill=tk.X, pady=(0, 6))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.progress_label = ttk.Label(prog_frame, text="0%", width=6)
        self.progress_label.pack(side=tk.LEFT, padx=(6, 0))

        # P1-UX-3：错误信息栏——显示分类、安全文案、建议动作与重试按钮
        # 仅在 PARTIAL / ERROR 状态显示；其他状态隐藏避免占用空间。
        self.error_frame = ttk.Frame(main)
        self.error_label = ttk.Label(
            self.error_frame,
            text="",
            font=("微软雅黑", 10),
            wraplength=820,
            justify=tk.LEFT,
        )
        self.error_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.retry_btn = ttk.Button(
            self.error_frame, text="重试失败行", command=self._on_retry_failed
        )
        self.retry_btn.pack(side=tk.RIGHT, padx=(6, 0))
        # 初始隐藏；_refresh_status 根据任务状态切换可见性

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
        # UX-6：F2 / Enter 启动内联编辑译文列，纯键盘可校对
        self.table.bind("<F2>", lambda _e: self._cell_editor.start_edit_selected())
        self.table.bind("<Return>", lambda _e: self._cell_editor.start_edit_selected())

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
        # P2-4：分块加载——10,000 行文档同步插入会阻塞 UI 主线程约 1.5s，
        # 期间窗口无法响应绘制/滚动事件。改为 250 行/批，用 after(1) 让出
        # 主线程，让窗口先绘制前若干行（用户立刻看到内容）再继续追加。
        # _load_generation 用于切换任务/重开窗口时取消过期批次。
        self._load_generation += 1
        self._load_in_progress = True
        # 先刷新一次状态（按钮/进度），让用户在加载期间就能看到任务状态
        self._refresh_status()
        self._load_chunk(list(task.source_lines), list(task.target_lines), 0, self._load_generation)

    def _load_chunk(self, source_lines, target_lines, start, generation):
        """P2-4：分块插入行。generation 不匹配时立即中止。"""
        if generation != self._load_generation or not self.win.winfo_exists():
            self._load_in_progress = False
            return
        chunk_size = 250
        end = min(start + chunk_size, len(source_lines))
        for i in range(start, end):
            src = source_lines[i]
            tgt = target_lines[i] if i < len(target_lines) else ""
            self.table.insert("", "end", values=(i + 1, src, tgt))
            self._row_metadata.append((i + 1, src))
            self._last_target_snapshot.append(tgt)
        if end < len(source_lines):
            self.win.after(1, lambda: self._load_chunk(source_lines, target_lines, end, generation))
        else:
            self._load_in_progress = False

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
        is_error = task.status == "error"
        # R2-BUG-011：partial 状态允许重新开始以继续翻译失败行
        self.start_btn.config(
            state=tk.NORMAL if (is_pending or is_paused or is_partial) else tk.DISABLED
        )
        self.pause_btn.config(state=tk.NORMAL if is_running else tk.DISABLED)
        can_cancel = task.status not in ("completed", "cancelled")
        self.cancel_btn.config(state=tk.NORMAL if can_cancel else tk.DISABLED)

        # P1-UX-3：错误信息栏仅在 PARTIAL / ERROR 显示
        if is_partial or is_error:
            failed_count = int(getattr(task, "failed_count", 0) or 0)
            category = getattr(task, "error_category", None) or "unknown"
            safe_msg = (
                getattr(task, "error_safe_message", None)
                or getattr(task, "error_message", None)
                or ""
            )
            action = getattr(task, "recommended_action", None) or ""
            correlation = getattr(task, "correlation_id", None) or ""
            retryable = bool(getattr(task, "error_retryable", False))
            # 拼装可读错误文案：[分类] 安全文案 | 建议动作 | 失败行数 | correlation_id
            parts = [f"[{category}]", safe_msg]
            if action:
                parts.append(f"建议：{action}")
            if failed_count > 0:
                parts.append(f"失败 {failed_count} 行")
            if correlation:
                parts.append(f"cid={correlation}")
            self.error_label.config(text=" | ".join(p for p in parts if p))
            # 重试按钮：仅可重试或存在失败行时启用
            self.retry_btn.config(
                state=tk.NORMAL if (retryable or failed_count > 0) else tk.DISABLED
            )
            # 显示 error_frame（若未 pack）
            if not self._error_frame_visible:
                # pack 在进度条之后、表格之前
                self.error_frame.pack(fill=tk.X, pady=(0, 6), before=self.table.master)
                self._error_frame_visible = True
        else:
            # 非错误状态：隐藏 error_frame
            if self._error_frame_visible:
                try:
                    self.error_frame.pack_forget()
                except tk.TclError:
                    pass
                self._error_frame_visible = False

    def _on_retry_failed(self):
        """P1-UX-3：详情窗口「重试失败行」按钮。

        校验 API 配置后调用 ``manager.start_task``——Coordinator 会跳过
        ``completed_indices`` / ``manually_edited_indices``，只翻译失败行。
        """
        task = self.manager.get_task(self.task_id)
        if task is None:
            return
        if not getattr(self.manager.config_manager, "is_api_configured", lambda: False)():
            messagebox.showwarning("配置警告", "请先配置 API 设置后再重试。", parent=self.win)
            return
        failed = int(getattr(task, "failed_count", 0) or 0)
        if failed == 0 and not task.failed_indices:
            messagebox.showinfo(
                "提示",
                "未检测到失败行。如需重新翻译，请先清除对应行的译文。",
                parent=self.win,
            )
            return
        self.manager.start_task(task.task_id)

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
