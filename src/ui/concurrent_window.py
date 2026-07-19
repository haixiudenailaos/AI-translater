#!/usr/bin/env python3
"""
队列翻译管理窗口
展示任务列表，支持添加/开始/暂停/取消/查看详情/一键导出/翻译图片。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..application.error_handling import format_diagnostic_info
from ..core.concurrent_manager import ConcurrentTranslationManager
from ..domain.edition import EditionCapabilities, detect_edition_capabilities
from .task_detail_window import TaskDetailWindow
from .theme import COLORS, FONT_APP_SMALL
from .ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox


class ConcurrentWindow:
    """队列翻译管理窗口"""

    # 队列翻译并发优化阶段 4：状态文案覆盖新状态机的全部状态
    # （QueueTaskState：pending / ready / running / pause_requested / paused /
    #  completed / partial / cancelled / error）。
    STATUS_MAP = {
        "pending": "等待中",
        "preparing": "准备中",
        "ready": "准备中",
        "running": "翻译中",
        "pause_requested": "暂停中",
        "paused": "已暂停",
        "finalizing": "正在保存",
        "completed": "已完成",
        "partial": "部分完成",
        "cancelled": "已取消",
        "error": "出错",
    }

    def _manga_enabled(self) -> bool:
        capabilities = getattr(self, "edition_capabilities", None)
        if capabilities is None:
            capabilities = detect_edition_capabilities()
        return capabilities.manga_enabled

    def __init__(
        self,
        parent,
        config_manager,
        app_paths=None,
        manager=None,
        edition_capabilities: EditionCapabilities | None = None,
    ):
        """初始化队列翻译管理窗口。

        P1-3：``manager`` 由应用生命周期所有（``MainWindow`` 持有），
        窗口只订阅快照。关闭窗口不停止任务，只取消订阅。

        Args:
            parent: 父 Tk 窗口
            config_manager: 配置管理器
            app_paths: 应用路径（Manga Provider 模型目录）
            manager: 外部注入的 ``ConcurrentTranslationManager``。若提供，
                窗口不拥有其生命周期，关闭时只取消订阅；若为 None，
                为向后兼容由本窗口自建并拥有（旧路径，将逐步淘汰）。
            edition_capabilities: Text/Full 版本能力契约。Text 版本禁用 Manga 入口。
        """
        self.parent = parent
        self.config_manager = config_manager
        self.app_paths = app_paths  # Manga Provider 模型目录使用
        self.edition_capabilities = (
            edition_capabilities
            if edition_capabilities is not None
            else detect_edition_capabilities()
        )
        # P1-3：manager 归应用所有，窗口只订阅快照
        if manager is not None:
            self.manager = manager
            self._owns_manager = False
        else:
            # 向后兼容路径：窗口自建并拥有 manager（旧调用方/测试）
            self.manager = ConcurrentTranslationManager(config_manager, app_paths=app_paths)
            self._owns_manager = True
        self.manager.set_progress_callback(self._on_task_update)
        self._detail_windows: dict = {}
        self._closed = False  # R2-BUG-013：标记窗口是否已关闭，防止后台回调投递到已销毁窗口

        # P1-1：UI 回调邮箱 + Tk 主线程事件泵
        # manager 的 progress_callback 可能从工作线程调用，
        # 通过邮箱提交回调避免跨线程 Tk 调用。
        self._ui_mailbox = UICallbackMailbox()
        self._ui_pump = TkUICallbackPump(parent, self._ui_mailbox)
        self._ui_pump.start()

        # P1-8：图片翻译 worker 状态机
        # - _image_translate_busy：防止重复启动（按钮禁用入口）
        # - _image_translate_run_id：单调递增，回调闭包捕获后比对，
        #   迟到回调（旧 run_id）被安全丢弃，避免旧进度覆盖新状态
        # - _image_cancel_event：用户取消信号，worker 在任务间隙检查
        # - _image_service：运行中的 service 引用，供 cancel 调用
        self._image_translate_busy = False
        self._image_translate_run_id = 0
        self._image_cancel_event = threading.Event()
        self._image_service = None

        # P1-9：批量导出 worker 状态机（与图片翻译同构）
        # - _export_busy：防止重复启动，按钮在 idle/busy 间切换文案与命令
        # - _export_run_id：丢弃迟到回调
        # - _export_cancel_event：用户取消信号，导出循环间隙检查
        self._export_busy = False
        self._export_run_id = 0
        self._export_cancel_event = threading.Event()

        # PERF-6: Queue input parsing can read large TXT files or build EPUB
        # mappings. Keep it out of Tk and cap concurrent parsing work.
        self._import_busy = False
        self._import_generation = 0
        self._import_cancel_event = threading.Event()

        self.win = tk.Toplevel(parent)
        self.win.title("队列翻译管理")
        self.win.geometry("820x520")
        self.win.minsize(680, 400)

        # R2-BUG-013：注册 WM_DELETE_WINDOW
        # P1-3：关闭窗口只取消订阅，不停止任务（除非窗口拥有 manager）
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._install_fingerprint_dialog_bridge()
        self._schedule_refresh()

    # ── UI构建 ──────────────────────────────────────
    def _build_ui(self):
        main = ttk.Frame(self.win, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # 工具栏
        toolbar = ttk.Frame(main)
        toolbar.pack(fill=tk.X, pady=(0, 6))

        self.add_files_btn = ttk.Button(toolbar, text="添加文件", command=self._add_files)
        self.add_files_btn.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="全部开始", command=self._start_all).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="全部暂停", command=self._pause_all).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="全部取消", command=self._cancel_all).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="移除已完成", command=self._remove_finished).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        # P1-9：导出按钮在 idle/busy 间动态切换（导出/取消导出）
        self.export_all_btn = ttk.Button(toolbar, text="一键导出", command=self._export_all)
        self.export_all_btn.pack(side=tk.LEFT, padx=(0, 4))
        # P1-8：图片翻译按钮在 idle/busy 间动态切换命令与文案，
        # 替代原本每次点击就新起线程、无防重复启动的旧实现。
        self.translate_images_btn = ttk.Button(
            toolbar, text="翻译图片", command=self._translate_all_images
        )
        self.translate_images_btn.pack(side=tk.LEFT, padx=(0, 4))
        if not self._manga_enabled():
            self.translate_images_btn.configure(state=tk.DISABLED)

        # 任务列表
        # P1-UX-3：新增 failed / error_summary 列展示失败行数与脱敏错误摘要
        cols = (
            "task_id",
            "file_name",
            "status",
            "progress",
            "failed",
            "error_summary",
            "in_flight",
        )
        self.tree = ttk.Treeview(main, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("task_id", text="ID")
        self.tree.heading("file_name", text="文件名")
        self.tree.heading("status", text="状态")
        self.tree.heading("progress", text="进度")
        self.tree.heading("failed", text="失败")
        self.tree.heading("error_summary", text="错误摘要")
        self.tree.heading("in_flight", text="在途批次")
        self.tree.column("task_id", width=70, minwidth=60, anchor="center", stretch=False)
        self.tree.column("file_name", width=240, minwidth=120, anchor="w")
        self.tree.column("status", width=70, minwidth=55, anchor="center", stretch=False)
        self.tree.column("progress", width=70, minwidth=55, anchor="center", stretch=False)
        self.tree.column("failed", width=50, minwidth=40, anchor="center", stretch=False)
        self.tree.column("error_summary", width=160, minwidth=80, anchor="w")
        self.tree.column("in_flight", width=70, minwidth=55, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        # 双击打开详情
        self.tree.bind("<Double-Button-1>", self._on_double_click)
        # UX-6：键盘可达——Enter 打开详情、Shift+F10 上下文菜单、
        # Delete 取消/移除（带确认）、Escape 关闭窗口
        self.tree.bind("<Return>", lambda _e: self._open_detail_selected())
        self.tree.bind("<Shift-F10>", self._on_keyboard_context_menu)
        self.tree.bind("<Delete>", lambda _e: self._confirm_remove_selected())
        self.win.bind("<Escape>", lambda _e: self._on_close())
        # 右键菜单
        self.ctx_menu = tk.Menu(self.win, tearoff=0)
        self.ctx_menu.add_command(label="开始/恢复", command=self._ctx_start)
        self.ctx_menu.add_command(label="暂停", command=self._ctx_pause)
        self.ctx_menu.add_command(label="取消", command=self._ctx_cancel)
        self.ctx_menu.add_separator()
        # P1-UX-3：错误恢复动作——重试失败行、复制诊断信息、打开日志目录
        self.ctx_menu.add_command(label="重试失败行", command=self._ctx_retry_failed)
        self.ctx_menu.add_command(label="复制诊断信息", command=self._ctx_copy_diagnostics)
        self.ctx_menu.add_command(label="打开日志目录", command=self._ctx_open_log_dir)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="查看详情", command=self._ctx_detail)
        self.ctx_menu.add_command(label="移除", command=self._ctx_remove)
        self.tree.bind("<Button-3>", self._on_right_click)

        # 队列翻译并发优化阶段 4：队列指标状态栏（消费不可变 QueueSnapshot）
        # 工作线程零 Tk 调用——所有数据来自 Coordinator 发布的不可变快照，
        # 主线程 500ms 轮询一次。
        metrics_bar = ttk.Frame(self.win, padding=(8, 4))
        metrics_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.metrics_var = tk.StringVar(
            value="并发: -/- | 429: 0 | 超时: 0 | 吞吐: - 行/分 | ETA: -"
        )
        ttk.Label(
            metrics_bar,
            textvariable=self.metrics_var,
            font=FONT_APP_SMALL,
            foreground=COLORS["muted"],
        ).pack(side=tk.LEFT)

    # ── 窗口关闭 ────────────────────────────────────
    def _on_close(self):
        """R2-BUG-013 + P1-3：关闭窗口时取消订阅，不停止任务。

        P1-3：``manager`` 由应用生命周期所有，关闭窗口只做：
        1. 标记 ``_closed`` 让刷新循环和回调停止投递到已销毁窗口
        2. 关闭所有详情子窗口
        3. 解除 progress_callback 引用（避免窗口销毁后回调持有引用）
        4. P1-1：关闭 UI 回调事件泵
        5. 销毁本窗口

        P1-8：若图片翻译 worker 正在运行，触发取消让其在任务间隙退出，
        并让 ``_submit_ui`` 的迟到回调被邮箱丢弃。

        任务继续在后台运行；真正停止任务由 ``MainWindow.on_closing``
        或显式 "全部取消" 操作触发。

        向后兼容：若窗口自建并拥有 manager（``_owns_manager=True``），
        仍调用 ``manager.close()`` 释放资源（旧路径/单测）。
        """
        if self._closed:
            return
        self._closed = True

        # P1-8：取消图片翻译 worker，让其在任务间隙退出
        # 迟到回调会因邮箱关闭和 run_id 检查被双重丢弃
        # getattr 防御：部分单测通过 __new__ 绕过 __init__，未设置 P1-8/P1-9 新增属性
        if getattr(self, "_image_translate_busy", False):
            cancel_event = getattr(self, "_image_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()
            service = getattr(self, "_image_service", None)
            if service is not None:
                try:
                    from ..domain.image_translation import ImageTranslationProviderId

                    service.cancel(ImageTranslationProviderId.MANGA)
                except Exception:
                    pass

        # P1-9：取消批量导出 worker，让其在任务间隙退出
        if getattr(self, "_export_busy", False):
            cancel_event = getattr(self, "_export_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()

        # PERF-6: stop scheduling remaining file imports. In-flight parser
        # calls finish naturally and their late UI callbacks are discarded.
        import_cancel_event = getattr(self, "_import_cancel_event", None)
        if import_cancel_event is not None:
            import_cancel_event.set()

        setter = getattr(self.manager, "set_fingerprint_mismatch_callback", None)
        if callable(setter):
            setter(None)

        # 关闭所有详情子窗口
        for dw in list(self._detail_windows.values()):
            try:
                if dw.win.winfo_exists():
                    dw.win.destroy()
            except Exception:
                pass
        self._detail_windows.clear()

        # P1-3：解除 progress_callback 引用，避免窗口销毁后回调持有引用
        try:
            self.manager.set_progress_callback(None)
        except Exception:
            pass

        # P1-1：关闭 UI 回调事件泵
        pump = getattr(self, "_ui_pump", None)
        if pump is not None:
            try:
                pump.close()
            except Exception:
                pass
            self._ui_pump = None

        # P1-3：仅当窗口拥有 manager 时才关闭它（向后兼容路径）
        if self._owns_manager:
            try:
                self.manager.close()
            except Exception:
                pass

        # 销毁窗口
        try:
            if self.win.winfo_exists():
                self.win.destroy()
        except Exception:
            pass

    # ── 刷新 ────────────────────────────────────────
    def _schedule_refresh(self):
        if self._closed or not self.win.winfo_exists():
            return
        self._refresh_tree()
        self._refresh_metrics()
        self.win.after(500, self._schedule_refresh)

    def _on_task_update(self, task_id):
        """队列翻译并发优化阶段 2：来自 manager 的 kick 通知。

        P1-1：manager 的 ``_notify_progress`` 可能从工作线程调用此回调，
        通过 UI 回调邮箱提交刷新任务，避免跨线程 Tk 调用。
        回调参数可能为 None（``start_all`` 等批量命令）或 task_id。
        """
        if self._closed:
            return
        mailbox = getattr(self, "_ui_mailbox", None)
        if mailbox is None:
            return
        mailbox.submit(self._refresh_tree)
        mailbox.submit(self._refresh_metrics)

    def _refresh_tree(self):
        """刷新任务列表。

        队列翻译并发优化阶段 4：只消费轻量不可变 ``QueueSnapshot``。
        ``get_all_tasks()`` 会复制每个任务的全部原文与译文，仅供详情和
        旧兼容命令使用；不能在 500ms 刷新循环中调用。
        """
        snapshot = self.manager.get_snapshot()
        if snapshot is None:
            return
        existing = {self.tree.item(iid)["values"][0]: iid for iid in self.tree.get_children()}

        seen_ids = set()
        for task in snapshot.tasks:
            seen_ids.add(task.task_id)
            status_text = self.STATUS_MAP.get(task.status, task.status)
            progress_text = f"{task.progress:.0f}%"
            in_flight_text = str(task.in_flight_batches)
            # P1-UX-3：失败行数与脱敏错误摘要
            failed_count = int(task.failed_count or 0)
            failed_text = str(failed_count) if failed_count > 0 else ""
            # 优先使用脱敏 safe_message；缺失时回退 error_message；空字符串保持空
            error_summary = task.error_safe_message or task.error_message or ""
            # 截断过长的摘要，避免挤掉其他列
            if len(error_summary) > 80:
                error_summary = error_summary[:77] + "..."
            vals = (
                task.task_id,
                task.file_name,
                status_text,
                progress_text,
                failed_text,
                error_summary,
                in_flight_text,
            )

            if task.task_id in existing:
                self.tree.item(existing[task.task_id], values=vals)
            else:
                self.tree.insert("", "end", values=vals)

        # 移除已删除的任务
        for tid, iid in existing.items():
            if tid not in seen_ids:
                self.tree.delete(iid)

    def _refresh_metrics(self):
        """刷新队列指标状态栏（消费不可变 QueueSnapshot）。

        阶段 4：UI 主线程每 500ms 调用一次，从 Coordinator 发布的快照中读取
        并发数、429 计数、超时计数、吞吐和 ETA，无工作线程 Tk 调用。
        """
        snapshot = self.manager.get_snapshot()
        if snapshot is None:
            self.metrics_var.set("并发: -/- | 429: 0 | 超时: 0 | 吞吐: - 行/分 | ETA: -")
            return
        m = snapshot.metrics
        if m.is_blocked:
            concurrency_text = f"冷却 {m.cooldown_remaining:.0f}s"
        else:
            concurrency_text = f"{m.active_requests}/{m.current_limit}"
        throughput_text = (
            f"{m.throughput_lines_per_minute:.0f}" if m.throughput_lines_per_minute > 0 else "-"
        )
        if m.eta_seconds is None:
            eta_text = "-"
        elif m.eta_seconds < 60:
            eta_text = f"{m.eta_seconds:.0f}s"
        else:
            eta_text = f"{m.eta_seconds / 60:.1f}分"
        self.metrics_var.set(
            f"并发: {concurrency_text} (硬上限 {m.hard_cap}) | "
            f"429: {m.total_429} | 超时: {m.total_timeouts} | "
            f"成功: {m.total_success} | 吞吐: {throughput_text} 行/分 | ETA: {eta_text}"
        )

    # ── 工具栏操作 ──────────────────────────────────
    def _install_fingerprint_dialog_bridge(self) -> None:
        """Route recovery decisions through the Tk callback mailbox.

        Queue imports parse files in worker threads. The project-recovery path
        can ask the user whether changed source content should be mapped or
        discarded, so the manager must never invoke that dialog from a worker.
        """
        setter = getattr(self.manager, "set_fingerprint_mismatch_callback", None)
        if callable(setter):
            setter(self._request_fingerprint_mismatch_decision)

    def _request_fingerprint_mismatch_decision(self, info: dict) -> str:
        if threading.current_thread() is threading.main_thread():
            return self._show_fingerprint_mismatch_dialog(info)

        mailbox = getattr(self, "_ui_mailbox", None)
        if mailbox is None or getattr(self, "_closed", False):
            return "new"

        completed = threading.Event()
        decision = {"value": "new"}

        def ask_on_tk_thread() -> None:
            try:
                if not getattr(self, "_closed", False):
                    decision["value"] = self._show_fingerprint_mismatch_dialog(info)
            finally:
                completed.set()

        mailbox.submit(ask_on_tk_thread)
        while not completed.wait(0.1):
            if getattr(self, "_closed", False):
                return "new"
        return decision["value"]

    def _show_fingerprint_mismatch_dialog(self, info: dict) -> str:
        file_name = Path(str(info.get("file_path", ""))).name or "当前文件"
        old_lines = int(info.get("old_total_lines", 0) or 0)
        new_lines = int(info.get("new_total_lines", 0) or 0)
        choice = messagebox.askyesnocancel(
            "源文件已变化",
            (
                f"{file_name} 的内容已变化。\n\n"
                f"旧版本: {old_lines} 行\n新版本: {new_lines} 行\n\n"
                "选择“是”尝试按相同行号复用进度；\n"
                "选择“否”放弃旧进度；\n"
                "选择“取消”创建新任务并保留旧进度。"
            ),
            parent=self.win,
        )
        if choice is True:
            return "map"
        if choice is False:
            return "discard"
        return "new"

    def _add_files(self):
        if self._import_busy:
            self._import_cancel_event.set()
            self.add_files_btn.configure(state=tk.DISABLED, text="正在取消…")
            return
        paths = filedialog.askopenfilenames(
            title="选择要翻译的文件",
            filetypes=[
                ("支持的文件", "*.txt *.epub"),
                ("文本文件", "*.txt"),
                ("EPUB电子书", "*.epub"),
                ("所有文件", "*.*"),
            ],
            parent=self.win,
        )
        if not paths:
            return
        self._import_busy = True
        self._import_generation += 1
        generation = self._import_generation
        self._import_cancel_event = threading.Event()
        self.add_files_btn.configure(text="取消导入")

        def worker() -> None:
            failures: list[tuple[str, str]] = []
            completed = 0
            path_iter = iter(paths)
            in_flight: dict[Future, str] = {}

            def submit_next(executor: ThreadPoolExecutor) -> bool:
                if self._import_cancel_event.is_set():
                    return False
                try:
                    path = next(path_iter)
                except StopIteration:
                    return False
                in_flight[executor.submit(self.manager.add_task, path)] = path
                return True

            try:
                with ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="queue-import"
                ) as executor:
                    while len(in_flight) < 2 and submit_next(executor):
                        pass
                    while in_flight:
                        done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                        for future in done:
                            path = in_flight.pop(future)
                            try:
                                future.result()
                            except Exception as exc:  # noqa: BLE001
                                failures.append((path, str(exc)))
                            completed += 1
                            self._ui_mailbox.submit_keyed(
                                f"queue-import-{generation}",
                                lambda completed=completed: self._update_import_progress(
                                    generation, completed, len(paths)
                                ),
                            )
                        while len(in_flight) < 2 and submit_next(executor):
                            pass
            finally:
                self._ui_mailbox.submit(
                    lambda: self._finish_file_import(generation, completed, len(paths), failures)
                )

        threading.Thread(target=worker, name="queue-import-dispatch", daemon=True).start()

    def _update_import_progress(self, generation: int, completed: int, total: int) -> None:
        if generation != self._import_generation or self._closed:
            return
        self.add_files_btn.configure(text=f"取消导入 ({completed}/{total})")

    def _finish_file_import(
        self,
        generation: int,
        completed: int,
        total: int,
        failures: list[tuple[str, str]],
    ) -> None:
        if generation != self._import_generation or self._closed:
            return
        cancelled = self._import_cancel_event.is_set()
        self._import_busy = False
        self.add_files_btn.configure(state=tk.NORMAL, text="添加文件")
        self._refresh_tree()
        if failures:
            details = "\n".join(f"- {Path(path).name}: {message}" for path, message in failures[:8])
            more = "" if len(failures) <= 8 else f"\n另有 {len(failures) - 8} 项失败。"
            messagebox.showwarning("部分文件未加入队列", f"{details}{more}", parent=self.win)
        if cancelled:
            self.metrics_var.set(f"已取消队列导入：已处理 {completed}/{total} 个文件")

    def _start_all(self):
        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置", parent=self.win)
            return
        self.manager.start_all()

    def _pause_all(self):
        self.manager.pause_all()

    def _cancel_all(self):
        """P1-3：取消全部任务前确认，显示任务数、已完成量和后果。

        防止用户误点 "全部取消" 破坏批量任务。确认后调用
        ``manager.cancel_all()`` 取消所有未完成任务。
        """
        tasks = self.manager.get_all_tasks()
        if not tasks:
            messagebox.showinfo("提示", "队列为空，无可取消的任务", parent=self.win)
            return

        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == "completed")
        active = sum(
            1
            for t in tasks
            if t.status
            in (
                "running",
                "ready",
                "preparing",
                "pending",
                "pause_requested",
                "paused",
                "finalizing",
            )
        )
        finished = sum(1 for t in tasks if t.status in ("cancelled", "error", "partial"))

        # 无活动任务时直接返回，不弹确认
        if active == 0:
            messagebox.showinfo(
                "提示",
                f"当前无活动任务（共 {total} 个，已完成 {completed} 个）。",
                parent=self.win,
            )
            return

        msg = (
            f"即将取消 {active} 个活动任务。\n\n"
            f"队列概况：\n"
            f"  - 总数：{total}\n"
            f"  - 已完成：{completed}\n"
            f"  - 活动中：{active}\n"
            f"  - 已结束（取消/出错/部分）：{finished}\n\n"
            f"后果：\n"
            f"- 活动任务的已翻译内容会保留并落盘\n"
            f"- 未开始的行不会翻译\n"
            f"- 此操作不可撤销\n\n"
            f"确认取消全部活动任务？"
        )
        if not messagebox.askyesno("确认取消全部", msg, parent=self.win):
            return

        self.manager.cancel_all()

    def _remove_finished(self):
        for task in list(self.manager.get_all_tasks()):
            if task.status in ("completed", "cancelled", "error"):
                self.manager.remove_task(task.task_id)
        self._refresh_tree()

    def _export_all(self):
        """一键导出所有已完成任务的翻译结果

        P1-9：导出循环改为后台 job，通过邮箱投递进度与完成提示，UI 不阻塞。
        按钮在 idle/busy 间动态切换为「取消导出」，取消信号在任务间隙检查。

        R2-BUG-011：默认只导出 completed 任务，partial 任务不进入普通导出集合。
        存在 partial 任务时提示用户可重新翻译失败行。
        """
        # P1-9：busy 入口禁用，防止重复启动
        if self._export_busy:
            messagebox.showinfo(
                "正在导出",
                "导出正在进行中，请等待完成或点击按钮取消。",
                parent=self.win,
            )
            return

        all_tasks = self.manager.get_all_tasks()
        completed = [t for t in all_tasks if t.status == "completed"]
        partial_tasks = [t for t in all_tasks if t.status == "partial"]
        if not completed:
            if partial_tasks:
                messagebox.showinfo(
                    "提示",
                    "没有已完成的任务可导出。\n\n"
                    f"检测到 {len(partial_tasks)} 个「部分完成」的任务（存在失败行），\n"
                    "请在详情页点击「开始」继续翻译失败行后再导出。",
                    parent=self.win,
                )
            else:
                messagebox.showinfo("提示", "没有已完成的任务可导出", parent=self.win)
            return

        out_dir = filedialog.askdirectory(title="选择导出目录", parent=self.win)
        if not out_dir:
            return

        # P1-9：进入 busy 态，按钮切换为取消入口
        self._export_busy = True
        self._export_run_id += 1
        run_id = self._export_run_id
        self._export_cancel_event.clear()
        self._set_export_button_busy(True)

        total = len(completed)
        original_title = self.win.title()
        out_path = Path(out_dir)

        def run():
            success, fail, cancelled = 0, 0, 0
            epub_proc = self.manager._epub_processor

            try:
                for idx, task in enumerate(completed, 1):
                    # P1-9：任务间隙检查取消信号
                    if self._export_cancel_event.is_set():
                        cancelled = total - idx + 1
                        break
                    try:
                        # P1-9：进度通过邮箱投递，零跨线程 Tk 调用
                        self._submit_export_ui(
                            run_id,
                            lambda i=idx: self.win.title(f"队列翻译管理 - 正在导出 {i}/{total}"),
                        )

                        if task.file_type == "txt":
                            src = Path(task.file_path).with_name(
                                f"{Path(task.file_path).stem}_译文.txt"
                            )
                            if src.exists():
                                shutil.copy2(str(src), str(out_path / src.name))
                                success += 1
                            else:
                                fail += 1
                        elif task.file_type == "epub" and task.mapping_dir:
                            mapping_p = Path(task.mapping_dir)
                            epub_out = out_path / f"{Path(task.file_path).stem}_译文.epub"

                            image_map = None
                            result_file = mapping_p / "image_translation_result.json"
                            if result_file.exists():
                                try:
                                    raw = json.loads(result_file.read_text(encoding="utf-8"))
                                    # R2-BUG-018：兼容新旧格式
                                    if isinstance(raw, dict) and "result_map" in raw:
                                        image_map = raw["result_map"]
                                    else:
                                        image_map = raw
                                except Exception:
                                    pass

                            image_text_map = None
                            text_trans_file = mapping_p / "image_text_translations.json"
                            if text_trans_file.exists():
                                try:
                                    image_text_map = json.loads(
                                        text_trans_file.read_text(encoding="utf-8")
                                    )
                                except Exception:
                                    pass

                            epub_proc.export_epub(
                                str(mapping_p), str(epub_out), image_map, image_text_map
                            )
                            success += 1
                        else:
                            fail += 1
                    except Exception:
                        fail += 1
            finally:
                # P1-9：恢复按钮 idle 态，通过邮箱投递完成提示
                self._submit_export_ui(run_id, lambda: self._set_export_button_busy(False))
                self._export_busy = False

                def done():
                    try:
                        self.win.title(original_title)
                    except Exception:
                        pass
                    cancelled_note = f"，已取消: {cancelled}" if cancelled else ""
                    msg = f"成功: {success}，失败: {fail}{cancelled_note}\n导出目录: {out_dir}"
                    if partial_tasks and not cancelled:
                        msg += (
                            f"\n\n注意：{len(partial_tasks)} 个「部分完成」的任务未导出（存在失败行）。\n"
                            "请在详情页点击「开始」继续翻译失败行后再导出。"
                        )
                    messagebox.showinfo("导出完成", msg, parent=self.win)

                self._submit_export_ui(run_id, done)

        threading.Thread(target=run, daemon=True).start()

    def _cancel_export(self):
        """P1-9：取消正在进行的批量导出，在任务间隙退出循环。"""
        if not self._export_busy:
            return
        self._export_cancel_event.set()

    def _set_export_button_busy(self, busy: bool) -> None:
        """P1-9：busy 态下导出按钮文案改为取消入口。"""
        btn = getattr(self, "export_all_btn", None)
        if btn is None:
            return
        try:
            if busy:
                btn.configure(text="取消导出", command=self._cancel_export)
            else:
                btn.configure(text="一键导出", command=self._export_all)
        except Exception:
            pass

    def _submit_export_ui(self, run_id: int, callback) -> None:
        """P1-9：通过邮箱提交导出 UI 回调，丢弃迟到回调。

        与 ``_submit_ui`` 同构，但比对 ``_export_run_id``。
        """

        def guarded():
            if self._export_run_id != run_id:
                return
            callback()

        self._ui_mailbox.submit(guarded)

    def _translate_all_images(self):
        """一键翻译所有已完成EPUB任务中的图片

        P1-8：worker 通过 ``UICallbackMailbox`` 提交 UI 更新，零跨线程 Tk 调用。
        新增 busy/run_id 防重复启动与迟到回调丢弃，新增 cancel_event 用户取消入口。

        走默认 Manga Provider，移除原火山引擎 Key 硬编码：
        - Manga external_llm 复用文本翻译 API 配置，因此仍检查 is_api_configured。
        - 单任务失败/校验失败/无文字/取消都不调用火山 AI Provider。
        """
        if not self._manga_enabled():
            messagebox.showwarning(
                "图片翻译",
                "当前为 Text Edition，未打包本地 Manga 推理依赖。请使用 Full Edition。",
                parent=self.win,
            )
            return

        # P1-8：busy 入口禁用，防止重复启动
        if self._image_translate_busy:
            messagebox.showinfo(
                "正在翻译图片",
                "图片翻译正在进行中，请等待完成或点击按钮取消。",
                parent=self.win,
            )
            return

        epub_tasks = [
            t
            for t in self.manager.get_all_tasks()
            if t.status == "completed" and t.file_type == "epub" and t.mapping_dir
        ]
        if not epub_tasks:
            messagebox.showinfo("提示", "没有已完成的EPUB任务", parent=self.win)
            return

        # Manga external_llm 复用文本翻译 API 配置，需要 API 配置；
        # 不再检查火山 Key——Manga 默认模块与火山 Key 无关。
        if not self.config_manager.is_api_configured():
            messagebox.showwarning(
                "配置警告",
                "请先配置文本翻译 API 设置（Manga 模块复用该配置翻译图片文字）",
                parent=self.win,
            )
            return

        target_lang = self.config_manager.get_app_config().get("target_language", "中文")
        total_tasks = len(epub_tasks)
        original_title = self.win.title()

        # P1-8：进入 busy 态，切换按钮为取消入口，重置取消信号与 run_id
        self._image_translate_busy = True
        self._image_translate_run_id += 1
        run_id = self._image_translate_run_id
        self._image_cancel_event.clear()
        self._set_image_button_busy(True)

        def run():
            from ..application.image_translation_service import ImageTranslationService
            from ..domain.errors import (
                ImageTranslationCancelled,
                ImageTranslationConfigError,
            )
            from ..domain.image_translation import (
                ImageTranslationProgress,
                ImageTranslationProviderId,
            )
            from ..domain.translation import OperationStatus
            from ..infrastructure.image_translation.registry import (
                ImageTranslationProviderRegistry,
            )

            # 构建 Service：仅注册 Manga（队列默认模块）
            registry = ImageTranslationProviderRegistry()
            manga_provider = self._create_manga_provider()
            if manga_provider is not None:
                registry.register(manga_provider)
            service = ImageTranslationService(self.config_manager, registry)
            # P1-8：暴露 service 引用供 cancel 调用
            self._image_service = service

            success, fail, cancelled = 0, 0, 0

            try:
                for idx, task in enumerate(epub_tasks, 1):
                    # P1-8：任务间隙检查取消信号
                    if self._image_cancel_event.is_set():
                        cancelled = total_tasks - idx + 1
                        break

                    try:
                        # P1-8：通过邮箱提交 UI 更新，回调闭包捕获 run_id，
                        # 迟到回调（run_id 不匹配）被安全丢弃
                        self._submit_ui(
                            run_id,
                            lambda i=idx: self.win.title(
                                f"队列翻译管理 - Manga 图片翻译 {i}/{total_tasks}"
                            ),
                        )

                        request = service.make_request(
                            mapping_dir=Path(task.mapping_dir),
                            target_language=target_lang,
                            provider_id=ImageTranslationProviderId.MANGA,
                        )

                        # 执行前校验：Manga 校验失败不调用 AI，记为失败
                        errors = service.validate_request(request)
                        if errors:
                            fail += 1
                            continue

                        def on_progress(progress: ImageTranslationProgress, _i=idx):
                            # P1-8：进度回调也走邮箱 + run_id 检查
                            self._submit_ui(
                                run_id,
                                lambda p=progress, _idx=_i: self.win.title(
                                    f"队列翻译管理 - [{_idx}/{total_tasks}] "
                                    f"{p.stage} {p.current}/{p.total}"
                                ),
                            )

                        result = service.translate(request, on_progress)
                        if result.status in (
                            OperationStatus.SUCCEEDED,
                            OperationStatus.PARTIAL,
                        ):
                            # 有成功结果即计入成功
                            success += 1
                        else:
                            fail += 1
                    except ImageTranslationCancelled:
                        # P1-8：单任务被取消，计入 cancelled 并结束循环
                        cancelled = total_tasks - idx + 1
                        break
                    except ImageTranslationConfigError:
                        # 配置错误：跳过该任务，不切换 AI
                        fail += 1
                    except Exception:
                        fail += 1
            finally:
                # 关闭 Provider 生命周期，释放模型/线程资源
                try:
                    service.close_provider(ImageTranslationProviderId.MANGA)
                except Exception:
                    pass
                self._image_service = None

                # P1-8：恢复按钮 idle 态，并通过邮箱投递完成提示
                self._submit_ui(run_id, lambda: self._set_image_button_busy(False))
                self._image_translate_busy = False

                def done():
                    try:
                        self.win.title(original_title)
                    except Exception:
                        pass
                    cancelled_note = f"，已取消: {cancelled}" if cancelled else ""
                    messagebox.showinfo(
                        "图片翻译完成",
                        f"成功: {success}，失败: {fail}{cancelled_note}",
                        parent=self.win,
                    )

                self._submit_ui(run_id, done)

        threading.Thread(target=run, daemon=True).start()

    def _cancel_image_translation(self):
        """P1-8：取消正在进行的图片翻译。

        设置 cancel_event 让 worker 在任务间隙退出循环，并调用
        ``service.cancel`` 触发当前 Provider 的取消路径。
        """
        if not self._image_translate_busy:
            return
        self._image_cancel_event.set()
        service = self._image_service
        if service is not None:
            try:
                from ..domain.image_translation import ImageTranslationProviderId

                service.cancel(ImageTranslationProviderId.MANGA)
            except Exception:
                pass

    def _set_image_button_busy(self, busy: bool) -> None:
        """P1-8：busy 态下按钮文案改为取消入口，并禁用重复启动。"""
        btn = getattr(self, "translate_images_btn", None)
        if btn is None:
            return
        try:
            if busy:
                btn.configure(
                    text="取消图片翻译",
                    command=self._cancel_image_translation,
                )
            else:
                btn.configure(
                    text="翻译图片",
                    command=self._translate_all_images,
                )
        except Exception:
            pass

    def _submit_ui(self, run_id: int, callback) -> None:
        """P1-8：通过邮箱提交 UI 回调，并丢弃迟到回调。

        回调闭包捕获 ``run_id``，执行前比对当前 ``_image_translate_run_id``。
        不匹配说明是旧 run 的迟到回调（窗口已开始新一轮或已结束），安全跳过。
        邮箱关闭后（窗口销毁）submit 也会被丢弃并计数。
        """

        def guarded():
            if self._image_translate_run_id != run_id:
                return
            callback()

        self._ui_mailbox.submit(guarded)

    def _create_manga_provider(self):
        """创建 Manga Provider（与 ImageTranslationHandler 一致）。"""
        if not self._manga_enabled():
            return None
        from ..infrastructure.image_translation.manga_provider import (
            MangaImageTranslationProvider,
        )

        img_config = self.config_manager.get_image_translation_config()
        manga_cfg = img_config.get("manga", {})

        model_dir = None
        if self.app_paths is not None:
            model_dir = Path(self.app_paths.data_dir) / "models" / "manga"
            model_dir.mkdir(parents=True, exist_ok=True)
        elif manga_cfg.get("model_dir"):
            model_dir = Path(manga_cfg["model_dir"])

        return MangaImageTranslationProvider(
            self.config_manager,
            model_dir=model_dir,
            resource_dir=(
                Path(self.app_paths.resource_dir) if self.app_paths is not None else None
            ),
            font_path=None,
            quality_preset=manga_cfg.get("quality_preset", "standard"),
            device=manga_cfg.get("device", "auto"),
            python_executable=manga_cfg.get("python_executable") or None,
        )

    # ── 双击详情 ────────────────────────────────────
    def _on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        vals = self.tree.item(item)["values"]
        if vals:
            self._open_detail(str(vals[0]))

    def _open_detail_selected(self):
        """UX-6：Enter 键打开当前选中任务详情。"""
        tid = self._selected_task_id()
        if tid:
            self._open_detail(tid)

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

    def _on_keyboard_context_menu(self, _event):
        """UX-6：Shift+F10 在选中行位置弹出上下文菜单。"""
        sel = self.tree.selection()
        if not sel:
            return
        bbox = self.tree.bbox(sel[0])
        if bbox:
            x = self.tree.winfo_rootx() + bbox[0] + bbox[2] // 2
            y = self.tree.winfo_rooty() + bbox[1] + bbox[3] // 2
        else:
            x = self.tree.winfo_rootx()
            y = self.tree.winfo_rooty()
        try:
            self.ctx_menu.tk_popup(x, y)
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

    # ── P1-UX-3：错误恢复与诊断动作 ────────────────────

    def _ctx_retry_failed(self):
        """重试当前任务的失败行。

        仅对 PARTIAL / ERROR 状态的任务有意义；其他状态给出提示。
        实现复用 ``manager.start_task`` / ``resume_task``——Coordinator 在
        重新启动时会跳过 ``completed_indices`` 和 ``manually_edited_indices``
        （P1-UX-2），所以失败行会自动进入下一轮待翻译集合。
        """
        tid = self._selected_task_id()
        if not tid:
            return
        task = self.manager.get_task(tid)
        if task is None:
            return
        failed = int(getattr(task, "failed_count", 0) or 0)
        if task.status not in ("partial", "error"):
            messagebox.showinfo(
                "提示",
                f"当前任务状态为 {task.status}，无需重试失败行。\n"
                "只有「部分完成」或「出错」状态的任务才能重试。",
                parent=self.win,
            )
            return
        if failed == 0 and not task.failed_indices:
            messagebox.showinfo(
                "提示",
                "未检测到失败行。如需重新翻译，请先清除对应行的译文。",
                parent=self.win,
            )
            return
        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置 API 设置后再重试。", parent=self.win)
            return
        # 触发重试——Coordinator 启动新一轮 attempt 时跳过已完成 / 手工编辑的行
        self.manager.start_task(tid)
        messagebox.showinfo(
            "已开始重试",
            f"已开始重试任务 {task.task_id} 的失败行。\n"
            "Coordinator 会自动跳过已完成和手工编辑的行。",
            parent=self.win,
        )

    def _ctx_copy_diagnostics(self):
        """复制当前任务的诊断信息到剪贴板。

        包含 correlation_id、类别、安全文案和建议动作，不包含原始堆栈、
        API Key 或完整文件路径——便于用户反馈问题给开发者。
        """
        tid = self._selected_task_id()
        if not tid:
            return
        task = self.manager.get_task(tid)
        if task is None:
            return
        # 从 TranslationTask 字段拼装可安全复制的诊断信息
        # 字段可能为 None（旧数据或无错误）；用空串兜底
        from ..application.error_handling import (
            ActionableError,
            ErrorCategory,
            sanitize_error_message,
        )

        category_str = getattr(task, "error_category", None) or "unknown"
        try:
            category = ErrorCategory(category_str)
        except ValueError:
            category = ErrorCategory.UNKNOWN
        actionable = ActionableError(
            category=category,
            safe_message=getattr(task, "error_safe_message", None) or "",
            recommended_action=getattr(task, "recommended_action", None) or "",
            retryable=bool(getattr(task, "error_retryable", False)),
            correlation_id=getattr(task, "correlation_id", None) or "",
        )
        diag = format_diagnostic_info(
            Exception(sanitize_error_message(task.error_message or "")),
            actionable,
            extra={
                "task_id": task.task_id,
                "file_name": task.file_name,
                "failed_count": getattr(task, "failed_count", 0),
            },
        )
        self.win.clipboard_clear()
        self.win.clipboard_append(diag)
        messagebox.showinfo(
            "已复制",
            "诊断信息已复制到剪贴板。\n可粘贴给开发者协助排查。",
            parent=self.win,
        )

    def _ctx_open_log_dir(self):
        """打开日志目录（优先 app_paths.log_dir，回退 ./logs）。

        日志文件包含完整异常堆栈与 correlation_id 关联的条目，
        便于用户主动反馈问题。
        """
        log_dir: Path
        if self.app_paths is not None and getattr(self.app_paths, "log_dir", None):
            log_dir = Path(self.app_paths.log_dir)
        else:
            log_dir = Path("logs").resolve()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "无法创建日志目录", f"路径: {log_dir}\n错误: {exc}", parent=self.win
            )
            return
        try:
            if os.name == "nt":
                os.startfile(str(log_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(log_dir)])
            else:
                subprocess.Popen(["xdg-open", str(log_dir)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "无法打开日志目录",
                f"路径: {log_dir}\n错误: {exc}\n\n请手动打开该目录。",
                parent=self.win,
            )

    def _confirm_remove_selected(self):
        """UX-6：Delete 键移除选中任务，运行中的任务先确认是否取消。"""
        tid = self._selected_task_id()
        if not tid:
            return
        task = self.manager.get_task(tid)
        if not task:
            return
        if task.status == "running":
            confirm = messagebox.askyesno(
                "确认取消",
                f"任务 {task.file_name} 正在运行。\n确定要取消并移除吗？已完成内容不会保留。",
                parent=self.win,
            )
            if not confirm:
                return
            self.manager.cancel_task(tid)
        self.manager.remove_task(tid)
        self._refresh_tree()
