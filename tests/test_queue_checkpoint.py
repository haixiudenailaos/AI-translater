#!/usr/bin/env python3
"""检查点协调器单元测试（队列翻译并发优化阶段 4）。

覆盖：
- ``CheckpointCoordinator`` single-flight：并发 mark_dirty 只触发一次保存。
- debounce：短时间内多次 mark_dirty 只保存最新 generation。
- ``flush_blocking``：同步等待最后一次保存完成。
- 保存失败保留 dirty 和错误状态。
- ``ExclusiveWriteLockRegistry`` 同路径独占。
"""

from src.core.queue_checkpoint import (
    CheckpointCoordinator,
    CheckpointSnapshot,
    ExclusiveWriteLockRegistry,
    make_txt_save_fn,
)
from src.utils.file_handler import FileHandler


def _make_snapshot(task_id: str, generation: int, save_fn) -> CheckpointSnapshot:
    return CheckpointSnapshot(
        task_id=task_id,
        generation=generation,
        save_fn=save_fn,
    )


class TestCheckpointSingleFlight:
    def test_mark_dirty_triggers_save(self, tmp_path):
        target = tmp_path / "out.txt"
        fh = FileHandler()
        saved_generations = []

        def _save(generation):
            saved_generations.append(generation)

        cc = CheckpointCoordinator("task-1", debounce_seconds=0.05)
        try:
            cc.mark_dirty(_make_snapshot("task-1", 1, _save))
            # 等待 debounce + 保存
            assert cc.flush_blocking(timeout=2.0) is True
            assert 1 in saved_generations
        finally:
            cc.close()

    def test_concurrent_mark_dirty_coalesces(self, tmp_path):
        """多个 mark_dirty 在 debounce 窗口内合并为一次保存（最新 generation）。"""
        saved_generations = []

        def _save(generation):
            saved_generations.append(generation)

        cc = CheckpointCoordinator("task-2", debounce_seconds=0.1)
        try:
            # 快速连续提交 5 个 generation
            for gen in range(1, 6):
                cc.mark_dirty(_make_snapshot("task-2", gen, _save))
            assert cc.flush_blocking(timeout=2.0) is True
            # 至少保存了最新 generation（5）
            assert 5 in saved_generations
            # debounce 期间不一定全部保存，但不应每个 generation 都保存
            # （允许最多 2 次：debounce 期间的 1 次 + flush 触发的 1 次）
            assert len(saved_generations) <= 3
        finally:
            cc.close()

    def test_lazy_snapshot_is_built_once_after_debounce(self):
        """PERF-3：高频 dirty 不应在每次调用时复制完整文档快照。"""
        built_generations: list[int] = []
        saved_generations: list[int] = []
        cc = CheckpointCoordinator("lazy-snapshot", debounce_seconds=0.05)

        def make_snapshot(generation: int) -> CheckpointSnapshot:
            built_generations.append(generation)
            return _make_snapshot(
                "lazy-snapshot",
                generation,
                lambda saved_generation: saved_generations.append(saved_generation),
            )

        try:
            for generation in range(1, 8):
                cc.mark_dirty_lazy(generation, make_snapshot)
            assert cc.flush_blocking(timeout=2.0) is True
            assert built_generations == [7]
            assert saved_generations == [7]
        finally:
            cc.close()

    def test_flush_blocking_returns_true_when_no_dirty(self, tmp_path):
        cc = CheckpointCoordinator("task-3")
        try:
            assert cc.flush_blocking(timeout=1.0) is True
        finally:
            cc.close()

    def test_save_failure_keeps_dirty_and_error(self, tmp_path):
        def _failing_save(generation):
            raise OSError("disk full")

        cc = CheckpointCoordinator("task-4", debounce_seconds=0.02)
        try:
            cc.mark_dirty(_make_snapshot("task-4", 1, _failing_save))
            # 等待保存尝试完成
            result = cc.flush_blocking(timeout=2.0)
            # 保存失败：flush 返回 False，dirty 保留，has_error=True
            assert result is False
            assert cc.has_error is True
            assert "disk full" in (cc.last_error or "")
        finally:
            cc.close()

    def test_close_is_idempotent(self, tmp_path):
        cc = CheckpointCoordinator("task-5")
        cc.close()
        cc.close()  # 不抛异常

    def test_p1_2_save_failure_restores_pending_snapshot_no_deadlock(self, tmp_path):
        """P1-2：保存失败时必须恢复 pending_snapshot，避免 dirty=True/pending=None 死锁。

        旧实现在保存前 ``pending_snapshot = None``，失败时未恢复，
        导致后续 ``_await_next_snapshot`` 永远进入 else 分支等待唤醒，
        即便有未保存的 dirty 也无法重试。新实现恢复 pending_snapshot 并
        按指数退避重试。
        """
        from src.core import queue_checkpoint

        # 缩短重试上限与退避基数，使测试在 1 秒内进入终态
        saved_max_retries = queue_checkpoint._MAX_SAVE_RETRIES
        saved_backoff = queue_checkpoint._RETRY_BACKOFF_BASE
        queue_checkpoint._MAX_SAVE_RETRIES = 2
        queue_checkpoint._RETRY_BACKOFF_BASE = 0.01
        try:
            attempts = []

            def _failing_save(generation):
                attempts.append(generation)
                raise OSError("disk full")

            cc = CheckpointCoordinator("p1-2-task", debounce_seconds=0.01)
            try:
                cc.mark_dirty(_make_snapshot("p1-2-task", 1, _failing_save))
                # 等待重试到达上限，进入失败终态
                import time

                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if cc.has_terminal_failure:
                        break
                    time.sleep(0.02)
                # 至少尝试过 MAX_RETRIES 次
                assert len(attempts) >= 2
                assert cc.has_terminal_failure is True
                assert cc.retry_count >= 2
                assert cc.is_dirty is True  # dirty 保留
                assert cc.has_error is True
            finally:
                cc.close()
        finally:
            queue_checkpoint._MAX_SAVE_RETRIES = saved_max_retries
            queue_checkpoint._RETRY_BACKOFF_BASE = saved_backoff

    def test_p1_2_new_generation_resets_terminal_failure(self, tmp_path):
        """P1-2：失败终态后，新的 mark_dirty（更高 generation）必须重置终态。"""
        from src.core import queue_checkpoint

        saved_max_retries = queue_checkpoint._MAX_SAVE_RETRIES
        saved_backoff = queue_checkpoint._RETRY_BACKOFF_BASE
        queue_checkpoint._MAX_SAVE_RETRIES = 1
        queue_checkpoint._RETRY_BACKOFF_BASE = 0.01
        try:
            attempts = []

            def _fail_then_succeed(generation):
                attempts.append(generation)
                if generation == 1:
                    raise OSError("disk full")
                # generation 2 不抛异常，保存成功

            cc = CheckpointCoordinator("p1-2-recover", debounce_seconds=0.01)
            try:
                cc.mark_dirty(_make_snapshot("p1-2-recover", 1, _fail_then_succeed))
                import time

                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if cc.has_terminal_failure:
                        break
                    time.sleep(0.02)
                assert cc.has_terminal_failure is True

                # 提交更高 generation，应重置终态并恢复保存
                cc.mark_dirty(_make_snapshot("p1-2-recover", 2, _fail_then_succeed))
                assert cc.has_terminal_failure is False
                assert cc.flush_blocking(timeout=2.0) is True
                assert cc.is_dirty is False
                assert 2 in attempts
            finally:
                cc.close()
        finally:
            queue_checkpoint._MAX_SAVE_RETRIES = saved_max_retries
            queue_checkpoint._RETRY_BACKOFF_BASE = saved_backoff

    def test_p1_2_close_terminates_save_thread(self, tmp_path):
        """P1-2：close() 必须让保存线程退出，不留僵尸线程。"""
        import time

        def _slow_save(generation):
            time.sleep(0.5)

        cc = CheckpointCoordinator("p1-2-close", debounce_seconds=0.01)
        cc.mark_dirty(_make_snapshot("p1-2-close", 1, _slow_save))
        time.sleep(0.05)  # 让保存线程开始
        cc.close()
        # close 等待 join，结束后线程应已退出
        assert cc._save_thread is None or not cc._save_thread.is_alive()


