#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
队列翻译管理窗口
展示任务列表，支持添加/开始/暂停/取消/查看详情/一键导出/翻译图片。
"""

import json
import shutil
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from ..core.concurrent_manager import ConcurrentTranslationManager
from .task_detail_window import TaskDetailWindow


class ConcurrentWindow:
    """队列翻译管理窗口"""

    STATUS_MAP = {
        "pending": "等待中",
        "running": "翻译中",
        "paused": "已暂停",
        "completed": "已完成",
        "partial": "部分完成",
        "cancelled": "已取消",
        "error": "出错",
    }

    def __init__(self, parent, config_manager, app_paths=None):
        self.parent = parent
        self.config_manager = config_manager
        self.app_paths = app_paths  # Manga Provider 模型目录使用
        self.manager = ConcurrentTranslationManager(config_manager, app_paths=app_paths)
        self.manager.set_progress_callback(self._on_task_update)
        self._detail_windows: dict = {}
        self._closed = False  # R2-BUG-013：标记窗口是否已关闭，防止后台回调投递到已销毁窗口

        self.win = tk.Toplevel(parent)
        self.win.title("队列翻译管理")
        self.win.geometry("780x480")
        self.win.minsize(640, 360)

        # R2-BUG-013：注册 WM_DELETE_WINDOW，关闭窗口时清理管理器资源
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

        ttk.Button(toolbar, text="➕ 添加文件", command=self._add_files).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="▶ 全部开始", command=self._start_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="⏸ 全部暂停", command=self._pause_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="✖ 全部取消", command=self._cancel_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="🗑 移除已完成", command=self._remove_finished).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="📥 一键导出", command=self._export_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="🖼️ 翻译图片", command=self._translate_all_images).pack(side=tk.LEFT, padx=(0, 4))

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
    # ── 窗口关闭 ────────────────────────────────────
    def _on_close(self):
        """R2-BUG-013：关闭窗口时清理管理器资源，取消所有任务和回调。"""
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

        # 关闭管理器（停止所有任务、释放 API 资源）
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
        self.win.after(500, self._schedule_refresh)

    def _on_task_update(self, task_id: str):
        """来自manager的回调（可能在子线程），调度到主线程"""
        if self._closed:
            return
        try:
            if self.win.winfo_exists():
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
                            image_text_map = json.loads(text_trans_file.read_text(encoding="utf-8"))
                        except Exception:
                            pass

                    epub_proc.export_epub(
                        str(mapping_p), str(epub_out), image_map, image_text_map
                    )
                    success += 1
                else:
                    fail += 1
            except Exception as e:
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
            t for t in self.manager.get_all_tasks()
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
                        self.win.after(0, lambda i=idx: self.win.title(
                            f"队列翻译管理 - Manga 图片翻译 {i}/{total_tasks}"
                        ))

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
            font_path=None,
            quality_preset=manga_cfg.get("quality_preset", "standard"),
            device=manga_cfg.get("device", "auto"),
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
