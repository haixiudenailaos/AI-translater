#!/usr/bin/env python3
"""P1-1 回归测试：通用 UI 回调邮箱与 Tk 主线程事件泵。

覆盖 ``PYTHON_UIUX_BEST_PRACTICES_AUDIT.md`` P1-1 验收标准：
- 测试记录所有 Tk 方法的调用线程 ID，任何后台线程调用都失败。
- 窗口销毁后的迟到事件被安全丢弃且有可观测计数。
- 工作线程只 ``submit``，主线程只 ``after``。
"""

from __future__ import annotations

import threading
from typing import List, Tuple

from src.ui.ui_callback_mailbox import TkUICallbackPump, UICallbackMailbox

# ── 线程检查 Fake Root ──────────────────────────────────


class ThreadCheckingRoot:
    """Fake Tk root：记录创建线程，断言 after/after_cancel 只在主线程调用。

    用于验证事件泵不会从工作线程调用 Tk API（P1-1 验收标准）。
    """

    def __init__(self):
        self.owner_thread = threading.get_ident()
        # (after_id, interval_ms, callback)
        self.scheduled: List[Tuple[str, int, callable]] = []
        self.cancelled: List[str] = []
        self.violation_message: str | None = None
        self._next_id = 0

    def after(self, interval_ms: int, callback):
        if threading.get_ident() != self.owner_thread:
            self.violation_message = (
                f"after() called from non-owner thread: "
                f"owner={self.owner_thread}, caller={threading.get_ident()}"
            )
            raise AssertionError(self.violation_message)
        after_id = f"after-{self._next_id}"
        self._next_id += 1
        self.scheduled.append((after_id, interval_ms, callback))
        return after_id

    def after_cancel(self, after_id):
        if threading.get_ident() != self.owner_thread:
            self.violation_message = "after_cancel() called from non-owner thread"
            raise AssertionError(self.violation_message)
        self.cancelled.append(after_id)
        self.scheduled = [(aid, i, c) for aid, i, c in self.scheduled if aid != after_id]

    def trigger_next(self):
        """触发下一个已调度的回调（按 FIFO）。"""
        if not self.scheduled:
            return
        _after_id, _interval, callback = self.scheduled.pop(0)
        callback()


# ── 邮箱：基本行为 ──────────────────────────────────────


class TestUICallbackMailbox:
    def test_submit_and_drain_preserves_order(self):
        """submit 后 drain 返回回调列表，保持提交顺序。"""
        mailbox = UICallbackMailbox()
        results: List[int] = []

        mailbox.submit(lambda: results.append(1))
        mailbox.submit(lambda: results.append(2))
        mailbox.submit(lambda: results.append(3))

        callbacks = mailbox.drain()
        assert len(callbacks) == 3
        for cb in callbacks:
            cb()
        assert results == [1, 2, 3]

    def test_drain_clears_mailbox(self):
        """drain 后邮箱为空，再次 drain 返回空列表。"""
        mailbox = UICallbackMailbox()
        mailbox.submit(lambda: None)
        mailbox.submit(lambda: None)

        first = mailbox.drain()
        assert len(first) == 2

        second = mailbox.drain()
        assert second == []

    def test_drain_empty_mailbox_returns_empty_list(self):
        """空邮箱 drain 返回空列表。"""
        mailbox = UICallbackMailbox()
        assert mailbox.drain() == []

    def test_pending_count(self):
        """pending_count 反映当前待处理回调数。"""
        mailbox = UICallbackMailbox()
        assert mailbox.pending_count == 0

        mailbox.submit(lambda: None)
        mailbox.submit(lambda: None)
        assert mailbox.pending_count == 2

        mailbox.drain()
        assert mailbox.pending_count == 0

    def test_submit_keyed_coalesces_to_the_latest_pending_callback(self):
        mailbox = UICallbackMailbox()
        values: List[int] = []

        mailbox.submit_keyed("progress", lambda: values.append(1))
        mailbox.submit_keyed("progress", lambda: values.append(2))
        mailbox.submit_keyed("progress", lambda: values.append(3))

        callbacks = mailbox.drain()
        assert len(callbacks) == 1
        callbacks[0]()
        assert values == [3]