class TestTxtSaveFn:
    def test_make_txt_save_fn_writes_file(self, tmp_path):
        fh = FileHandler()
        target = tmp_path / "novel_译文.txt"
        lines = ["第一行译文", "第二行译文", ""]
        save_fn = make_txt_save_fn(fh, target, lines)
        save_fn(generation=1)
        content = target.read_text(encoding="utf-8")
        assert "第一行译文" in content
        assert "第二行译文" in content


class TestExclusiveWriteLock:
    def test_try_acquire_same_key_twice_fails(self):
        registry = ExclusiveWriteLockRegistry()
        assert registry.try_acquire("txt:/path/to/file") is True
        # 同 key 第二次获取失败
        assert registry.try_acquire("txt:/path/to/file") is False

    def test_different_keys_independent(self):
        registry = ExclusiveWriteLockRegistry()
        assert registry.try_acquire("txt:/a")
        assert registry.try_acquire("txt:/b")

    def test_release_allows_reacquire(self):
        registry = ExclusiveWriteLockRegistry()
        assert registry.try_acquire("txt:/a")
        registry.release("txt:/a")
        assert registry.try_acquire("txt:/a") is True

    def test_release_idempotent(self):
        registry = ExclusiveWriteLockRegistry()
        # 未获取就 release 不抛异常
        registry.release("txt:/never")
        registry.release("txt:/never")

    def test_is_held(self):
        registry = ExclusiveWriteLockRegistry()
        assert not registry.is_held("txt:/a")
        registry.try_acquire("txt:/a")
        assert registry.is_held("txt:/a")
        registry.release("txt:/a")
        assert not registry.is_held("txt:/a")

    def test_ref_counting(self):
        """同 key 多次 acquire 累加引用计数，全部释放后才删除。"""
        registry = ExclusiveWriteLockRegistry()
        # 注意：同 key 第二次 try_acquire 会失败（lock 已被持有），
        # 这是独占语义。引用计数是为 release 配对设计的——
        # 实际场景中 add_task 成功后 try_acquire 一次，remove_task release 一次。
        assert registry.try_acquire("txt:/a")
        assert not registry.try_acquire("txt:/a")  # 第二次失败
        registry.release("txt:/a")
        # 释放后可重新获取
        assert registry.try_acquire("txt:/a")
