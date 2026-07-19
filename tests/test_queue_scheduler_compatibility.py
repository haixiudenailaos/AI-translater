from __future__ import annotations

from src.core.queue_scheduler import QueueTaskState, QueueTranslationCoordinator, _TaskSlot


def test_get_all_task_data_does_not_reacquire_coordinator_lock():
    coordinator = QueueTranslationCoordinator.__new__(QueueTranslationCoordinator)
    coordinator._lock = __import__("threading").Lock()
    coordinator._task_order = ["first"]
    coordinator._tasks = {
        "first": _TaskSlot(
            task_id="first",
            file_path="source.txt",
            file_name="source.txt",
            file_type="txt",
            mapping_dir=None,
            source_lines=["source"],
            target_lines=["target"],
            state=QueueTaskState.READY,
        )
    }

    assert coordinator.get_all_task_data() == [
        {
            "task_id": "first",
            "file_path": "source.txt",
            "file_name": "source.txt",
            "file_type": "txt",
            "mapping_dir": None,
            "status": "ready",
            "progress": 0.0,
            "source_lines": ["source"],
            "target_lines": ["target"],
            "error_message": None,
            "failed_indices": [],
            "error_category": None,
            "error_safe_message": None,
            "recommended_action": None,
            "error_retryable": False,
            "correlation_id": None,
            "failed_count": 0,
        }
    ]
