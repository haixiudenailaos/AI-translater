#!/usr/bin/env python3
"""
队列翻译管理窗口
展示任务列表，支持添加/开始/暂停/取消/查看详情/一键导出/翻译图片。
"""

import json
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..core.concurrent_manager import ConcurrentTranslationManager
from .task_detail_window import TaskDetailWindow
from .ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox


class ConcurrentWindow:
    """队列翻译管理窗口"""

    # 队列翻译并发优化阶段 4：状态文案覆盖新状态机的全部状态
    # （QueueTaskState：pending / ready / running / pause_requested / paused /
    #  completed / partial / cancelled / error）。
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

    def __init__(self, parent, config_manager, app_paths=None, manager=None):
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
        """
        self.parent = parent
        self.config_manager = config_manager
        self.app_paths = app_paths  # Manga Provider 模型目录使用
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

        self.win = tk.Toplevel(parent)
        self.win.title("队列翻译管理")
        self.win.geometry("820x520")
        self.win.minsize(680, 400)

        # R2-BUG-013：注册 WM_DELETE_WINDOW
        # P1-3：关闭窗口只取消订阅，不停止任务（除非窗口拥有 manager）
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._schedule_refresh()

    # ── UI构建 ──────────────────────────────────────
    def _build_ui(self):
        main = ttk.Frame(self.win, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # 工具栏
        toolbar = ttk.Frame(main)
        toolbar.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(toolbar, text="➕ 添加文件", command=self._add_files).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="▶ 全部开始", command=self._start_all).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="⏸ 全部暂停", command=self._pause_all).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="✖ 全部取消", command=self._cancel_all).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="🗑 移除已完成", command=self._remove_finished).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="📥 一键导出", command=self._export_all).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="🖼️ 翻译图片", command=self._translate_all_images).pack(
            side=tk.LEFT, padx=(0, 4)
        )

        # 任务列表
        cols = ("task_id", "file_name", "status", "progress", "in_flight")
        self.tree = ttk.Treeview(main, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("task_id", text="ID")
        self.tree.heading("file_name", text="文件名")
        self.tree.heading("status", text="状态")
        self.tree.heading("progress", text="进度")
        self.tree.heading("in_flight", text="在途批次")
        self.tree.column("task_id", width=70, minwidth=60, anchor="center", stretch=False)
        self.tree.column("file_name", width=320, minwidth=150, anchor="w")
        self.tree.column("status", width=80, minwidth=60, anchor="center", stretch=False)
        self.tree.column("progress", width=90, minwidth=70, anchor="center", stretch=False)
        self.tree.column("in_flight", width=80, minwidth=60, anchor="center", stretch=False)

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
            font=("TkDefaultFont", 9),
            foreground="#444",
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

        任务继续在后台运行；真正停止任务由 ``MainWindow.on_closing``
        或显式 "全部取消" 操作触发。

        向后兼容：若窗口自建并拥有 manager（``_owns_manager=True``），
        仍调用 ``manager.close()`` 释放资源（旧路径/单测）。
        """
        if self._closed:
            return
        self._closed = True

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

        队列翻译并发优化阶段 4：优先使用不可变 ``QueueSnapshot`` 中的
        ``in_flight_batches`` 字段显示在途批次数；snapshot 缺失时回退到
        ``get_all_tasks()``。两种路径都在主线程读取，无跨线程 Tk 调用。
        """
        snapshot = self.manager.get_snapshot()
        # 构建 task_id -> in_flight_batches 映射
        in_flight_map = {}
        if snapshot is not None:
            for t in snapshot.tasks:
                in_flight_map[t.task_id] = t.in_flight_batches

        tasks = self.manager.get_all_tasks()
        existing = {self.tree.item(iid)["values"][0]: iid for iid in self.tree.get_children()}

        seen_ids = set()
        for task in tasks:
            seen_ids.add(task.task_id)
            status_text = self.STATUS_MAP.get(task.status, task.status)
            progress_text = f"{task.progress:.0f}%"
            in_flight_text = str(in_flight_map.get(task.task_id, ""))
            vals = (task.task_id, task.file_name, status_text, progress_text, in_flight_text)

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
    def _add_files(self):
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
            if t.status in ("running", "ready", "pending", "pause_requested", "paused")
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

        R2-BUG-011：默认只导出 completed 任务，partial 任务不进入普通导出集合。
        存在 partial 任务时提示用户可重新翻译失败行。
        """
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

        out_path = Path(out_dir)
        success, fail = 0, 0
        epub_proc = self.manager._epub_processor

        for task in completed:
            try:
                if task.file_type == "txt":
                    src = Path(task.file_path).with_name(f"{Path(task.file_path).stem}_译文.txt")
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
                            image_text_map = json.loads(text_trans_file.read_text(encoding="utf-8"))
                        except Exception:
                            pass

                    epub_proc.export_epub(str(mapping_p), str(epub_out), image_map, image_text_map)
                    success += 1
                else:
                    fail += 1
            except Exception:
                fail += 1

        msg = f"成功: {success}，失败: {fail}\n导出目录: {out_dir}"
        if partial_tasks:
            msg += (
                f"\n\n注意：{len(partial_tasks)} 个「部分完成」的任务未导出（存在失败行）。\n"
                "请在详情页点击「开始」继续翻译失败行后再导出。"
            )
        messagebox.showinfo("导出完成", msg, parent=self.win)

    def _translate_all_images(self):
        """一键翻译所有已完成EPUB任务中的图片

        走默认 Manga Provider，移除原火山引擎 Key 硬编码：
        - Manga external_llm 复用文本翻译 API 配置，因此仍检查 is_api_configured。
        - 单任务失败/校验失败/无文字/取消都不调用火山 AI Provider。
        """
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

            success, fail = 0, 0

            try:
                for idx, task in enumerate(epub_tasks, 1):
                    try:
                        self.win.after(
                            0,
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
                            self.win.after(
                                0,
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
                        fail += 1
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

            def done():
                self.win.title(original_title)
                messagebox.showinfo(
                    "图片翻译完成",
                    f"成功: {success}，失败: {fail}",
                    parent=self.win,
                )

            self.win.after(0, done)

        threading.Thread(target=run, daemon=True).start()

    def _create_manga_provider(self):
        """创建 Manga Provider（与 ImageTranslationHandler 一致）。"""
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
