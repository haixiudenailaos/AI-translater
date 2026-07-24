#!/usr/bin/env python3
"""队列调度器单元测试（队列翻译并发优化阶段 2）。

覆盖：
- ``plan_batches``：纯函数批次规划，按 token 预算和 max_batch_lines 拆分。
- ``execute_batch_job``：Worker 调用引擎 _translate_batch，正确上报结果给 limiter。
- ``QueueTranslationCoordinator``：状态机、attempt_id 隔离、round-robin 调度。
- 快照发布：UI 轮询读取不可变 ``QueueSnapshot``。

这些测试不依赖真实 API——使用 mock engine 和 mock limiter 隔离网络调用。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import List
from unittest.mock import MagicMock

from src.core.queue_provider import (
    ProviderLimiter,
    ProviderLimiterRegistry,
    ProviderRuntimeKey,
)
from src.core.queue_scheduler import (
    STATE_DISPLAY_MAP,
    BatchJob,
    QueuePolicy,
    QueueTaskState,
    QueueTranslationCoordinator,
    _InFlightBatch,
    execute_batch_job,
    plan_batches,
)
from src.core.translation_result import BatchTranslationResult
from src.core.translator import TranslationRunContext
from src.domain.errors import TranslationCancelled, TranslationRequestError
from src.domain.translation import OperationStatus

# ── 测试夹具 ──────────────────────────────────────────────


def _make_run_context() -> TranslationRunContext:
    return TranslationRunContext(
        provider="siliconflow",
        model_name="deepseek-ai/DeepSeek-V3.2",
        target_language="中文",
        base_prompt="翻译以下内容",
        glossary_prompt="",
        system_prompt=None,
        is_hunyuan=False,
        temperature=0.3,
        prompt_version="abc",
        glossary_version="",
    )


def _make_policy(**overrides) -> QueuePolicy:
    defaults = dict(
        max_in_flight_requests=2,
        hard_request_cap=4,
        max_active_tasks=4,
        per_task_soft_limit=1,
        target_batch_input_tokens=2000,
        max_batch_input_tokens=4000,
        max_batch_lines=10,
        min_batch_input_tokens=512,
        adaptive_concurrency=True,
        rpm_limit=0,
        tpm_limit=0,
    )
    defaults.update(overrides)
    return QueuePolicy(**defaults)


def _make_limiter(*, configured_max: int = 4, hard_cap: int = 8) -> ProviderLimiter:
    key = ProviderRuntimeKey(
        provider="siliconflow",
        normalized_base_url="https://api.siliconflow.cn/v1",
        model_name="deepseek-ai/DeepSeek-V3.2",
        credential_reference="abc12345",
        config_version="0.3:4000",
    )
    return ProviderLimiter(
        key,
        configured_max=configured_max,
        hard_cap=hard_cap,
    )


class MockEngine:
    """Mock TranslatorEngine，模拟 _translate_batch 行为。

    通过设置 ``result_factory`` 或 ``raise_exception`` 控制每次调用的返回/抛出。
    """

    def __init__(self):
        self.is_stopped = False
        self.api = MagicMock()
        self.config_manager = MagicMock()
        self._translate_calls: List[List[str]] = []
        self.result_factory = None  # callable(batch_lines) -> BatchTranslationResult
        self.raise_exception: Exception | None = None
        self.close_called = False
        self.stop_called = False

    def _ensure_api(self):
        pass

    def build_run_context(self) -> TranslationRunContext:
        return _make_run_context()

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
        if self.raise_exception is not None:
            raise self.raise_exception
        if self.result_factory is not None:
            return self.result_factory(batch_lines)
        # 默认：每行加前缀 "译:"
        return BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=[f"译:{line}" for line in batch_lines],
        )

    def close(self):
        self.close_called = True

    def stop(self):
        self.stop_called = True
        self.is_stopped = True


# ── plan_batches ─────────────────────────────────────────


class TestPlanBatches:
    def test_empty_missing_indices_returns_empty(self):
        ctx = _make_run_context()
        policy = _make_policy()
        jobs = plan_batches([], [], policy, ctx, 2000)
        assert jobs == []

    def test_single_batch_under_budget(self):
        ctx = _make_run_context()
        policy = _make_policy(max_batch_lines=10)
        # 5 行小内容，应放入一个批次
        lines = ["line" + str(i) for i in range(5)]
        jobs = plan_batches(lines, [0, 1, 2, 3, 4], policy, ctx, 2000)
        assert len(jobs) == 1
        assert jobs[0].source_indices == (0, 1, 2, 3, 4)
        assert jobs[0].source_lines == tuple(lines)

    def test_split_by_max_batch_lines(self):
        ctx = _make_run_context()
        policy = _make_policy(max_batch_lines=3)
        lines = [f"short{i}" for i in range(7)]
        jobs = plan_batches(lines, list(range(7)), policy, ctx, 100000)
        # 7 行 / max 3 = 3 批（3+3+1）
        assert len(jobs) == 3
        assert len(jobs[0].source_indices) == 3
        assert len(jobs[1].source_indices) == 3
        assert len(jobs[2].source_indices) == 1

    def test_split_by_token_budget(self):
        ctx = _make_run_context()
        # 极小 token 预算，强制每行一个批次
        policy = _make_policy(max_batch_lines=100)
        lines = ["a" * 100 for _ in range(5)]
        jobs = plan_batches(lines, [0, 1, 2, 3, 4], policy, ctx, 50)
        # 应被 token 预算切分为多批
        assert len(jobs) > 1

    def test_batch_ids_increment(self):
        ctx = _make_run_context()
        policy = _make_policy(max_batch_lines=2)
        lines = ["a", "b", "c", "d"]
        jobs = plan_batches(lines, [0, 1, 2, 3], policy, ctx, 10000, start_batch_id=10)
        assert [j.batch_id for j in jobs] == [10, 11]

    def test_missing_indices_subset(self):
        """只翻译缺失行（target_lines 为空的行）。"""
        ctx = _make_run_context()
        policy = _make_policy(max_batch_lines=10)
        lines = ["a", "b", "c", "d", "e"]
        # 只翻译行 1 和 3（已部分翻译）
        jobs = plan_batches(lines, [1, 3], policy, ctx, 10000)
        assert len(jobs) == 1
        assert jobs[0].source_indices == (1, 3)
        assert jobs[0].source_lines == ("b", "d")


# ── execute_batch_job ────────────────────────────────────


class TestExecuteBatchJob:
    def test_success_returns_outcome_with_translated_lines(self):
        ctx = _make_run_context()
        job = BatchJob(
            task_id="t1",
            attempt_id="a1",
            batch_id=0,
            source_indices=(0, 1),
            source_lines=("hello", "world"),
            estimated_input_tokens=100,
            run_context=ctx,
        )
        engine = MockEngine()
        limiter = _make_limiter()
        cancel_event = threading.Event()

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.task_id == "t1"
        assert outcome.attempt_id == "a1"
        assert outcome.batch_id == 0
        assert outcome.translated_lines == ("译:hello", "译:world")
        assert outcome.failed_relative_indices == ()
        assert outcome.cancelled is False
        assert outcome.error_message is None
        # limiter 记录一次成功
        assert limiter.metrics()["total_success"] == 1

    def test_cancelled_before_start(self):
        ctx = _make_run_context()
        job = BatchJob(
            task_id="t1",
            attempt_id="a1",
            batch_id=0,
            source_indices=(0,),
            source_lines=("hello",),
            estimated_input_tokens=100,
            run_context=ctx,
        )
        engine = MockEngine()
        limiter = _make_limiter()
        cancel_event = threading.Event()
        cancel_event.set()  # 已取消

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.cancelled is True
        assert outcome.translated_lines == ()
        assert engine._translate_calls == []  # 未调用引擎

    def test_translation_cancelled_exception(self):
        ctx = _make_run_context()
        job = BatchJob(
            task_id="t1",
            attempt_id="a1",
            batch_id=0,
            source_indices=(0,),
            source_lines=("hello",),
            estimated_input_tokens=100,
            run_context=ctx,
        )
        engine = MockEngine()
        engine.raise_exception = TranslationCancelled()
        limiter = _make_limiter()
        cancel_event = threading.Event()

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.cancelled is True
        assert outcome.translated_lines == ()

    def test_429_reports_rate_limited(self):
        ctx = _make_run_context()
        job = BatchJob(
            task_id="t1",
            attempt_id="a1",
            batch_id=0,
            source_indices=(0,),
            source_lines=("hello",),
            estimated_input_tokens=100,
            run_context=ctx,
        )
        engine = MockEngine()
        engine.raise_exception = TranslationRequestError(
            "rate limited", status_code=429, retry_after_seconds=5.0
        )
        limiter = _make_limiter()
        cancel_event = threading.Event()

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.rate_limited is True
        assert outcome.retry_after_seconds == 5.0
        # limiter 记录一次 429
        assert limiter.metrics()["total_429"] == 1
        assert limiter.current_limit == 2  # 4 // 2

    def test_unexpected_exception_recorded(self):
        ctx = _make_run_context()
        job = BatchJob(
            task_id="t1",
            attempt_id="a1",
            batch_id=0,
            source_indices=(0,),
            source_lines=("hello",),
            estimated_input_tokens=100,
            run_context=ctx,
        )
        engine = MockEngine()
        engine.raise_exception = RuntimeError("unexpected")
        limiter = _make_limiter()
        cancel_event = threading.Event()

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.cancelled is False
        assert "unexpected" in (outcome.error_message or "")
        assert outcome.translated_lines == ()


# ── QueueTranslationCoordinator ─────────────────────────


class _MockFileHandler:
    def write_file(self, path, content):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")

    def read_file(self, path):
        return Path(path).read_text(encoding="utf-8")


class _MockEpubProcessor:
    def save_translations(self, mapping_dir, lines):
        pass


from pathlib import Path  # noqa: E402


class TestCoordinatorLifecycle:
    """Coordinator 状态机和生命周期测试。

    使用 MockEngine 替换 TranslatorEngine，避免真实 API 调用。
    """

    def _make_coordinator(
        self,
        config_manager,
        policy=None,
    ) -> QueueTranslationCoordinator:
        policy = policy or _make_policy()
        return QueueTranslationCoordinator(
            config_manager,
            policy,
            file_handler=_MockFileHandler(),
            epub_processor=_MockEpubProcessor(),
        )

    def test_add_task_and_remove(self, tmp_config_manager):
        """add_task 注册任务，remove_task 清理。"""
        coord = self._make_coordinator(tmp_config_manager)
        coord.start()
        try:
            ok = coord.add_task(
                task_id="t1",
                file_path="/tmp/test.txt",
                file_name="test.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=["a", "b"],
                target_lines=["", ""],
            )
            assert ok is True
            # 重复添加同 task_id 失败
            ok2 = coord.add_task(
                task_id="t1",
                file_path="/tmp/test.txt",
                file_name="test.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=["a", "b"],
                target_lines=["", ""],
            )
            assert ok2 is False
            # 移除
            ok3 = coord.remove_task("t1")
            assert ok3 is True
        finally:
            coord.close()

    def test_add_task_same_path_rejected(self, tmp_config_manager):
        """同路径独占写锁：第二个任务添加失败。"""
        coord = self._make_coordinator(tmp_config_manager)
        coord.start()
        try:
            ok1 = coord.add_task(
                task_id="t1",
                file_path="/tmp/novel.txt",
                file_name="novel.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=["a"],
                target_lines=[""],
            )
            assert ok1 is True
            # 同路径不同 task_id 应失败（独占写锁）
            ok2 = coord.add_task(
                task_id="t2",
                file_path="/tmp/novel.txt",
                file_name="novel.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=["a"],
                target_lines=[""],
            )
            assert ok2 is False
        finally:
            coord.close()

    def test_snapshot_published_after_add(self, tmp_config_manager):
        """add_task 后 Coordinator 发布快照，UI 可读取。"""
        coord = self._make_coordinator(tmp_config_manager)
        coord.start()
        try:
            coord.add_task(
                task_id="t1",
                file_path="/tmp/test.txt",
                file_name="test.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=["a"],
                target_lines=[""],
            )
            # 等待 Coordinator 发布快照
            time.sleep(0.2)
            snapshot = coord.get_snapshot()
            assert snapshot is not None
            assert len(snapshot.tasks) == 1
            assert snapshot.tasks[0].task_id == "t1"
            assert snapshot.tasks[0].state == QueueTaskState.PENDING
            assert snapshot.tasks[0].file_name == "test.txt"
        finally:
            coord.close()

    def test_close_is_idempotent(self, tmp_config_manager):
        coord = self._make_coordinator(tmp_config_manager)
        coord.close()
        coord.close()  # 不抛异常

    def test_close_does_not_clear_application_limiter_registry(self, tmp_config_manager):
        registry = ProviderLimiterRegistry()
        key = ProviderRuntimeKey(
            provider="test",
            normalized_base_url="https://example.invalid/v1",
            model_name="test-model",
            credential_reference="credential",
            config_version="0.3:1000",
        )
        limiter = registry.get_or_create(key, configured_max=2, hard_cap=2)
        coord = QueueTranslationCoordinator(
            tmp_config_manager,
            _make_policy(max_in_flight_requests=2, hard_request_cap=2),
            file_handler=_MockFileHandler(),
            epub_processor=_MockEpubProcessor(),
            limiter_registry=registry,
        )

        coord.close()

        assert registry.get_or_create(key, configured_max=2, hard_cap=2) is limiter

    def test_task_limiter_and_future_release_remain_stable_after_provider_switch(
        self, tmp_config_manager
    ):
        """任务和 Future 必须释放派发时所属的 limiter，而不是当前全局值。"""
        coord = self._make_coordinator(tmp_config_manager, _make_policy(hard_request_cap=4))
        limiter_a = _make_limiter(configured_max=3, hard_cap=4)
        limiter_b = ProviderLimiter(
            ProviderRuntimeKey(
                provider="deepseek",
                normalized_base_url="https://api.deepseek.com/v1",
                model_name="deepseek-chat",
                credential_reference="different-account",
                config_version="0.3:4000",
            ),
            configured_max=2,
            hard_cap=4,
        )
        try:
            assert coord.add_task(
                "provider-a",
                "/tmp/provider-a.txt",
                "provider-a.txt",
                "txt",
                None,
                ["one"],
                [""],
            )
            assert coord.add_task(
                "provider-b",
                "/tmp/provider-b.txt",
                "provider-b.txt",
                "txt",
                None,
                ["two"],
                [""],
            )

            with coord._lock:
                for task_id, limiter, source in (
                    ("provider-a", limiter_a, "one"),
                    ("provider-b", limiter_b, "two"),
                ):
                    slot = coord._tasks[task_id]
                    slot.attempt_id = f"attempt-{task_id}"
                    slot.state = QueueTaskState.READY
                    slot.engine = MockEngine()
                    slot.limiter = limiter
                    slot.pending_batches.append(
                        BatchJob(
                            task_id=task_id,
                            attempt_id=slot.attempt_id,
                            batch_id=0,
                            source_indices=(0,),
                            source_lines=(source,),
                            estimated_input_tokens=1,
                            run_context=_make_run_context(),
                        )
                    )
                # 模拟用户在第一项开始后切换 Provider。新任务会成为当前
                # UI limiter，但旧任务必须继续使用自己的 limiter。
                coord._active_limiter = limiter_b

            coord._dispatch()
            submitted = tuple(coord._in_flight)
            assert len(submitted) == 2
            assert {meta.limiter for meta in coord._in_flight.values()} == {
                limiter_a,
                limiter_b,
            }
            for future in submitted:
                future.result(timeout=2.0)

            # 两个额外的非成功路径确保收割逻辑同样释放原始 limiter。
            assert limiter_a.try_acquire()
            failed = Future()
            failed.set_exception(RuntimeError("provider worker failed"))
            assert limiter_a.try_acquire()
            cancelled = Future()
            assert cancelled.cancel()
            # Bare Future.cancel() stays in CANCELLED until an executor worker
            # observes it. Mirror the executor transition so wait() can harvest
            # this cancelled job in the same coordinator iteration.
            assert cancelled.set_running_or_notify_cancel() is False
            coord._in_flight[failed] = _InFlightBatch(
                task_id="provider-a-error",
                batch_id=1,
                limiter=limiter_a,
            )
            coord._in_flight[cancelled] = _InFlightBatch(
                task_id="provider-a-cancelled",
                batch_id=2,
                limiter=limiter_a,
            )

            coord._drain_completed_outcomes()
            assert coord._in_flight == {}
            assert limiter_a.metrics()["in_flight"] == 0
            assert limiter_b.metrics()["in_flight"] == 0
        finally:
            coord.close()

    def test_executor_submit_failure_releases_limiter_slot(self, tmp_config_manager):
        """Executor 拒绝任务时不能留下 limiter 槽位或半派发批次。"""

        class _RejectingExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("executor is shut down")

            def shutdown(self, **_kwargs):
                return None

        coord = self._make_coordinator(tmp_config_manager)
        original_executor = coord._executor
        original_executor.shutdown(wait=False, cancel_futures=True)
        coord._executor = _RejectingExecutor()
        limiter = _make_limiter(configured_max=1, hard_cap=1)
        try:
            assert coord.add_task(
                "submit-error",
                "/tmp/submit-error.txt",
                "submit-error.txt",
                "txt",
                None,
                ["one"],
                [""],
            )
            with coord._lock:
                slot = coord._tasks["submit-error"]
                slot.attempt_id = "submit-attempt"
                slot.state = QueueTaskState.READY
                slot.engine = MockEngine()
                slot.limiter = limiter
                slot.pending_batches.append(
                    BatchJob(
                        task_id=slot.task_id,
                        attempt_id=slot.attempt_id,
                        batch_id=0,
                        source_indices=(0,),
                        source_lines=("one",),
                        estimated_input_tokens=1,
                        run_context=_make_run_context(),
                    )
                )
                assert not coord._try_dispatch_one_locked(slot, limiter)

            assert coord._in_flight == {}
            assert slot.in_flight_batches == {}
            assert not slot.pending_batches
            assert slot.state == QueueTaskState.ERROR
            assert limiter.metrics()["in_flight"] == 0
        finally:
            coord.close()

    def test_close_releases_engines(self, tmp_config_manager):
        coord = self._make_coordinator(tmp_config_manager)
        coord.start()
        try:
            coord.add_task(
                task_id="t1",
                file_path="/tmp/test.txt",
                file_name="test.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=["a"],
                target_lines=[""],
            )
        finally:
            coord.close()


class TestCoordinatorStateTransitions:
    """状态机：PENDING -> READY -> RUNNING -> COMPLETED/CANCELLED/ERROR。"""

    def test_engine_preparation_releases_global_state_lock(self, tmp_config_manager, monkeypatch):
        """Provider initialization must not block queue commands behind ``_lock``."""
        entered = threading.Event()
        release = threading.Event()
        lock_was_available: list[bool] = []

        class _BlockingInitEngine(MockEngine):
            def _ensure_api(self):
                acquired = coordinator._lock.acquire(blocking=False)
                lock_was_available.append(acquired)
                if acquired:
                    coordinator._lock.release()
                entered.set()
                assert release.wait(3.0)

        monkeypatch.setattr(
            "src.core.queue_scheduler.TranslatorEngine", lambda _config: _BlockingInitEngine()
        )
        coordinator = QueueTranslationCoordinator(
            tmp_config_manager,
            _make_policy(),
            file_handler=_MockFileHandler(),
            epub_processor=_MockEpubProcessor(),
        )
        coordinator.start()
        try:
            assert coordinator.add_task(
                "prepare-lock",
                "/tmp/prepare-lock.txt",
                "prepare-lock.txt",
                "txt",
                None,
                ["line"],
                [""],
            )
            coordinator.submit_command("start", "prepare-lock")
            assert entered.wait(3.0)
            assert lock_was_available == [True]

            # This call would block if _ensure_api still held the coordinator lock.
            coordinator.submit_command("cancel", "prepare-lock")
            release.set()

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                task = coordinator.get_task_data("prepare-lock")
                if task is not None and task["status"] == "cancelled":
                    break
                time.sleep(0.01)
            assert coordinator.get_task_data("prepare-lock")["status"] == "cancelled"
        finally:
            release.set()
            coordinator.close()

    def test_cancel_bumps_attempt_id(self, tmp_config_manager):
        """取消后 attempt_id 改变，旧 Outcome 被丢弃（§9.3 隔离语义）。

        使用阻塞型 MockEngine：``_translate_batch`` 在 release_event 被设置
        前不返回，确保任务处于 RUNNING 时取消命令能被处理。
        """
        from unittest.mock import patch

        release_event = threading.Event()

        class _BlockingEngine(MockEngine):
            def _translate_batch(
                self,
                batch_lines,
                progress_callback,
                batch_start,
                total_lines=None,
                emit_stream_progress=True,
                run_context=None,
            ):
                # 阻塞直到外部释放或取消
                release_event.wait(timeout=5.0)
                if self.is_stopped:
                    raise TranslationCancelled("已取消")
                return BatchTranslationResult(
                    status=OperationStatus.SUCCEEDED,
                    lines=[f"译:{line}" for line in batch_lines],
                )

        def _engine_factory(config_manager):
            return _BlockingEngine()

        coord = QueueTranslationCoordinator(
            tmp_config_manager,
            _make_policy(),
            file_handler=_MockFileHandler(),
            epub_processor=_MockEpubProcessor(),
        )
        coord.start()
        try:
            coord.add_task(
                task_id="t1",
                file_path="/tmp/test.txt",
                file_name="test.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=["a", "b"],
                target_lines=["", ""],
            )
            with patch("src.core.queue_scheduler.TranslatorEngine", _engine_factory):
                coord.submit_command("start", "t1")
                # 等待任务进入 RUNNING（批次已派发）
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    snapshot = coord.get_snapshot()
                    if snapshot and snapshot.tasks and snapshot.tasks[0].in_flight_batches > 0:
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("任务未进入 RUNNING（无在途批次）")

                first_attempt_id = coord._tasks["t1"].attempt_id
                assert first_attempt_id, "attempt_id 不应为空"

                # 取消任务：应 bump attempt_id
                coord.submit_command("cancel", "t1")
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    snapshot = coord.get_snapshot()
                    if (
                        snapshot
                        and snapshot.tasks
                        and snapshot.tasks[0].state == QueueTaskState.CANCELLED
                    ):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("任务未进入 CANCELLED")

                second_attempt_id = coord._tasks["t1"].attempt_id
                # 取消必须 bump attempt_id（§9.3）
                assert second_attempt_id != first_attempt_id, "取消后 attempt_id 必须改变"
        finally:
            # 释放阻塞的工作线程，避免 close 超时
            release_event.set()
            coord.close()


class TestStateDisplayMap:
    """STATE_DISPLAY_MAP 覆盖所有 QueueTaskState。"""

    def test_all_states_have_display(self):
        for state in QueueTaskState:
            assert state in STATE_DISPLAY_MAP
            assert isinstance(STATE_DISPLAY_MAP[state], str)
            assert STATE_DISPLAY_MAP[state]


# ── Batch-D: on_retryable_response hook + retry_count tracking ───────────────


class TestExecuteBatchJobBatchD:
    """Batch-D: hook on_retryable_response + retry_count dans execute_batch_job."""

    def _make_job(self) -> BatchJob:
        return BatchJob(
            task_id="t1",
            attempt_id="a1",
            batch_id=0,
            source_indices=(0,),
            source_lines=("hello",),
            estimated_input_tokens=100,
            run_context=_make_run_context(),
        )

    def test_hook_installed_and_cleared_on_success(self):
        """Le hook est installé avant _translate_batch et retiré après succès."""
        job = self._make_job()
        engine = MockEngine()
        limiter = _make_limiter()
        cancel_event = threading.Event()

        hook_during_call = []

        def capturing_translate_batch(*args, **kwargs):
            # Capture l'état du hook au moment de l'appel
            hook_during_call.append(engine.api.set_on_retryable_response.call_count)
            return BatchTranslationResult(
                status=OperationStatus.SUCCEEDED,
                lines=["译:hello"],
            )

        engine._translate_batch = capturing_translate_batch
        engine.api.set_on_retryable_response = MagicMock()

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        # Hook installé avant l'appel (set avec un callable)
        assert engine.api.set_on_retryable_response.call_count == 2
        first_arg = engine.api.set_on_retryable_response.call_args_list[0][0][0]
        assert callable(first_arg)
        # Puis réinitialisé à None dans le finally
        last_arg = engine.api.set_on_retryable_response.call_args_list[1][0][0]
        assert last_arg is None

        assert outcome.retry_count == 0
        assert not outcome.cancelled

    def test_hook_cleared_on_exception(self):
        """Le hook est toujours retiré même quand _translate_batch lève une exception."""
        job = self._make_job()
        engine = MockEngine()
        engine.raise_exception = TranslationRequestError("fail", status_code=500)
        engine.api.set_on_retryable_response = MagicMock()
        limiter = _make_limiter()
        cancel_event = threading.Event()

        execute_batch_job(job, engine, cancel_event, limiter)

        # finally doit avoir appelé set_on_retryable_response(None)
        last_arg = engine.api.set_on_retryable_response.call_args_list[-1][0][0]
        assert last_arg is None

    def test_retry_count_incremented_by_hook(self):
        """retry_count dans BatchOutcome reflète le nombre de fois que le hook a été déclenché."""
        job = self._make_job()
        engine = MockEngine()
        limiter = _make_limiter()
        cancel_event = threading.Event()

        installed_hook = []

        def capturing_set(hook):
            if hook is not None:
                installed_hook.append(hook)

        engine.api.set_on_retryable_response = MagicMock(side_effect=capturing_set)

        # Simuler 2 retry via le hook avant que _translate_batch réussisse
        call_count = [0]

        def translate_with_retries(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1 and installed_hook:
                # Déclencher le hook manuellement (simule BaseAPI qui fire le hook sur 429)
                installed_hook[0](429, 5.0)
                installed_hook[0](503, None)
            return BatchTranslationResult(
                status=OperationStatus.SUCCEEDED,
                lines=["译:hello"],
            )

        engine._translate_batch = translate_with_retries

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        # 2 appels au hook = retry_count == 2
        assert outcome.retry_count == 2
        # Le hook a aussi notifié le limiter: 1x record_rate_limited (429) + 1x record_timeout (503)
        assert limiter.metrics()["total_429"] == 1

    def test_hook_not_installed_when_engine_has_no_api(self):
        """Pas d'erreur si engine n'a pas d'attribut api."""
        job = self._make_job()
        engine = MockEngine()
        del engine.api  # Retirer l'attribut api
        limiter = _make_limiter()
        cancel_event = threading.Event()

        # Doit fonctionner sans exception
        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.translated_lines == ("译:hello",)
        assert outcome.retry_count == 0

    def test_429_no_double_count_when_hook_already_notified(self):
        """Si le hook a déjà notifié le limiter, le handler TranslationRequestError ne re-notifie pas."""
        job = self._make_job()
        engine = MockEngine()
        limiter = _make_limiter()
        cancel_event = threading.Event()

        installed_hook = []

        def capturing_set(hook):
            if hook is not None:
                installed_hook.append(hook)

        engine.api.set_on_retryable_response = MagicMock(side_effect=capturing_set)

        # Simuler: hook déclenché une fois sur 429 PUIS TranslationRequestError levé
        def translate_fires_hook_then_raises(*args, **kwargs):
            if installed_hook:
                installed_hook[0](429, 3.0)  # hook notifie d'abord
            raise TranslationRequestError("still 429", status_code=429, retry_after_seconds=3.0)

        engine._translate_batch = translate_fires_hook_then_raises

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.rate_limited is True
        # total_429 == 1 (via hook), pas 2 (pas double-comptage)
        assert limiter.metrics()["total_429"] == 1

    def test_429_notified_when_hook_never_fired(self):
        """Si le hook n'a jamais déclenché (max_attempts=1), le handler notifie le limiter."""
        job = self._make_job()
        engine = MockEngine()
        engine.raise_exception = TranslationRequestError(
            "rate limited", status_code=429, retry_after_seconds=10.0
        )
        # Pas de set_on_retryable_response sur cet api mock
        limiter = _make_limiter()
        cancel_event = threading.Event()

        outcome = execute_batch_job(job, engine, cancel_event, limiter)

        assert outcome.rate_limited is True
        assert outcome.retry_count == 0
        # Le handler a notifié le limiter
        assert limiter.metrics()["total_429"] == 1
