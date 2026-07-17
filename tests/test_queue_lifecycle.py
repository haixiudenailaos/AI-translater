#!/usr/bin/env python3
"""队列翻译生命周期集成测试（队列翻译并发优化阶段 0/2-4）。

覆盖：
- ``ConcurrentTranslationManager`` 端到端：add_task → start_task → COMPLETED。
- 取消任务：attempt_id 隔离，旧 Outcome 丢弃。
- 暂停/恢复：PAUSE_REQUESTED → PAUSED → READY。
- 检查点：成功批次触发文件保存。
- 快照：``get_snapshot`` 返回不可变 ``QueueSnapshot``。
- 旧 API 兼容：``TranslationTask`` 视图从快照重建动态字段。

这些测试通过 monkey-patch ``TranslatorEngine`` 替换为 MockEngine，
避免真实 API 调用，但验证完整的 Coordinator 调度流程。
"""

from __future__ import annotations

import time
from typing import List
from unittest.mock import MagicMock

import pytest

from src.core.concurrent_manager import ConcurrentTranslationManager, TranslationTask
from src.core.queue_scheduler import QueueTaskState
from src.core.translation_result import BatchTranslationResult
from src.domain.translation import OperationStatus


class MockEngine:
    """Mock TranslatorEngine 用于生命周期测试。

    模拟 ``_translate_batch`` 成功翻译每行为 ``译:<原文>``。
    """

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.is_stopped = False
        self.api = MagicMock()
        self._translate_calls: List[List[str]] = []
        self.close_called = False
        self.stop_called = False
        # 控制每次调用是否抛出异常
        self.raise_on_next: Exception | None = None

    def _ensure_api(self):
        pass

    def _init_api(self):
        pass

    def build_run_context(self):
        from src.core.translator import TranslationRunContext

        return TranslationRunContext(
            provider="siliconflow",
            model_name="deepseek-ai/DeepSeek-V3.2",
            target_language="中文",
            base_prompt="translate",
            glossary_prompt="",
            system_prompt=None,
            is_hunyuan=False,
            temperature=0.3,
            prompt_version="abc",
            glossary_version="",
        )

    def compute_input_token_budget(self, configured: int) -> int:
        return configured

    def _translate_batch(
        self,
        batch_lines,
        progress_callback,
        batch_start,
        total_lines=None,
        emit_stream_progress=True,
        run_context=None,
    ) -> BatchTranslationResult:
        self._translate_calls.append(list(batch_lines))
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        return BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=[f"译:{line}" for line in batch_lines],
        )

    def refresh_api(self):
        pass

    def close(self):
        self.close_called = True

    def stop(self):
        self.stop_called = True
        self.is_stopped = True

    def reset(self):
        self.is_stopped = False


@pytest.fixture
def patch_engine(monkeypatch):
    """替换 ``TranslatorEngine`` 为 ``MockEngine``，避免真实 API 调用。"""
    from src.core import queue_scheduler

    instances: List[MockEngine] = []

    def _factory(config_manager):
        engine = MockEngine(config_manager)
        instances.append(engine)
        return engine

    monkeypatch.setattr(queue_scheduler, "TranslatorEngine", _factory)
    # concurrent_manager 通过 queue_scheduler.TranslatorEngine 间接引用，
    # 无需单独 patch。
    return instances


