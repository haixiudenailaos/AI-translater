from __future__ import annotations

import threading
import time
from pathlib import Path
from zipfile import ZipFile

from src.application.export_job import ExportJob, ExportResult, TextExportJob
from src.utils.file_handler import FileHandler


def _wait_for_result(job: ExportJob) -> ExportResult:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        for event in job.drain_events():
            if isinstance(event, ExportResult):
                return event
        time.sleep(0.01)
    raise AssertionError("export job did not finish")


def _write_epub(path: Path) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")
    return path


def test_export_job_atomically_replaces_verified_output(tmp_path):
    output = tmp_path / "book.epub"
    output.write_bytes(b"old-output")
    saved = []

    job = ExportJob(
        output_path=output,
        save_mapping=lambda: saved.append(True),
        export_to=_write_epub,
    )
    job.start()

    result = _wait_for_result(job)

    assert result.succeeded
    assert saved == [True]
    with ZipFile(output) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"
    assert not list(tmp_path.glob(".*.tmp.epub"))


def test_export_job_keeps_existing_output_when_export_fails(tmp_path):
    output = tmp_path / "book.epub"
    output.write_bytes(b"old-output")

    def fail_export(_path: Path) -> Path:
        raise OSError("disk full")

    job = ExportJob(output_path=output, save_mapping=lambda: None, export_to=fail_export)
    job.start()

    result = _wait_for_result(job)

    assert not result.succeeded
    assert result.error_message == "disk full"
    assert output.read_bytes() == b"old-output"
    assert not list(tmp_path.glob(".*.tmp.epub"))


def test_export_job_cancelled_before_publish_preserves_existing_output(tmp_path):
    output = tmp_path / "book.epub"
    output.write_bytes(b"old-output")
    job: ExportJob

    def export_and_cancel(path: Path) -> Path:
        _write_epub(path)
        job.cancel()
        return path

    job = ExportJob(output_path=output, save_mapping=lambda: None, export_to=export_and_cancel)
    job.start()

    result = _wait_for_result(job)

    assert result.cancelled
    assert output.read_bytes() == b"old-output"
    assert not list(tmp_path.glob(".*.tmp.epub"))


def test_text_export_builds_and_publishes_from_a_worker(tmp_path):
    output = tmp_path / "translation.txt"
    output.write_text("old-output", encoding="utf-8")
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []
    mapping_saved: list[bool] = []
    handler = FileHandler()

    def build_content() -> str:
        worker_threads.append(threading.get_ident())
        return "line one\nline two"

    def write_content(path: Path, content: str) -> None:
        worker_threads.append(threading.get_ident())
        handler.write_file(str(path), content)

    job = TextExportJob(
        output_path=output,
        build_content=build_content,
        write_content=write_content,
        save_mapping=lambda: mapping_saved.append(True),
    )
    job.start()

    result = _wait_for_result(job)

    assert result.succeeded
    assert output.read_text(encoding="utf-8") == "line one\nline two"
    assert mapping_saved == [True]
    assert worker_threads and all(thread_id != caller_thread for thread_id in worker_threads)
    assert not list(tmp_path.glob(".*.tmp.txt"))


def test_text_export_cancellation_before_write_preserves_existing_output(tmp_path):
    output = tmp_path / "translation.txt"
    output.write_text("old-output", encoding="utf-8")
    writes: list[Path] = []
    job: TextExportJob

    def build_content() -> str:
        job.cancel()
        return "new-output"

    def write_content(path: Path, _content: str) -> None:
        writes.append(path)

    job = TextExportJob(
        output_path=output,
        build_content=build_content,
        write_content=write_content,
    )
    job.start()

    result = _wait_for_result(job)

    assert result.cancelled
    assert output.read_text(encoding="utf-8") == "old-output"
    assert writes == []
    assert not list(tmp_path.glob(".*.tmp.txt"))