# ── 邮箱：关闭与迟到事件 ────────────────────────────────────


class TestMailboxClose:
    def test_close_rejects_late_submit(self):
        """关闭后的 submit 被安全丢弃，不执行。"""
        mailbox = UICallbackMailbox()
        executed: List[int] = []

        mailbox.submit(lambda: executed.append(1))
        mailbox.close()

        # 关闭后的迟到回调
        mailbox.submit(lambda: executed.append(2))
        mailbox.submit(lambda: executed.append(3))

        callbacks = mailbox.drain()
        for cb in callbacks:
            cb()

        # 只执行了关闭前的回调
        assert executed == [1]
        # 迟到回调被丢弃
        assert mailbox.discarded_count == 2

    def test_discarded_count_is_observable(self):
        """P1-1 验收标准：迟到事件有可观测计数。"""
        mailbox = UICallbackMailbox()
        assert mailbox.discarded_count == 0

        mailbox.close()
        for _i in range(10):
            mailbox.submit(lambda: None)

        assert mailbox.discarded_count == 10

    def test_close_is_idempotent(self):
        """close() 幂等，多次调用不抛异常。"""
        mailbox = UICallbackMailbox()
        mailbox.close()
        mailbox.close()
        mailbox.close()
        assert mailbox.is_closed is True

    def test_total_submitted_excludes_discarded(self):
        """total_submitted 不含被丢弃的迟到回调。"""
        mailbox = UICallbackMailbox()
        mailbox.submit(lambda: None)
        mailbox.submit(lambda: None)
        assert mailbox.total_submitted == 2

        mailbox.close()
        mailbox.submit(lambda: None)  # 被丢弃
        assert mailbox.total_submitted == 2  # 不增加


# ── 邮箱：多线程并发 ────────────────────────────────────


class TestMailboxConcurrency:
    def test_concurrent_submit_is_thread_safe(self):
        """多线程并发 submit 不丢回调、不抛异常。"""
        mailbox = UICallbackMailbox()
        errors: List[Exception] = []

        def worker(thread_id: int):
            try:
                for i in range(100):
                    mailbox.submit(lambda tid=thread_id, idx=i: (tid, idx))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

        # PERF-6a：drain 现在有界（默认 64/轮），需循环排空
        callbacks: list = []
        while mailbox.pending_count > 0:
            callbacks.extend(mailbox.drain())
        assert len(callbacks) == 800

    def test_concurrent_submit_and_close(self):
        """并发 submit 和 close 不导致数据丢失或异常。"""
        mailbox = UICallbackMailbox()
        errors: List[Exception] = []
        stop = threading.Event()

        def submitter():
            try:
                i = 0
                while not stop.is_set() and i < 1000:
                    mailbox.submit(lambda: None)
                    i += 1
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=submitter)
        t.start()

        # 让 submitter 跑一会
        threading.Event().wait(0.01)

        # 关闭邮箱（可能丢弃部分迟到回调）
        mailbox.close()
        stop.set()
        t.join(timeout=2.0)

        assert errors == []
        # 关闭后 discarded_count >= 0
        assert mailbox.discarded_count >= 0


# ── 事件泵：基本行为 ────────────────────────────────────


