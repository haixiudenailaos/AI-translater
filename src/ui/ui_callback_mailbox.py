#!/usr/bin/env python3
"""P1-1：通用 UI 回调邮箱与 Tk 主线程事件泵。

解决 ``PYTHON_UIUX_BEST_PRACTICES_AUDIT.md`` P1-1 问题：
工作线程直接调用 ``root.after()`` / ``winfo_exists()`` 是跨线程 Tk 调用，
依赖 Tcl 构建和 mainloop 时序，窗口销毁或高并发时可能静默丢事件、卡住
或抛 ``TclError``。

方案：复用已有的 mailbox/event-pump 模式（参考 ``translation_event_mailbox.py``
和 ``tk_event_pump.py``），提供通用的 UI 回调邮箱：

- 工作线程只调用 ``UICallbackMailbox.submit(callback)``（基于 ``queue.Queue``，
  线程安全）。
- ``TkUICallbackPump`` 由 Tk 主线程创建，按固定间隔排空邮箱并执行回调。
- 关闭时先标记 ``closed``，再取消 ``after`` 调度，最后关闭邮箱拒绝后续 submit。
- 窗口销毁后的迟到事件被安全丢弃，``discarded_count`` 提供可观测计数。

约束：
- ``start`` / ``_poll`` / ``close`` 只由 Tk 主线程调用。
- 工作线程只能调用 ``mailbox.submit()``。
- 回调执行期间的异常被 pump 捕获并记录，不会中断后续回调。
"""

from __future__ import annotations

import queue
import time
from collections import deque
from threading import Lock
from typing import Callable, List

from ..utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_MAX_CALLBACKS_PER_POLL = 64
_DEFAULT_MAX_FRAME_MS = 8.0
_DEFAULT_IDLE_INTERVAL_MS = 250
_BACKLOG_FALLBACK_INTERVAL_MS = 1


