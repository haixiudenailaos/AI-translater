#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并发翻译任务管理器
负责管理多个翻译任务的生命周期，不涉及UI。
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
    status: str = "pending"  # pending / running / paused / completed / cancelled / error
    progress: float = 0.0
    source_lines: List[str] = field(default_factory=list)
    target_lines: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    mapping_dir: Optional[str] = None  # EPUB专用


class ConcurrentTranslationManager:
    """并发翻译管理器"""

    def __init__(self, config_manager, max_concurrent: int = 3):
        self.config_manager = config_manager
        self.max_concurrent = max_concurrent
        self.tasks: Dict[str, TranslationTask] = {}
        self._lock = threading.Lock()  # 保护 tasks/engines/threads 的读写
        self._engines: Dict[str, TranslatorEngine] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._semaphore = threading.Semaphore(max_concurrent)
        self._progress_callback: Optional[Callable] = None
        self._file_handler = FileHandler()
        self._epub_processor = EPUBProcessor()

    def set_progress_callback(self, callback: Callable):
        """设置UI进度回调"""
        self._progress_callback = callback

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
        """一键开始所有pending/paused任务"""
        with self._lock:
            task_ids = list(self.tasks.keys())
        for task_id in task_ids:
            task = self.get_task(task_id)
            if task and task.status == "paused":
                self.resume_task(task_id)
            elif task and task.status == "pending":
                self.start_task(task_id)

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
            # 构建待翻译内容（跳过已有译文的行）
            content = "\n".join(task.source_lines)
            if not content.strip():
                task.status = "completed"
                task.progress = 100.0
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
                            idx = batch_start + i
                            if idx < len(task.target_lines):
                                task.target_lines[idx] = line.strip()
                self._notify_progress(task_id)

            def complete_cb():
                task.status = "completed"
                task.progress = 100.0
                self._save_task_result(task)
                self._notify_progress(task_id)

            engine.translate_fast_mode(content, progress_cb, complete_cb)

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
            self._notify_progress(task_id)
        finally:
            self._semaphore.release()

    def _save_task_result(self, task: TranslationTask):
        """保存翻译结果到文件"""
        from pathlib import Path
        try:
            if task.file_type == "epub" and task.mapping_dir:
                self._epub_processor.save_translations(
                    task.mapping_dir, task.target_lines
                )
            else:
                tgt_path = Path(task.file_path).with_name(
                    f"{Path(task.file_path).stem}_译文.txt"
                )
                content = "\n".join(task.target_lines)
                self._file_handler.write_file(str(tgt_path), content)
        except Exception as e:
            logger.error("保存任务结果失败: %s", e)
