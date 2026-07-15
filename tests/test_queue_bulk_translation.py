from src.core.concurrent_manager import ConcurrentTranslationManager
from src.core.translation_result import BatchTranslationResult, TranslationStatus


class QueueConfig:
    def get_app_config(self):
        return {}

    def get_api_config(self):
        return {}

    def get_glossary_prompt(self):
        return ""


class BulkOnlyEngine:
    def __init__(self):
        self.bulk_calls = []

    def translate_bulk_mode(self, content, progress_callback, complete_callback):
        self.bulk_calls.append(content)
        lines = [f"译:{line}" for line in content.splitlines()]
        progress_callback(100.0, {
            "batch_start": 0,
            "translated_lines": lines,
            "streaming": False,
        })
        complete_callback(BatchTranslationResult(
            status=TranslationStatus.SUCCEEDED,
            lines=lines,
        ))

    def translate_fast_mode(self, *_args, **_kwargs):
        raise AssertionError("queue translation must not use interactive mode")

    def stop(self):
        pass

    def close(self):
        pass


def test_queue_manager_uses_bulk_engine_mode(tmp_path, tmp_app_paths):
    source_path = tmp_path / "novel.txt"
    source_path.write_text("第一行\n第二行", encoding="utf-8")
    manager = ConcurrentTranslationManager(
        QueueConfig(), app_paths=tmp_app_paths
    )
    task = manager.add_task(str(source_path))
    engine = BulkOnlyEngine()
    task.status = "running"
    manager._engines[task.task_id] = engine

    try:
        manager._run_task(task.task_id)

        assert engine.bulk_calls == ["第一行\n第二行"]
        assert task.status == "completed"
        assert task.progress == 100.0
        assert task.target_lines == ["译:第一行", "译:第二行"]
    finally:
        manager.close()