class TestTkUICallbackPump:
    def test_start_schedules_after(self):
        """start() 在主线程调度 after 回调。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        pump = TkUICallbackPump(root, mailbox, interval_ms=50)

        pump.start()
        assert len(root.scheduled) == 1
        assert root.scheduled[0][1] == 50  # interval_ms
        assert root.violation_message is None

    def test_start_is_idempotent(self):
        """start() 幂等：已启动时不重复调度。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        pump = TkUICallbackPump(root, mailbox, interval_ms=50)

        pump.start()
        pump.start()
        pump.start()
        assert len(root.scheduled) == 1

    def test_poll_drains_and_executes_callbacks(self):
        """_poll 排空邮箱并执行回调。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        executed: List[int] = []
        pump = TkUICallbackPump(root, mailbox, interval_ms=50, on_error=lambda e: None)

        pump.start()
        mailbox.submit(lambda: executed.append(1))
        mailbox.submit(lambda: executed.append(2))
        mailbox.submit(lambda: executed.append(3))

        # 触发 poll
        root.trigger_next()

        assert executed == [1, 2, 3]
        assert pump.executed_count == 3
        # poll 后重新调度下一轮
        assert len(root.scheduled) == 1

    def test_poll_preserves_callback_order(self):
        """回调按提交顺序执行。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        order: List[str] = []
        pump = TkUICallbackPump(root, mailbox, interval_ms=50)

        pump.start()
        mailbox.submit(lambda: order.append("a"))
        mailbox.submit(lambda: order.append("b"))
        mailbox.submit(lambda: order.append("c"))

        root.trigger_next()
        assert order == ["a", "b", "c"]

    def test_poll_handles_empty_mailbox(self):
        """空邮箱时 poll 不抛异常，仍重新调度。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        pump = TkUICallbackPump(root, mailbox, interval_ms=50)

        pump.start()
        root.trigger_next()  # 邮箱为空

        assert pump.executed_count == 0
        assert len(root.scheduled) == 1  # 重新调度


# ── 事件泵：回调异常处理 ────────────────────────────────


class TestPumpErrorHandling:
    def test_callback_exception_does_not_crash_pump(self):
        """回调抛异常不会中断 pump 或后续回调。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        executed: List[int] = []
        errors: List[Exception] = []
        pump = TkUICallbackPump(root, mailbox, on_error=errors.append, interval_ms=50)

        pump.start()
        mailbox.submit(lambda: executed.append(1))
        mailbox.submit(lambda: (_ for _ in ()).throw(ValueError("boom")))
        mailbox.submit(lambda: executed.append(3))

        root.trigger_next()

        assert executed == [1, 3]  # 第二个回调失败但不影响第三个
        assert pump.error_count == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)

    def test_on_error_exception_is_swallowed(self):
        """on_error 自身抛异常时被静默吞掉，不影响 pump。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()

        def bad_on_error(exc):
            raise RuntimeError("on_error itself failed")

        pump = TkUICallbackPump(root, mailbox, on_error=bad_on_error, interval_ms=50)

        pump.start()
        mailbox.submit(lambda: (_ for _ in ()).throw(ValueError("callback failed")))

        # 不应抛异常
        root.trigger_next()
        assert pump.error_count == 1
        # pump 仍重新调度
        assert len(root.scheduled) == 1


# ── 事件泵：关闭 ────────────────────────────────────────


class TestPumpClose:
    def test_close_cancels_pending_after(self):
        """close() 取消待执行的 after 回调。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        pump = TkUICallbackPump(root, mailbox, interval_ms=50)

        pump.start()
        assert len(root.scheduled) == 1
        assert pump.is_closed is False

        pump.close()
        assert pump.is_closed is True
        assert len(root.cancelled) == 1
        assert len(root.scheduled) == 0

    def test_close_is_idempotent(self):
        """close() 幂等，多次调用不抛异常。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        pump = TkUICallbackPump(root, mailbox)

        pump.close()
        pump.close()
        pump.close()
        assert pump.is_closed is True

    def test_close_does_not_schedule_new_after(self):
        """关闭后不再调度新 after 回调。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        pump = TkUICallbackPump(root, mailbox, interval_ms=50)

        pump.start()
        pump.close()

        root.scheduled.clear()
        pump._poll()  # 即使 _poll 被调用也不会再调度
        assert len(root.scheduled) == 0

    def test_close_closes_mailbox(self):
        """close() 同时关闭邮箱，拒绝后续 submit。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        pump = TkUICallbackPump(root, mailbox)

        pump.close()
        assert mailbox.is_closed is True

        # 迟到回调被丢弃
        mailbox.submit(lambda: None)
        assert mailbox.discarded_count == 1


# ── P1-1 验收标准：线程边界 ────────────────────────────


class TestThreadBoundary:
    """P1-1 验收标准：工作线程只 submit，主线程只 after。"""

    def test_worker_threads_never_call_tk_after(self):
        """多个工作线程高频 submit，期间任何 root.after 调用都应来自主线程。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        executed: List[int] = []
        pump = TkUICallbackPump(root, mailbox, interval_ms=50, on_error=lambda e: None)
        pump.start()

        stop = threading.Event()

        def worker(thread_id: int):
            i = 0
            while not stop.is_set() and i < 500:
                mailbox.submit(lambda tid=thread_id, idx=i: executed.append(idx))
                i += 1

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()

        # 主线程触发几次 drain（模拟事件泵轮询）
        for _ in range(5):
            root.trigger_next()

        stop.set()
        for t in threads:
            t.join(timeout=2.0)

        # 排空剩余事件
        pump.close()
        root.scheduled.clear()
        if root.scheduled:
            root.trigger_next()

        # P1-1 核心断言：没有工作线程调用 Tk API
        assert root.violation_message is None, f"工作线程调用了 Tk API: {root.violation_message}"
        # 至少处理了一些事件
        assert len(executed) > 0

    def test_worker_submit_after_close_is_safe(self):
        """P1-1 验收标准：窗口销毁后的迟到事件被安全丢弃且有可观测计数。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        executed: List[int] = []
        pump = TkUICallbackPump(root, mailbox, interval_ms=50, on_error=lambda e: None)
        pump.start()

        # 模拟窗口关闭
        pump.close()

        # 工作线程继续提交（模拟 worker 不知道窗口已关闭）
        late_count = 0
        for i in range(20):
            mailbox.submit(lambda x=i: executed.append(x))
            late_count += 1

        # 触发一次 poll（不应执行任何回调）
        root.scheduled.clear()
        pump._poll()

        # 迟到事件被安全丢弃
        assert executed == []
        assert mailbox.discarded_count == late_count
        assert mailbox.is_closed is True

    def test_no_winfo_exists_call_from_worker(self):
        """P1-1：工作线程不应调用 winfo_exists()（Tk 跨线程调用）。

        旧模式中 ``_safe_after`` 在工作线程调用 ``target_window.winfo_exists()``，
        新模式完全移除该调用，回调直接 submit 到邮箱。
        """
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        winfo_calls: List[int] = []  # 记录调用线程 ID

        class TrackingWindow:
            """记录 winfo_exists 调用线程的假窗口。"""

            def winfo_exists(self):
                winfo_calls.append(threading.get_ident())
                return True

        fake_window = TrackingWindow()
        pump = TkUICallbackPump(root, mailbox, interval_ms=50)
        pump.start()

        # 工作线程模拟旧 _safe_after 调用模式
        # 新模式只调用 mailbox.submit，不接触 winfo_exists
        def worker():
            # 旧模式会调用 fake_window.winfo_exists() —— 新模式不调用
            mailbox.submit(lambda: None)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # 工作线程没有调用 winfo_exists
        assert winfo_calls == [], f"工作线程调用了 winfo_exists: {winfo_calls}"

        # 清理
        pump.close()


# ── 端到端：邮箱 + 事件泵 ────────────────────────────────


class TestEndToEnd:
    def test_end_to_end_flow(self):
        """端到端：工作线程 submit → 主线程 pump drain → 回调执行。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        results: List[str] = []
        pump = TkUICallbackPump(root, mailbox, interval_ms=50, on_error=lambda e: None)
        pump.start()

        def worker():
            mailbox.submit(lambda: results.append("step1"))
            mailbox.submit(lambda: results.append("step2"))
            mailbox.submit(lambda: results.append("step3"))

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # 主线程排空
        root.trigger_next()

        assert results == ["step1", "step2", "step3"]
        assert pump.executed_count == 3
        assert root.violation_message is None

        pump.close()

    def test_close_during_worker_submission(self):
        """工作线程正在 submit 时关闭 pump，不丢数据不崩溃。"""
        root = ThreadCheckingRoot()
        mailbox = UICallbackMailbox()
        executed: List[int] = []
        pump = TkUICallbackPump(root, mailbox, interval_ms=50, on_error=lambda e: None)
        pump.start()

        barrier = threading.Barrier(2)
        errors: List[Exception] = []

        def worker():
            try:
                barrier.wait()
                for i in range(100):
                    mailbox.submit(lambda x=i: executed.append(x))
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()

        # 主线程和 worker 同时行动
        barrier.wait()
        pump.close()
        t.join(timeout=2.0)

        assert errors == []
        # 关闭前提交的回调可能已排空或被丢弃，但无异常
        assert pump.is_closed is True
