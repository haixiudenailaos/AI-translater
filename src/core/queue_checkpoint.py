#!/usr/bin/env python3
"""批次检查点保存协调器（队列翻译并发优化阶段 4）。

职责（见 QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §10.2）：
- 每任务最多一个保存 Future（single-flight）。
- 500-1000 ms debounce，避免每个批次都写盘。
- 保存进行中又有新结果时只提升 generation，完成后再补一次最新快照。
- 使用现有原子写能力（``write_text_atomic`` / EPUB ``save_translations``）。
- 取消/关闭前触发最后一次关键保存并设置合理超时。
- 保存失败保留 dirty 和错误状态，不能假装任务已完整完成。
- 按规范化源路径或 EPUB project ID 做独占写锁，避免两个任务并发写同一文件。

线程模型：
- ``mark_dirty`` / ``flush_blocking`` 由 Coordinator 线程调用（任务注册表锁外）。
- 内部保存线程串行执行写盘，不阻塞调度。
- ``close`` 幂等，等待最后一次保存完成（带超时）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..utils.logger import get_logger

logger = get_logger(__name__)

# debounce 间隔（§10.2：500-1000ms）。
_DEBOUNCE_SECONDS = 0.5

# 关键保存（关闭/取消）的最大等待时长。
_FINAL_SAVE_TIMEOUT_SECONDS = 5.0

# P1-2：保存失败后的有界重试上限。超过后进入失败终态，
# 避免无限重试占用线程并掩盖底层故障（磁盘满、只读路径等）。
_MAX_SAVE_RETRIES = 5
# P1-2：重试之间的指数退避基数（秒）。第 n 次重试等待 _RETRY_BACKOFF_BASE * 2^n。
_RETRY_BACKOFF_BASE = 0.5


@dataclass(frozen=True, slots=True)
class CheckpointSnapshot:
    """不可变保存快照。

    ``generation`` 单调递增，用于在保存线程中判断是否已被更新快照取代。
    保存线程只读取快照数据，不修改任务状态。
    """

    task_id: str
    generation: int
    # 保存函数：接收不可变数据，执行原子写。失败时抛异常。
    save_fn: Callable[[int], None]
    # generation 作为参数传入，便于保存函数校验是否仍是最新。


class _TaskSaveState:
    """单个任务的保存状态（受 ``CheckpointCoordinator._lock`` 保护）。"""

    __slots__ = (
        "dirty",
        "latest_generation",
        "pending_snapshot",
        "pending_snapshot_factory",
        "save_in_flight",
        "last_error",
        "wake_event",
        "closed",
        "retry_count",
        "failed_terminal",
        "next_retry_at",
    )

    def __init__(self) -> None:
        self.dirty: bool = False
        self.latest_generation: int = 0
        self.pending_snapshot: CheckpointSnapshot | None = None
        self.pending_snapshot_factory: Callable[[int], CheckpointSnapshot] | None = None
        self.save_in_flight: bool = False
        self.last_error: str | None = None
        self.wake_event = threading.Event()
        self.closed: bool = False
        # P1-2：失败重试与终态
        self.retry_count: int = 0
        self.failed_terminal: bool = False
        self.next_retry_at: float = 0.0


class CheckpointCoordinator:
    """单任务检查点协调器：single-flight + debounce 原子保存。

    生命周期由 ``QueueTranslationCoordinator`` 管理：每任务一个实例，
    在任务移除/终态时 ``close()``。
    """

    def __init__(
        self,
        task_id: str,
        *,
        debounce_seconds: float = _DEBOUNCE_SECONDS,
        clock: callable = time.monotonic,
    ) -> None:
        self._task_id = task_id
        self._debounce = max(0.0, float(debounce_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = _TaskSaveState()
        self._save_thread: threading.Thread | None = None
        self._last_dirty_at: float = 0.0

    # ── Coordinator 调用 ────────────────────────────────

    def mark_dirty(self, snapshot: CheckpointSnapshot) -> None:
        """标记任务有未保存的成功批次，提交不可变快照。

        幂等可重入。若保存线程正在执行，仅提升 generation，
        保存完成后会自动补一次最新快照。

        P1-2：新快照（generation 推进）会重置失败重试计数和失败终态，
        让任务在故障恢复后能够继续保存。
        """
        with self._lock:
            if self._state.closed:
                return
            self._state.dirty = True
            # P1-2：generation 推进时清除失败终态，允许新快照重新尝试保存
            if snapshot.generation > self._state.latest_generation:
                self._state.retry_count = 0
                self._state.failed_terminal = False
                self._state.next_retry_at = 0.0
            self._state.latest_generation = snapshot.generation
            self._state.pending_snapshot = snapshot
            self._state.pending_snapshot_factory = None
            self._state.last_error = None
            self._last_dirty_at = self._clock()
            self._state.wake_event.set()
            self._ensure_save_thread_locked()

    def mark_dirty_lazy(
        self,
        generation: int,
        snapshot_factory: Callable[[int], CheckpointSnapshot],
    ) -> None:
        """Mark dirty without building a full snapshot until debounce expires.

        Queue batch completion only needs to advance a generation.  The factory
        runs in the checkpoint thread after the debounce window, so repeated
        fast batches coalesce before copying large translation buffers.
        """
        with self._lock:
            if self._state.closed:
                return
            self._state.dirty = True
            if generation > self._state.latest_generation:
                self._state.retry_count = 0
                self._state.failed_terminal = False
                self._state.next_retry_at = 0.0
            self._state.latest_generation = generation
            self._state.pending_snapshot = None
            self._state.pending_snapshot_factory = snapshot_factory
            self._state.last_error = None
            self._last_dirty_at = self._clock()
            self._state.wake_event.set()
            self._ensure_save_thread_locked()

    def flush_blocking(self, timeout: float = _FINAL_SAVE_TIMEOUT_SECONDS) -> bool:
        """同步触发并等待最后一次保存完成（关闭/取消前调用）。

        返回 True 表示保存成功（或本就无 dirty）；False 表示超时或保存失败。
        """
        with self._lock:
            if self._state.closed:
                return not self._state.dirty
            if not self._state.dirty and not self._state.save_in_flight:
                return True
            self._last_dirty_at = 0.0  # 立即触发，不等 debounce
            self._state.wake_event.set()
            self._ensure_save_thread_locked()
        # 在锁外等待保存线程完成最新一次保存。
        deadline = self._clock() + max(0.0, timeout)
        while self._clock() < deadline:
            with self._lock:
                if not self._state.dirty and not self._state.save_in_flight:
                    return self._state.last_error is None
            time.sleep(0.05)
        return False

    @property
    def has_error(self) -> bool:
        with self._lock:
            return self._state.last_error is not None

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._state.last_error

    @property
    def is_dirty(self) -> bool:
        with self._lock:
            return self._state.dirty

    @property
    def has_terminal_failure(self) -> bool:
        """P1-2：是否进入失败终态（达到重试上限）。"""
        with self._lock:
            return self._state.failed_terminal

    @property
    def retry_count(self) -> int:
        """P1-2：当前已重试次数（用于诊断和测试断言）。"""
        with self._lock:
            return self._state.retry_count

    def close(self, timeout: float = _FINAL_SAVE_TIMEOUT_SECONDS) -> None:
        """关闭协调器，触发最后一次关键保存并等待（带超时）。幂等。

        P1-2：``close`` 必须确保保存线程退出，否则调用方可能在持有
        协调器引用的情况下退出而留下僵尸线程。``_save_thread.join``
        带超时，超时后记录 warning 但不再阻塞调用方。
        """
        with self._lock:
            if self._state.closed:
                return
            self._state.closed = True
            self._state.wake_event.set()
            self._ensure_save_thread_locked()
        # 等待保存线程结束。
        if self._save_thread is not None:
            self._save_thread.join(timeout=max(0.0, timeout))
            if self._save_thread.is_alive():
                logger.warning(
                    "任务 %s 检查点保存线程在 close 后仍未退出",
                    self._task_id,
                )

    # ── 保存线程 ────────────────────────────────────────

    def _ensure_save_thread_locked(self) -> None:
        """在持有 ``_lock`` 时确保保存线程已启动。"""
        if self._save_thread is not None and self._save_thread.is_alive():
            return
        self._save_thread = threading.Thread(
            target=self._save_loop,
            name=f"queue-checkpoint-{self._task_id}",
            daemon=True,
        )
        self._save_thread.start()

    def _save_loop(self) -> None:
        """保存线程主循环：debounce 等待 + single-flight 串行写盘。"""
        while True:
            # 取下一个待保存快照（带 debounce 等待）。
            snapshot = self._await_next_snapshot()
            if snapshot is None:
                return  # 已关闭且无待保存
            self._execute_save(snapshot)
            # 保存完成后检查是否又有新 dirty；若有则继续循环。
            with self._lock:
                if self._state.closed and not self._state.dirty:
                    return
                if not self._state.dirty:
                    return

    def _await_next_snapshot(self) -> CheckpointSnapshot | None:
        """阻塞直到 debounce 到期或关闭，返回待保存快照。

        P1-2：失败重试使用指数退避；达到 ``_MAX_SAVE_RETRIES`` 后进入
        失败终态（``failed_terminal``），保存线程退出，避免无限重试。
        新 ``mark_dirty`` 提交的更高 generation 快照会重置终态。
        """
        while True:
            snap: CheckpointSnapshot | None = None
            snapshot_factory: Callable[[int], CheckpointSnapshot] | None = None
            generation = 0
            ready_to_build = False
            with self._lock:
                # 失败终态：停止保存循环，等待新 mark_dirty 唤醒
                if self._state.failed_terminal:
                    return None
                if self._state.closed and not self._state.dirty:
                    return None
                if self._state.dirty and (
                    self._state.pending_snapshot is not None
                    or self._state.pending_snapshot_factory is not None
                ):
                    # debounce：自上次 dirty 起等待 _debounce 秒
                    wait_remaining = 0.0
                    if self._last_dirty_at > 0:
                        wait_remaining = self._last_dirty_at + self._debounce - self._clock()
                    # P1-2：失败重试退避（pending_snapshot 来自失败重试时 next_retry_at 已设）
                    if self._state.next_retry_at > 0:
                        retry_wait = self._state.next_retry_at - self._clock()
                        if retry_wait > wait_remaining:
                            wait_remaining = retry_wait
                    if wait_remaining <= 0:
                        # Take the builder under lock, then construct the
                        # potentially large snapshot after releasing it.
                        snap = self._state.pending_snapshot
                        snapshot_factory = self._state.pending_snapshot_factory
                        generation = self._state.latest_generation
                        self._state.pending_snapshot = None
                        self._state.pending_snapshot_factory = None
                        self._state.save_in_flight = True
                        ready_to_build = True
                else:
                    wait_remaining = max(0.5, self._debounce)
            if ready_to_build:
                if snapshot_factory is not None:
                    try:
                        snap = snapshot_factory(generation)
                    except Exception as exc:  # noqa: BLE001

                        def _raise_snapshot_error(_generation: int, error=exc) -> None:
                            raise error

                        snap = CheckpointSnapshot(
                            task_id=self._task_id,
                            generation=generation,
                            save_fn=_raise_snapshot_error,
                        )
                if snap is not None:
                    return snap
                # A caller must provide either a snapshot or a factory. Keep
                # the coordinator live if a malformed request slips through.
                with self._lock:
                    self._state.save_in_flight = False
                    self._state.dirty = False
                    self._state.wake_event.set()
                continue
            # 锁外等待唤醒
            self._state.wake_event.clear()
            # 二次检查避免丢失唤醒
            with self._lock:
                if self._state.failed_terminal:
                    return None
                if self._state.dirty and (
                    self._state.pending_snapshot is not None
                    or self._state.pending_snapshot_factory is not None
                ):
                    continue  # 重新走 debounce 计算
                if self._state.closed and not self._state.dirty:
                    return None
            self._state.wake_event.wait(timeout=max(wait_remaining, 0.05))

    def _execute_save(self, snapshot: CheckpointSnapshot) -> None:
        """执行一次原子保存。

        P1-2：失败时恢复 ``pending_snapshot``（避免 dirty=True/pending=None 死锁），
        按指数退避安排重试；达到上限后进入失败终态并退出保存线程。
        """
        try:
            snapshot.save_fn(snapshot.generation)
            # 保存成功：清除 dirty 标记（若 generation 仍是最新）。
            with self._lock:
                self._state.save_in_flight = False
                # 只有当没有更新的快照覆盖时才清除 dirty。
                if self._state.latest_generation == snapshot.generation:
                    self._state.dirty = False
                # P1-2：成功后重置重试计数
                self._state.retry_count = 0
                self._state.failed_terminal = False
                self._state.next_retry_at = 0.0
                self._state.last_error = None
                self._state.wake_event.set()
        except Exception as exc:
            logger.error("任务 %s 检查点保存失败: %s", self._task_id, exc)
            with self._lock:
                self._state.save_in_flight = False
                # P1-2：恢复 pending_snapshot，避免 dirty=True/pending=None 死锁。
                # 若期间有更新的 mark_dirty 到达，保留新快照不覆盖。
                if self._state.pending_snapshot is None:
                    self._state.pending_snapshot = snapshot
                self._state.last_error = str(exc)
                self._state.retry_count += 1
                if self._state.retry_count >= _MAX_SAVE_RETRIES:
                    # P1-2：进入失败终态。保留 dirty 与 last_error，
                    # 保存线程退出；新 mark_dirty（generation 推进）才能恢复。
                    self._state.failed_terminal = True
                    logger.error(
                        "任务 %s 检查点保存达到重试上限 %d，进入失败终态",
                        self._task_id,
                        _MAX_SAVE_RETRIES,
                    )
                else:
                    # P1-2：指数退避，下次重试时间 = now + base * 2^(retry_count-1)
                    backoff = _RETRY_BACKOFF_BASE * (2 ** (self._state.retry_count - 1))
                    self._state.next_retry_at = self._clock() + backoff
                self._state.wake_event.set()


# ── 同路径独占写锁 ─────────────────────────────────────


class _WriteLockEntry:
    __slots__ = ("lock", "ref_count")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ref_count = 0


class ExclusiveWriteLockRegistry:
    """按规范化输出路径做独占写锁（§10.2）。

    同一输入被重复添加时，拒绝第二个活动任务或复用已有任务，
    不能让两个任务同时写同一个 ``_译文.txt`` 或 mapping 目录。

    线程安全。引用计数：任务终态时 ``release``，最后一个引用释放锁。
    """

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._entries: dict[str, _WriteLockEntry] = {}

    def try_acquire(self, key: str) -> bool:
        """尝试获取指定 key 的写锁。成功返回 True；已被占用返回 False。"""
        entry: _WriteLockEntry
        with self._registry_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _WriteLockEntry()
                self._entries[key] = entry
            if entry.lock.acquire(blocking=False):
                entry.ref_count += 1
                return True
            return False

    def release(self, key: str) -> None:
        """释放指定 key 的写锁。幂等。"""
        with self._registry_lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            try:
                entry.lock.release()
            except RuntimeError:
                return
            entry.ref_count -= 1
            if entry.ref_count <= 0:
                self._entries.pop(key, None)

    def is_held(self, key: str) -> bool:
        with self._registry_lock:
            entry = self._entries.get(key)
            return entry is not None and entry.ref_count > 0


def make_txt_save_fn(
    file_handler, target_path: Path, target_lines: list[str]
) -> Callable[[int], None]:
    """构造 TXT 检查点保存函数。

    闭包捕获不可变快照数据（``tuple(target_lines)``），保存线程只读取。
    ``generation`` 参数用于日志，不参与写盘逻辑。
    """

    snapshot_lines = list(target_lines)

    def _save(generation: int) -> None:
        content = "\n".join(snapshot_lines)
        file_handler.write_file(str(target_path), content)

    return _save