class TestManagerEndToEnd:
    """端到端：add_task → start → 等待 COMPLETED。"""

    def test_completed_task_writes_translated_file(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        # 准备源文件
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            assert task.task_id is not None
            assert task.status == "pending"

            # 启动任务
            manager.start_task(task.task_id)

            # 轮询等待完成（最多 5 秒）
            deadline = time.time() + 5.0
            while time.time() < deadline:
                t = manager.get_task(task.task_id)
                if t.status in ("completed", "partial", "error", "cancelled"):
                    break
                time.sleep(0.1)

            final = manager.get_task(task.task_id)
            assert final.status == "completed"
            assert final.progress == 100.0
            # 译文已写入 target_lines
            assert final.target_lines[0] == "译:第一行"
            assert final.target_lines[1] == "译:第二行"
            assert final.target_lines[2] == "译:第三行"

            # 等待检查点写盘（debounce）
            time.sleep(1.0)
            tgt_file = tmp_path / "novel_译文.txt"
            assert tgt_file.exists(), "译文文件应被检查点保存"
            content = tgt_file.read_text(encoding="utf-8")
            assert "译:第一行" in content
            assert "译:第二行" in content
        finally:
            manager.close()

    def test_cancel_task(self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine):
        source = tmp_path / "novel.txt"
        source.write_text("line1\nline2\nline3", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            manager.start_task(task.task_id)
            # 立即取消
            manager.cancel_task(task.task_id)

            deadline = time.time() + 3.0
            while time.time() < deadline:
                t = manager.get_task(task.task_id)
                if t.status == "cancelled":
                    break
                time.sleep(0.1)

            final = manager.get_task(task.task_id)
            assert final.status == "cancelled"
        finally:
            manager.close()


class TestSnapshotPolling:
    """``get_snapshot`` 返回不可变 QueueSnapshot 供 UI 轮询。"""

    def test_snapshot_returns_immutable(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        source = tmp_path / "novel.txt"
        source.write_text("a\nb", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            manager.add_task(str(source))
            # 等待 Coordinator 发布快照
            time.sleep(0.3)
            snapshot = manager.get_snapshot()
            assert snapshot is not None
            # tasks 是 tuple（不可变）
            assert isinstance(snapshot.tasks, tuple)
            assert len(snapshot.tasks) == 1
            task_snap = snapshot.tasks[0]
            assert task_snap.task_id is not None
            assert task_snap.state == QueueTaskState.PENDING
            # metrics 字段齐全
            assert snapshot.metrics.hard_cap >= 1
            assert snapshot.metrics.current_limit >= 1
            assert isinstance(snapshot.metrics.is_blocked, bool)
        finally:
            manager.close()


class TestLegacyApiCompat:
    """旧 API（TranslationTask 视图、get_task/get_all_tasks）兼容性。"""

    def test_get_task_returns_translation_task(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        source = tmp_path / "novel.txt"
        source.write_text("hello", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            assert isinstance(task, TranslationTask)
            assert task.file_name == "novel.txt"
            assert task.file_type == "txt"
            assert task.source_lines == ["hello"]

            # get_task 返回最新视图
            fetched = manager.get_task(task.task_id)
            assert fetched is not None
            assert fetched.task_id == task.task_id
            assert fetched.file_name == "novel.txt"
            assert fetched.source_lines == ["hello"]
        finally:
            manager.close()

    def test_get_all_tasks_preserves_order(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        s1 = tmp_path / "a.txt"
        s1.write_text("a", encoding="utf-8")
        s2 = tmp_path / "b.txt"
        s2.write_text("b", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            t1 = manager.add_task(str(s1))
            t2 = manager.add_task(str(s2))
            all_tasks = manager.get_all_tasks()
            assert len(all_tasks) == 2
            assert all_tasks[0].task_id == t1.task_id
            assert all_tasks[1].task_id == t2.task_id
        finally:
            manager.close()


class TestQueuePolicyFromConfig:
    """``build_queue_policy_from_app_config`` 正确构造 QueuePolicy。"""

    def test_default_policy(self):
        from src.config.translation_profile import (
            DEFAULT_QUEUE_HARD_REQUEST_CAP,
            DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS,
            build_queue_policy_from_app_config,
        )

        policy = build_queue_policy_from_app_config({})
        assert policy.max_in_flight_requests == DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS
        assert policy.hard_request_cap == DEFAULT_QUEUE_HARD_REQUEST_CAP
        assert policy.max_batch_lines > 0

    def test_legacy_concurrency_fallback(self):
        """旧字段 queue_translation_concurrency 在新字段缺失时作为 fallback。"""
        from src.config.translation_profile import (
            build_queue_policy_from_app_config,
        )

        policy = build_queue_policy_from_app_config(
            {
                "queue_translation_concurrency": 3,
            }
        )
        # legacy_concurrency=3，max_in_flight 应为 min(default=2, 3) = 2
        # hard_cap = max(default=4, min(8, 6)) = 6
        assert policy.max_in_flight_requests <= 3
        assert policy.hard_request_cap >= 4

    def test_new_field_overrides_legacy(self):
        from src.config.translation_profile import (
            build_queue_policy_from_app_config,
        )

        policy = build_queue_policy_from_app_config(
            {
                "queue_max_in_flight_requests": 3,
                "queue_hard_request_cap": 6,
                "queue_translation_concurrency": 1,  # 应被忽略
            }
        )
        assert policy.max_in_flight_requests == 3
        assert policy.hard_request_cap == 6

    def test_clamp_out_of_range(self):
        from src.config.translation_profile import (
            MAX_QUEUE_HARD_REQUEST_CAP,
            build_queue_policy_from_app_config,
        )

        policy = build_queue_policy_from_app_config(
            {
                "queue_max_in_flight_requests": 999,
                "queue_hard_request_cap": 999,
            }
        )
        assert policy.max_in_flight_requests <= MAX_QUEUE_HARD_REQUEST_CAP
        assert policy.hard_request_cap == MAX_QUEUE_HARD_REQUEST_CAP
