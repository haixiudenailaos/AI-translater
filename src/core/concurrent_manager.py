#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
队列翻译任务管理器
负责管理多个翻译任务的生命周期（队列模式：逐个执行），不涉及UI。
"""

import uuid
import threading
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

from .translator import TranslatorEngine
from ..utils.file_handler import FileHandler
from .epub_processor import EPUBProcessor
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TranslationTask:
    """单个翻译任务的数据模型"""
    task_id: str
    file_path: str
    file_name: str
    file_type: str  # "txt" / "epub"
    status: str = "pending"  # pending / running / paused / completed / partial / cancelled / error
    progress: float = 0.0
    source_lines: List[str] = field(default_factory=list)
    target_lines: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    mapping_dir: Optional[str] = None  # EPUB专用
    failed_indices: List[int] = field(default_factory=list)  # R2-BUG-011：PARTIAL 时的失败行原索引


class ConcurrentTranslationManager:
    """队列翻译管理器（任务按添加顺序逐个执行）"""

    def __init__(self, config_manager, max_concurrent: int = 1, app_paths=None):
        self.config_manager = config_manager
        self.max_concurrent = max_concurrent
        self.tasks: Dict[str, TranslationTask] = {}
        self._lock = threading.Lock()  # 保护 tasks/engines/threads 的读写
        self._engines: Dict[str, TranslatorEngine] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._semaphore = threading.Semaphore(max_concurrent)
        self._progress_callback: Optional[Callable] = None
        self._file_handler = FileHandler()
        self._closed = False  # R2-BUG-012：关闭标志，防止关闭后继续调度
        # BUG-001：使用统一工作区目录
        self._epub_processor = EPUBProcessor(app_paths=app_paths)

    def set_progress_callback(self, callback: Callable):
        """设置UI进度回调"""
        self._progress_callback = callback

    def close(self):
        """R2-BUG-012：关闭管理器，禁止新任务调度，取消 pending，释放 API 资源。

        幂等可安全多次调用。关闭后 start_task/start_all/_start_next_pending 均不会创建新线程。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # 取消所有 pending 任务，停止 running 任务
            for task in self.tasks.values():
                if task.status in ("pending", "running", "paused"):
                    if task.status == "running":
                        task.status = "cancelled"
                    elif task.status == "pending":
                        task.status = "cancelled"
                    elif task.status == "paused":
                        task.status = "cancelled"
            engines = list(self._engines.values())
            self._engines.clear()
            # 移除进度回调，防止关闭后继续投递
            self._progress_callback = None
        for engine in engines:
            try:
                engine.stop()
                engine.close()
            except Exception as e:
                logger.warning("关闭队列翻译引擎失败: %s", e)

    def add_task(self, file_path: str) -> TranslationTask:
        """添加翻译任务，解析文件内容"""
        from pathlib import Path
        path = Path(file_path)
        file_type = "epub" if path.suffix.lower() == ".epub" else "txt"
        task_id = str(uuid.uuid4())[:8]

        task = TranslationTask(
            task_id=task_id,
            file_path=file_path,
            file_name=path.name,
            file_type=file_type,
        )

        # 解析文件内容
        if file_type == "epub":
            mapping_info = self._epub_processor.import_epub(file_path)
            task.mapping_dir = mapping_info["mapping_dir"]
            originals, translations = self._epub_processor.load_content_mapping(
                task.mapping_dir
            )
            task.source_lines = list(originals)
            task.target_lines = list(translations)
        else:
            content = self._file_handler.read_file(file_path)
            task.source_lines = content.splitlines()
            task.target_lines = [""] * len(task.source_lines)

        with self._lock:
            self.tasks[task_id] = task
        return task

    def start_task(self, task_id: str):
        """启动单个任务"""
        with self._lock:
            # R2-BUG-012：关闭后禁止启动新任务
            if self._closed:
                return
            task = self.tasks.get(task_id)
            if not task or task.status in ("running", "completed"):
                return
            task.status = "running"
            task.error_message = None

            engine = TranslatorEngine(self.config_manager)
            self._engines[task_id] = engine

            t = threading.Thread(
                target=self._run_task, args=(task_id,), daemon=True
            )
            self._threads[task_id] = t
        t.start()

    def pause_task(self, task_id: str):
        """暂停单个任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status != "running":
                return
            engine = self._engines.get(task_id)
            if engine:
                engine.pause()
            task.status = "paused"
        self._notify_progress(task_id)

    def resume_task(self, task_id: str):
        """恢复暂停的任务"""
        with self._lock:
            # R2-BUG-012：关闭后禁止恢复任务
            if self._closed:
                return
            task = self.tasks.get(task_id)
            if not task or task.status != "paused":
                return
            engine = self._engines.get(task_id)
            if engine:
                engine.resume()
            task.status = "running"
        self._notify_progress(task_id)

    def cancel_task(self, task_id: str):
        """取消任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status in ("completed", "cancelled"):
                return
            engine = self._engines.get(task_id)
            if engine:
                engine.stop()
            task.status = "cancelled"
        self._notify_progress(task_id)

    def start_all(self):
        """一键开始：恢复所有paused任务，然后启动队列中第一个pending任务"""
        with self._lock:
            # R2-BUG-012：关闭后禁止启动任务
            if self._closed:
                return
            task_ids = list(self.tasks.keys())
        for task_id in task_ids:
            task = self.get_task(task_id)
            if task and task.status == "paused":
                self.resume_task(task_id)
        self._start_next_pending()

    def pause_all(self):
        """一键暂停所有running任务"""
        with self._lock:
            items = [(tid, t.status) for tid, t in self.tasks.items()]
        for tid, status in items:
            if status == "running":
                self.pause_task(tid)

    def cancel_all(self):
        """一键取消所有非completed任务"""
        with self._lock:
            items = [(tid, t.status) for tid, t in self.tasks.items()]
        for tid, status in items:
            if status not in ("completed", "cancelled"):
                self.cancel_task(tid)

    def get_task(self, task_id: str) -> Optional[TranslationTask]:
        with self._lock:
            return self.tasks.get(task_id)

    def get_all_tasks(self) -> List[TranslationTask]:
        with self._lock:
            return list(self.tasks.values())

    def remove_task(self, task_id: str):
        """移除任务（仅限非running状态）"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status == "running":
                return
            self.tasks.pop(task_id, None)
            self._engines.pop(task_id, None)
            self._threads.pop(task_id, None)

    def save_task(self, task_id: str) -> bool:
        """R2-BUG-015：公开的保存接口，将任务译文持久化到文件。

        供详情页编辑后落盘使用。保存失败时返回 False，由调用方决定恢复策略。

        Returns:
            True 保存成功，False 保存失败或任务不存在
        """
        task = self.get_task(task_id)
        if not task:
            return False
        try:
            self._save_task_result(task)
            return True
        except Exception as e:
            logger.error("任务 %s 手动保存失败: %s", task_id, e)
            return False

    def _start_next_pending(self):
        """检查队列，如果没有running任务则启动第一个pending任务"""
        with self._lock:
            # R2-BUG-012：关闭后禁止调度新任务
            if self._closed:
                return
            has_running = any(t.status == "running" for t in self.tasks.values())
            if has_running:
                return
            for task_id, task in self.tasks.items():
                if task.status == "pending":
                    next_id = task_id
                    break
            else:
                return
        self.start_task(next_id)

    def _notify_progress(self, task_id: str):
        """通知UI更新"""
        if self._progress_callback:
            self._progress_callback(task_id)

    def _run_task(self, task_id: str):
        """在线程中执行翻译任务"""
        task = self.tasks.get(task_id)
        engine = self._engines.get(task_id)
        if not task or not engine:
            return

        self._semaphore.acquire()
        try:
            # R2-BUG-024：只翻译缺失行（target_lines 为空的行），已有译文不重译
            missing_indices = [
                i for i, t in enumerate(task.target_lines)
                if not t.strip() and i < len(task.source_lines)
                and task.source_lines[i].strip()
            ]
            missing_sources = [task.source_lines[i] for i in missing_indices]
            content = "\n".join(missing_sources)

            # R2-BUG-027：空内容也必须保存结果后才能标记完成
            if not content.strip():
                try:
                    self._save_task_result(task)
                    task.status = "completed"
                    task.progress = 100.0
                except Exception as save_err:
                    logger.error("任务 %s 空结果保存失败: %s", task_id, save_err)
                    task.status = "error"
                    task.error_message = f"翻译完成但保存失败：{save_err}"
                self._notify_progress(task_id)
                return

            def progress_cb(progress, batch_data):
                if task.status == "cancelled":
                    return
                task.progress = progress
                if batch_data and isinstance(batch_data, dict):
                    is_streaming = batch_data.get("streaming", False)
                    if not is_streaming:
                        batch_start = batch_data.get("batch_start", 0)
                        translated = batch_data.get("translated_lines", [])
                        for i, line in enumerate(translated):
                            # R2-BUG-024：通过 missing_indices 映射回原位置
                            rel_idx = batch_start + i
                            if 0 <= rel_idx < len(missing_indices):
                                actual_idx = missing_indices[rel_idx]
                                if actual_idx < len(task.target_lines):
                                    task.target_lines[actual_idx] = line.strip()
                self._notify_progress(task_id)

            def complete_cb(result):
                # BUG-004：基于结构化结果设置任务状态
                from .translation_result import TranslationStatus
                if result.is_cancelled:
                    task.status = "cancelled"
                    self._notify_progress(task_id)
                    return
                if result.is_failed:
                    task.status = "error"
                    task.error_message = result.error_message or "翻译失败"
                    self._notify_progress(task_id)
                    return
                # 成功或部分成功：将结构化结果通过 missing_indices 写回原位置
                if result.lines:
                    for i, line in enumerate(result.lines):
                        if i < len(missing_indices):
                            actual_idx = missing_indices[i]
                            if actual_idx < len(task.target_lines):
                                # 仅在译文非空时覆盖，避免覆盖已存在的成功译文
                                if line.strip():
                                    task.target_lines[actual_idx] = line.strip()
                # R2-BUG-011：区分 SUCCEEDED 和 PARTIAL，PARTIAL 不得标记 completed
                is_partial = (result.status == TranslationStatus.PARTIAL)
                if is_partial:
                    # 通过 missing_indices 将相对失败索引映射回原位置，便于继续失败行
                    task.failed_indices = [
                        missing_indices[i] for i in result.failed_indices
                        if i < len(missing_indices)
                    ]
                    if result.error_message:
                        task.error_message = result.error_message
                else:
                    task.failed_indices = []
                # BUG-006：只有结果保存成功后才能进入终态
                try:
                    self._save_task_result(task)
                    if is_partial:
                        task.status = "partial"
                    else:
                        task.status = "completed"
                        task.progress = 100.0
                except Exception as save_err:
                    logger.error("任务 %s 结果保存失败: %s", task_id, save_err)
                    task.status = "error"
                    task.error_message = f"翻译完成但保存失败：{save_err}"
                self._notify_progress(task_id)

            # 队列任务无人值守，使用大批次吞吐模式；主窗口仍使用交互式
            # translate_fast_mode，不受这里的批大小和进度策略影响。
            engine.translate_bulk_mode(content, progress_cb, complete_cb)

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
            self._notify_progress(task_id)
        finally:
            self._semaphore.release()
            self._start_next_pending()

    def _save_task_result(self, task: TranslationTask):
        """保存翻译结果到文件。

        BUG-006：保存失败时抛出异常，由调用方决定任务状态（不在此吞掉）。
        """
        from pathlib import Path
        if task.file_type == "epub" and task.mapping_dir:
            self._epub_processor.save_translations(
                task.mapping_dir, task.target_lines
            )
        else:
            tgt_path = Path(task.file_path).with_name(
                f"{Path(task.file_path).stem}_译文.txt"
            )
            content = "\n".join(task.target_lines)
            # write_file 现在使用原子写入，失败时抛出异常
            self._file_handler.write_file(str(tgt_path), content)