class UICallbackMailbox:
    """P1-1：通用 UI 回调邮箱（线程安全）。

    工作线程 ``submit`` 回调，Tk 主线程通过 pump ``drain`` 排空并执行。

    - ``submit`` 可由多个工作线程并发调用（基于 ``queue.Queue``）。
    - ``drain`` 只由 Tk 主线程调用（与 pump 轮询同步）。
    - ``close`` 后迟到的 ``submit`` 被安全丢弃，``discarded_count`` 提供可观测计数。
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._queue: queue.Queue[tuple[float, Callable[[], None]]] = queue.Queue()
        self._keyed_callbacks: dict[str, Callable[[], None]] = {}
        self._closed = False
        self._lock = Lock()
        self._clock = clock or time.monotonic
        self._discarded_count = 0
        self._coalesced_count = 0
        self._total_submitted = 0
        self._total_drained = 0

    def submit(self, callback: Callable[[], None]) -> None:
        """工作线程提交回调（线程安全）。

        关闭后的迟到回调被安全丢弃，并增加 ``discarded_count``。
        """
        with self._lock:
            if self._closed:
                self._discarded_count += 1
                return
            self._total_submitted += 1
        self._queue.put((self._clock(), callback))

    def submit_keyed(self, key: str, callback: Callable[[], None]) -> None:
        """Submit at most one pending callback for a coalescing key.

        Progress producers can replace a stale pending render with their newest
        state without growing the Tk backlog. Normal ``submit`` calls retain
        strict FIFO behavior for terminal and user-visible callbacks.
        """
        with self._lock:
            if self._closed:
                self._discarded_count += 1
                return
            already_pending = key in self._keyed_callbacks
            self._keyed_callbacks[key] = callback
            self._total_submitted += 1
            if already_pending:
                self._coalesced_count += 1
        if already_pending:
            return

        def run_latest() -> None:
            with self._lock:
                latest = self._keyed_callbacks.pop(key, None)
            if latest is not None:
                latest()

        self._queue.put((self._clock(), run_latest))

    def drain(
        self, max_callbacks: int = _DEFAULT_MAX_CALLBACKS_PER_POLL
    ) -> List[Callable[[], None]]:
        """主线程排空邮箱，返回待执行回调列表（保持提交顺序）。

        只由 Tk 主线程调用（与 pump 轮询同步）。排空后邮箱为空。
        """
        if max_callbacks <= 0:
            raise ValueError("max_callbacks must be positive")
        callbacks: List[Callable[[], None]] = []
        while len(callbacks) < max_callbacks:
            try:
                _submitted_at, callback = self._queue.get_nowait()
                callbacks.append(callback)
            except queue.Empty:
                break
        with self._lock:
            self._total_drained += len(callbacks)
        return callbacks

    def close(self) -> None:
        """关闭邮箱，拒绝后续 ``submit``。幂等。"""
        with self._lock:
            self._closed = True
            self._keyed_callbacks.clear()

    @property
    def is_closed(self) -> bool:
        """邮箱是否已关闭。"""
        return self._closed

    @property
    def discarded_count(self) -> int:
        """关闭后被丢弃的迟到回调数（可观测计数，满足 P1-1 验收标准）。"""
        return self._discarded_count

    @property
    def coalesced_count(self) -> int:
        """Number of stale keyed renders replaced before reaching Tk."""
        return self._coalesced_count

    @property
    def pending_count(self) -> int:
        """当前待处理的回调数。"""
        return self._queue.qsize()

    @property
    def oldest_pending_age_seconds(self) -> float:
        """Age of the oldest queued callback, or zero when the mailbox is empty."""
        with self._queue.mutex:
            if not self._queue.queue:
                return 0.0
            submitted_at = self._queue.queue[0][0]
        return max(0.0, self._clock() - submitted_at)

    @property
    def total_submitted(self) -> int:
        """累计提交数（不含被丢弃的迟到回调）。"""
        return self._total_submitted

    @property
    def total_drained(self) -> int:
        """累计排空数。"""
        return self._total_drained


class TkUICallbackPump:
    """P1-1：Tk 主线程 UI 回调事件泵。

    按固定间隔（默认 50ms，对应 20 次/秒）在 Tk 主线程排空
    ``UICallbackMailbox`` 并执行回调。

    约束：
    - ``start`` / ``_poll`` / ``close`` 只由 Tk 主线程调用。
    - 工作线程只能调用 ``mailbox.submit()``。
    - 关闭时先标记 ``_closed``，再取消 ``after`` 调度，最后关闭邮箱。
    - 回调执行期间的异常被捕获并记录（通过 ``on_error``），不中断后续回调。
    """

    def __init__(
        self,
        root,
        mailbox: UICallbackMailbox,
        interval_ms: int = 50,
        on_error: Callable[[Exception], None] | None = None,
        max_callbacks_per_poll: int = _DEFAULT_MAX_CALLBACKS_PER_POLL,
        max_frame_ms: float = _DEFAULT_MAX_FRAME_MS,
        idle_interval_ms: int = _DEFAULT_IDLE_INTERVAL_MS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_callbacks_per_poll <= 0:
            raise ValueError("max_callbacks_per_poll must be positive")
        if max_frame_ms <= 0:
            raise ValueError("max_frame_ms must be positive")
        if idle_interval_ms <= 0:
            raise ValueError("idle_interval_ms must be positive")
        self._root = root
        self._mailbox = mailbox
        # 默认 50ms 对应 20 次/秒，满足 UI 响应需求且不过度消耗 CPU。
        self._interval_ms = interval_ms
        self._after_id: str | None = None
        self._closed = False
        self._on_error = on_error
        self._max_callbacks_per_poll = max_callbacks_per_poll
        self._max_frame_seconds = max_frame_ms / 1000.0
        self._idle_interval_ms = idle_interval_ms
        self._clock = clock or time.monotonic
        self._deferred_callbacks: deque[Callable[[], None]] = deque()
        self._executed_count = 0
        self._error_count = 0
        self._last_frame_callback_count = 0
        self._last_frame_duration_ms = 0.0
        self._max_frame_duration_ms = 0.0
        self._over_budget_frame_count = 0

    def start(self) -> None:
        """启动事件泵。幂等：已启动或已关闭时不再调度。"""
        if self._closed or self._after_id is not None:
            return
        self._after_id = self._root.after(self._interval_ms, self._poll)

    def _schedule_backlog_poll(self) -> None:
        """Yield to Tk before continuing an accumulated callback backlog."""
        if self._closed or self._after_id is not None:
            return
        after_idle = getattr(self._root, "after_idle", None)
        if callable(after_idle):
            self._after_id = after_idle(self._poll)
        else:
            self._after_id = self._root.after(_BACKLOG_FALLBACK_INTERVAL_MS, self._poll)

    def _schedule_idle_poll(self) -> None:
        if self._closed or self._after_id is not None:
            return
        self._after_id = self._root.after(self._idle_interval_ms, self._poll)

    def _take_callbacks_for_frame(self) -> list[Callable[[], None]]:
        callbacks: list[Callable[[], None]] = []
        while self._deferred_callbacks and len(callbacks) < self._max_callbacks_per_poll:
            callbacks.append(self._deferred_callbacks.popleft())
        remaining = self._max_callbacks_per_poll - len(callbacks)
        if remaining:
            callbacks.extend(self._mailbox.drain(remaining))
        return callbacks

    def _poll(self) -> None:
        """排空邮箱并执行回调。只在 Tk 主线程执行。"""
        self._after_id = None
        if self._closed:
            return
        callbacks = self._take_callbacks_for_frame()
        executed_this_frame = 0
        for callback in callbacks:
            try:
                callback()
                self._executed_count += 1
                executed_this_frame += 1
            except Exception as exc:
                executed_this_frame += 1
                self._error_count += 1
                # 记录异常但不中断后续回调
                logger.debug("UI 回调执行异常: %s", exc, exc_info=True)
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass
        # 继续下一轮调度
        self.start()

    def close(self) -> None:
        """关闭事件泵，取消待执行的 after 回调。幂等。

        同时关闭邮箱以拒绝后续 submit（迟到事件被丢弃并计数）。
        """
        self._closed = True
        self._mailbox.close()
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except Exception:
                # Tk 可能已被销毁，忽略取消失败
                pass
            self._after_id = None

    @property
    def is_closed(self) -> bool:
        """事件泵是否已关闭。"""
        return self._closed

    @property
    def executed_count(self) -> int:
        """累计成功执行的回调数。"""
        return self._executed_count

    @property
    def error_count(self) -> int:
        """累计执行失败的回调数。"""
        return self._error_count
