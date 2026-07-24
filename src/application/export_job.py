"""Background, atomic EPUB export orchestration without Tk dependencies."""

from __future__ import annotations

import os
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from zipfile import BadZipFile, ZipFile


@dataclass(frozen=True, slots=True)
class ExportProgress:
    stage: str
    message: str


@dataclass(frozen=True, slots=True)
class ExportResult:
    output_path: Path | None
    cancelled: bool = False
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.output_path is not None and not self.cancelled and self.error_message is None


class ExportJob:
    """Run an EPUB export in a worker and atomically publish only verified output."""

    def __init__(
        self,
        *,
        output_path: Path,
        save_mapping: Callable[[], None],
        export_to: Callable[[Path], Path | str],
        backup_existing: Callable[[Path], None] | None = None,
    ) -> None:
        self._output_path = Path(output_path)
        self._save_mapping = save_mapping
        self._export_to = export_to
        self._backup_existing = backup_existing
        self._cancel_event = threading.Event()
        self._events: queue.SimpleQueue[ExportProgress | ExportResult] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("导出任务正在运行")
        self._thread = threading.Thread(target=self._run, name="epub-export", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()

    def drain_events(self) -> list[ExportProgress | ExportResult]:
        events: list[ExportProgress | ExportResult] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def _publish_stage(self, stage: str, message: str) -> None:
        self._events.put(ExportProgress(stage, message))

    def _run(self) -> None:
        temporary_path: Path | None = None
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._temporary_output_path()
            if self._cancel_event.is_set():
                self._events.put(ExportResult(None, cancelled=True))
                return

            self._publish_stage("mapping", "正在保存 EPUB 映射…")
            self._save_mapping()
            if self._cancel_event.is_set():
                self._events.put(ExportResult(None, cancelled=True))
                return

            self._publish_stage("export", "正在重写 EPUB 内容…")
            produced_path = Path(self._export_to(temporary_path))
            if produced_path != temporary_path:
                raise RuntimeError("导出器未写入指定的临时输出文件")
            if self._cancel_event.is_set():
                self._events.put(ExportResult(None, cancelled=True))
                return

            self._publish_stage("verify", "正在验证导出文件…")
            self._verify_epub(temporary_path)
            if self._cancel_event.is_set():
                self._events.put(ExportResult(None, cancelled=True))
                return

            if self._backup_existing is not None and self._output_path.is_file():
                self._publish_stage("backup", "正在备份已有译文…")
                self._backup_existing(self._output_path)

            self._publish_stage("publish", "正在安全替换目标文件…")
            os.replace(temporary_path, self._output_path)
            temporary_path = None
            self._events.put(ExportResult(self._output_path))
        except Exception as exc:
            self._events.put(ExportResult(None, error_message=str(exc)))
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _temporary_output_path(self) -> Path:
        suffix = self._output_path.suffix or ".epub"
        return self._output_path.with_name(
            f".{self._output_path.stem}.{uuid.uuid4().hex}.tmp{suffix}"
        )

    @staticmethod
    def _verify_epub(path: Path) -> None:
        try:
            with ZipFile(path) as archive:
                if "mimetype" not in archive.namelist():
                    raise ValueError("导出文件缺少 EPUB mimetype 条目")
                if archive.testzip() is not None:
                    raise ValueError("导出文件包含损坏的 ZIP 条目")
        except BadZipFile as exc:
            raise ValueError("导出文件不是有效的 EPUB/ZIP") from exc


class TextExportJob:
    """Build and atomically publish a text export without touching Tk.

    The caller captures UI-owned source data before creating this job. Content
    construction, durable writing, and optional EPUB mapping persistence then
    run in the worker. A sibling temporary file keeps cancellation and failures
    from replacing an existing destination.
    """

    def __init__(
        self,
        *,
        output_path: Path,
        build_content: Callable[[], str],
        write_content: Callable[[Path, str], None],
        save_mapping: Callable[[], None] | None = None,
        backup_existing: Callable[[Path], None] | None = None,
    ) -> None:
        self._output_path = Path(output_path)
        self._build_content = build_content
        self._write_content = write_content
        self._save_mapping = save_mapping
        self._backup_existing = backup_existing
        self._cancel_event = threading.Event()
        self._events: queue.SimpleQueue[ExportProgress | ExportResult] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("Text export is already running")
        self._thread = threading.Thread(target=self._run, name="text-export", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()

    def drain_events(self) -> list[ExportProgress | ExportResult]:
        events: list[ExportProgress | ExportResult] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def _publish_stage(self, stage: str, message: str) -> None:
        self._events.put(ExportProgress(stage, message))

    def _run(self) -> None:
        temporary_path: Path | None = None
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._temporary_output_path()
            if self._cancel_event.is_set():
                self._events.put(ExportResult(None, cancelled=True))
                return

            self._publish_stage("build", "Building text export...")
            content = self._build_content()
            if self._cancel_event.is_set():
                self._events.put(ExportResult(None, cancelled=True))
                return

            self._publish_stage("write", "Writing text export...")
            self._write_content(temporary_path, content)
            if self._cancel_event.is_set():
                self._events.put(ExportResult(None, cancelled=True))
                return

            if self._save_mapping is not None:
                self._publish_stage("mapping", "Saving EPUB mapping...")
                self._save_mapping()
                if self._cancel_event.is_set():
                    self._events.put(ExportResult(None, cancelled=True))
                    return

            if self._backup_existing is not None and self._output_path.is_file():
                self._publish_stage("backup", "正在备份已有译文…")
                self._backup_existing(self._output_path)

            self._publish_stage("publish", "Publishing text export...")
            os.replace(temporary_path, self._output_path)
            temporary_path = None
            self._events.put(ExportResult(self._output_path))
        except Exception as exc:
            self._events.put(ExportResult(None, error_message=str(exc)))
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _temporary_output_path(self) -> Path:
        suffix = self._output_path.suffix or ".txt"
        return self._output_path.with_name(
            f".{self._output_path.stem}.{uuid.uuid4().hex}.tmp{suffix}"
        )
