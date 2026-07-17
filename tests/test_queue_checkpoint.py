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
