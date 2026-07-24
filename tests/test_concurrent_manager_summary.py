"""Regression tests for the lightweight bulk-task query API."""

import threading
from types import SimpleNamespace

from src.core.concurrent_manager import ConcurrentTranslationManager


class _SnapshotCoordinator:
    def __init__(self) -> None:
        self.task_data_calls = 0
        self._snapshot = SimpleNamespace(
            tasks=(
                SimpleNamespace(
                    task_id="task-1",
                    file_name="novel.txt",
                    file_type="txt",
                    status="completed",
                    failed_count=0,
                ),
            )
        )

    def get_snapshot(self):
        return self._snapshot

    def get_task_data(self, task_id: str):
        self.task_data_calls += 1
        raise AssertionError(f"snapshot path must not request full task data: {task_id}")


def test_bulk_task_summaries_use_snapshot_without_copying_text_lines():
    manager = object.__new__(ConcurrentTranslationManager)
    manager._meta_lock = threading.Lock()
    manager._task_order = ["task-1"]
    manager._task_meta = {
        "task-1": {
            "file_path": "C:/books/novel.txt",
            "mapping_dir": None,
            # These deliberately large fields must never be read or copied by
            # get_task_summaries().
            "source_lines": ["source"] * 100_000,
            "target_lines_backup": ["target"] * 100_000,
        }
    }
    coordinator = _SnapshotCoordinator()
    manager._coordinator = coordinator

    summaries = manager.get_task_summaries()

    assert len(summaries) == 1
    assert summaries[0].task_id == "task-1"
    assert summaries[0].file_path == "C:/books/novel.txt"
    assert summaries[0].status == "completed"
    assert coordinator.task_data_calls == 0
