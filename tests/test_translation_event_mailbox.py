#!/usr/bin/env python3
"""PERF：翻译事件邮箱和 Tk 事件泵的单元测试。

覆盖 TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md §6.7 的要求：
- 同批次流式事件合并
- 不同批次分别保留最新
- 终结事件不被覆盖
- 终结事件清除同批次流式状态
- 多线程并发 publish 不丢终结事件
- discard_run 只清理指定任务
- 事件泵关闭后不再调度
- 事件泵只在主线程调用 after
"""

from __future__ import annotations

import threading
from typing import List, Tuple

import pytest

from src.application.translation_events import (
    TranslationEventKind,
    TranslationProgressEvent,
)
from src.ui.tk_event_pump import TkTranslationEventPump
from src.ui.translation_event_mailbox import TranslationEventMailbox

# ── 测试辅助 ──────────────────────────────────────────────


def _make_event(
    run_id: str = "run-1",
    kind: TranslationEventKind = TranslationEventKind.STREAM,
    batch_start: int = 0,
    completed_lines: int = 0,
    **kwargs,
) -> TranslationProgressEvent:
    """构造测试用事件。"""
    return TranslationProgressEvent(
        run_id=run_id,
        kind=kind,
        batch_start=batch_start,
        expected_lines=kwargs.get("expected_lines", 10),
        completed_lines=completed_lines,
        overall_progress=kwargs.get("overall_progress", 0.0),
        stream_start_line=kwargs.get("stream_start_line", 0),
        stream_lines=kwargs.get("stream_lines", ()),
        translated_lines=kwargs.get("translated_lines", ()),
        display_batch_start=kwargs.get("display_batch_start"),
        message=kwargs.get("message"),
    )


# ── 邮箱：流式合并 ──────────────────────────────────────


def test_stream_events_for_same_batch_keep_only_latest():
    """10,000 个同批次流式事件排队后只保留最新一个。"""
    mailbox = TranslationEventMailbox()
    run_id = "run-latest"
    for i in range(10_000):
        mailbox.publish(
            _make_event(
                run_id=run_id,
                batch_start=0,
                completed_lines=i,
                stream_lines=(f"line-{i}",),
            )
        )

    terminal, stream = mailbox.drain()
    assert terminal == ()
    assert len(stream) == 1
    assert stream[0].completed_lines == 9_999
    assert stream[0].stream_lines == ("line-9999",)


def test_stream_events_for_different_batches_keep_respective_latest():
    """不同批次分别保留最新事件。"""
    mailbox = TranslationEventMailbox()
    for batch_start in (0, 10, 20):
        for i in range(5):
            mailbox.publish(
                _make_event(
                    batch_start=batch_start,
                    completed_lines=i,
                )
            )

    _, stream = mailbox.drain()
    assert len(stream) == 3
    batch_starts = {event.batch_start for event in stream}
    assert batch_starts == {0, 10, 20}
    # 每个批次都是最新的（completed_lines=4）
    assert all(event.completed_lines == 4 for event in stream)


def test_batch_completed_event_not_overwritten_by_stream_event():
    """批次完成事件不会被流式事件覆盖。

    BATCH_COMPLETED 进入终结队列，后续同批次 STREAM 不会清除它
    （因为 STREAM 只更新 _latest_stream，不影响 _terminal）。
    """
    mailbox = TranslationEventMailbox()
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.BATCH_COMPLETED,
            batch_start=0,
            translated_lines=("译文1", "译文2"),
        )
    )
    # 后到的流式事件不应清除已入队的终结事件
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.STREAM,
            batch_start=0,
            completed_lines=1,
        )
    )

    terminal, stream = mailbox.drain()
    # BATCH_COMPLETED 在终结队列
    assert len(terminal) == 1
    assert terminal[0].kind is TranslationEventKind.BATCH_COMPLETED
    # 同批次流式状态被 BATCH_COMPLETED 发布时清除，但新的 STREAM 又写入
    # 最新一轮 drain 会同时返回，但终结事件优先处理
    assert len(stream) == 1
    assert stream[0].kind is TranslationEventKind.STREAM


def test_terminal_event_clears_same_batch_stream_state():
    """终结事件发布后同批次中间状态被清除。"""
    mailbox = TranslationEventMailbox()
    # 先发布流式事件
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.STREAM,
            batch_start=0,
            completed_lines=5,
            stream_lines=("中间快照",),
        )
    )
    # 发布同批次终结事件
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.RUN_COMPLETED,
            batch_start=0,
            overall_progress=100.0,
        )
    )

    terminal, stream = mailbox.drain()
    assert len(terminal) == 1
    assert terminal[0].kind is TranslationEventKind.RUN_COMPLETED
    # 同批次流式状态已被清除
    assert stream == ()


def test_multiple_terminal_events_preserve_order():
    """多个终结事件保持发布顺序。"""
    mailbox = TranslationEventMailbox()
    for batch_start in (0, 10, 20):
        mailbox.publish(
            _make_event(
                kind=TranslationEventKind.BATCH_COMPLETED,
                batch_start=batch_start,
            )
        )

    terminal, _ = mailbox.drain()
    assert [e.batch_start for e in terminal] == [0, 10, 20]


def test_drain_clears_mailbox():
    """drain 后邮箱为空，再次 drain 返回空。"""
    mailbox = TranslationEventMailbox()
    # 不同批次，避免终结事件清除同批次流式状态
    mailbox.publish(_make_event(kind=TranslationEventKind.STREAM, batch_start=0))
    mailbox.publish(_make_event(kind=TranslationEventKind.BATCH_COMPLETED, batch_start=10))

    terminal1, stream1 = mailbox.drain()
    assert len(terminal1) == 1
    assert len(stream1) == 1

    terminal2, stream2 = mailbox.drain()
    assert terminal2 == ()
    assert stream2 == ()


# ── 邮箱：多线程并发 ────────────────────────────────────


def test_concurrent_publish_never_loses_terminal_events():
    """多线程并发 publish 不丢终结事件，不抛出异常。"""
    mailbox = TranslationEventMailbox()
    run_ids = [f"run-{i}" for i in range(8)]
    errors: List[Exception] = []

    def worker(run_id: str):
        try:
            # 每个线程发布 500 个流式事件 + 1 个终结事件
            for i in range(500):
                mailbox.publish(
                    _make_event(
                        run_id=run_id,
                        batch_start=0,
                        completed_lines=i,
                    )
                )
            mailbox.publish(
                _make_event(
                    run_id=run_id,
                    kind=TranslationEventKind.RUN_COMPLETED,
                    batch_start=0,
                    overall_progress=100.0,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in run_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"并发 publish 抛出异常: {errors}"

    # 收集所有 drain 的终结事件
    all_terminal: List[TranslationProgressEvent] = []
    while True:
        terminal, _ = mailbox.drain()
        if not terminal:
            break
        all_terminal.extend(terminal)

    # 每个 run 应该恰好有一个终结事件
    terminal_runs = [e.run_id for e in all_terminal if e.kind.is_terminal]
    assert sorted(terminal_runs) == sorted(run_ids), (
        f"终结事件丢失: 期望 {len(run_ids)} 个，实际 {len(terminal_runs)} 个"
    )


def test_concurrent_publish_from_same_run_keeps_one_stream_snapshot():
    """同一 run 多批次并发 publish，drain 后流式快照数等于批次数。"""
    mailbox = TranslationEventMailbox()
    run_id = "run-concurrent"
    batch_starts = list(range(0, 100, 10))  # 10 个批次
    barrier = threading.Barrier(len(batch_starts))

    def worker(batch_start: int):
        barrier.wait()
        for i in range(100):
            mailbox.publish(
                _make_event(
                    run_id=run_id,
                    batch_start=batch_start,
                    completed_lines=i,
                )
            )

    threads = [threading.Thread(target=worker, args=(bs,)) for bs in batch_starts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _, stream = mailbox.drain()
    # 10 个批次，每批保留 1 个最新流式快照
    assert len(stream) == 10
    stream_batches = {e.batch_start for e in stream}
    assert stream_batches == set(batch_starts)


# ── 邮箱：discard_run ──────────────────────────────────


def test_discard_run_only_clears_specified_run():
    """discard_run() 只清理指定任务，不影响其他任务。"""
    mailbox = TranslationEventMailbox()
    mailbox.publish(
        _make_event(
            run_id="old-run",
            batch_start=0,
            completed_lines=1,
        )
    )
    mailbox.publish(
        _make_event(
            run_id="old-run",
            kind=TranslationEventKind.BATCH_COMPLETED,
            batch_start=0,
        )
    )
    # new-run 使用不同 batch_start，避免终结事件清除同批次流式
    mailbox.publish(
        _make_event(
            run_id="new-run",
            batch_start=5,
            completed_lines=2,
        )
    )
    mailbox.publish(
        _make_event(
            run_id="new-run",
            kind=TranslationEventKind.RUN_COMPLETED,
            batch_start=10,
        )
    )

    mailbox.discard_run("old-run")

    terminal, stream = mailbox.drain()
    # 只剩 new-run 的事件
    assert all(e.run_id == "new-run" for e in terminal)
    assert all(e.run_id == "new-run" for e in stream)
    assert len(terminal) == 1
    assert len(stream) == 1


def test_discard_run_with_no_matching_events_is_noop():
    """discard_run 对不存在的 run_id 是 no-op。"""
    mailbox = TranslationEventMailbox()
    mailbox.publish(_make_event(run_id="run-1"))

    mailbox.discard_run("nonexistent-run")

    _, stream = mailbox.drain()
    assert len(stream) == 1
    assert stream[0].run_id == "run-1"


# ── 事件不可变性 ────────────────────────────────────────


def test_event_is_frozen():
    """frozen=True 防止事件被修改。"""
    event = _make_event(completed_lines=5)
    with pytest.raises(Exception):
        event.completed_lines = 10  # type: ignore[misc]


def test_event_has_slots():
    """slots=True 减少对象开销，且不能添加新属性。"""
    event = _make_event()
    with pytest.raises(AttributeError):
        event.new_field = "value"  # type: ignore[attr-defined]


# ── Tk 事件泵 ──────────────────────────────────────────


class ThreadCheckingRoot:
    """Fake Tk root：记录创建线程，断言 after/after_cancel 只在主线程调用。

    用于验证事件泵不会从工作线程调用 Tk API。
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
        # 匹配真实 Tk 行为：取消后从已调度列表移除
        self.scheduled = [(aid, i, c) for aid, i, c in self.scheduled if aid != after_id]

    def trigger_next(self):
        """触发下一个已调度的回调（按 FIFO）。"""
        if not self.scheduled:
            return
        _after_id, _interval, callback = self.scheduled.pop(0)
        callback()


def test_pump_starts_and_polls_on_main_thread():
    """事件泵启动后在主线程周期性排空邮箱。"""
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    rendered: List[TranslationProgressEvent] = []
    pump = TkTranslationEventPump(root, mailbox, rendered.append, interval_ms=50)

    pump.start()
    assert len(root.scheduled) == 1

    # 发布一个事件
    mailbox.publish(_make_event(completed_lines=1))

    # 触发 poll
    root.trigger_next()
    assert len(rendered) == 1
    assert rendered[0].completed_lines == 1
    # poll 后重新调度下一轮
    assert len(root.scheduled) == 1
    assert root.violation_message is None


def test_pump_close_cancels_pending_after():
    """close() 取消待执行的 after 回调。"""
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    pump = TkTranslationEventPump(root, mailbox, lambda _e: None, interval_ms=50)

    pump.start()
    assert len(root.scheduled) == 1
    assert pump.is_closed is False

    pump.close()
    assert pump.is_closed is True
    assert len(root.cancelled) == 1
    assert len(root.scheduled) == 0


def test_pump_close_is_idempotent():
    """close() 幂等，多次调用不抛出异常。"""
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    pump = TkTranslationEventPump(root, mailbox, lambda _e: None)

    pump.close()
    pump.close()  # 再次关闭不抛异常
    assert pump.is_closed is True


def test_pump_does_not_schedule_after_close():
    """事件泵关闭后不再调度新回调。"""
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    pump = TkTranslationEventPump(root, mailbox, lambda _e: None, interval_ms=50)

    pump.start()
    pump.close()

    # 清空已取消的调度，模拟 after_cancel 生效
    root.scheduled.clear()

    # 触发已调度的回调（如果有），不应再调度新回调
    # 由于 close 已设置 _closed=True，即使 _poll 被调用也不会再调度
    pump._poll()
    assert len(root.scheduled) == 0


def test_pump_terminal_events_processed_before_stream():
    """终结事件先于本轮残留流式事件处理。"""
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    rendered_order: List[TranslationEventKind] = []
    pump = TkTranslationEventPump(
        root,
        mailbox,
        lambda e: rendered_order.append(e.kind),
        interval_ms=50,
    )

    pump.start()
    # 先发布流式，再发布终结
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.STREAM,
            batch_start=0,
            completed_lines=5,
        )
    )
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.RUN_COMPLETED,
            batch_start=0,
        )
    )
    # 终结事件发布时已清除同批次流式状态
    # 但若终结后又来了流式（理论上不应发生），drain 仍保证终结先处理

    root.trigger_next()
    # 只剩终结事件（流式已被清除）
    assert rendered_order == [TranslationEventKind.RUN_COMPLETED]


def test_pump_start_is_idempotent():
    """start() 幂等：已启动时不重复调度。"""
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    pump = TkTranslationEventPump(root, mailbox, lambda _e: None, interval_ms=50)

    pump.start()
    pump.start()  # 重复调用不应再调度
    assert len(root.scheduled) == 1


def test_pump_render_receives_events_in_drain_order():
    """render 收到的事件顺序与 drain 返回顺序一致。"""
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    rendered: List[TranslationProgressEvent] = []
    pump = TkTranslationEventPump(root, mailbox, rendered.append, interval_ms=50)

    pump.start()
    # 三个批次的完成事件 + 两个批次的流式事件
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.BATCH_COMPLETED,
            batch_start=0,
        )
    )
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.BATCH_COMPLETED,
            batch_start=10,
        )
    )
    mailbox.publish(
        _make_event(
            kind=TranslationEventKind.STREAM,
            batch_start=20,
            completed_lines=1,
        )
    )

    root.trigger_next()
    # 终结事件（BATCH_COMPLETED）先处理，流式事件后处理
    assert len(rendered) == 3
    assert rendered[0].kind is TranslationEventKind.BATCH_COMPLETED
    assert rendered[0].batch_start == 0
    assert rendered[1].kind is TranslationEventKind.BATCH_COMPLETED
    assert rendered[1].batch_start == 10
    assert rendered[2].kind is TranslationEventKind.STREAM
    assert rendered[2].batch_start == 20


# ── 邮箱 + 事件泵端到端：并发场景 ────────────────────────


def test_worker_threads_only_publish_main_thread_only_after():
    """工作线程只调用 mailbox.publish，主线程只调用 root.after。

    验证：多个工作线程高频 publish，事件泵在主线程 drain，
    期间任何 root.after 调用都应来自主线程。
    """
    root = ThreadCheckingRoot()
    mailbox = TranslationEventMailbox()
    rendered: List[TranslationProgressEvent] = []
    pump = TkTranslationEventPump(root, mailbox, rendered.append, interval_ms=50)
    pump.start()

    run_id = "run-e2e"
    stop_event = threading.Event()

    def worker():
        i = 0
        while not stop_event.is_set() and i < 1000:
            mailbox.publish(
                _make_event(
                    run_id=run_id,
                    batch_start=i % 5,
                    completed_lines=i,
                )
            )
            i += 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()

    # 主线程触发几次 drain（模拟事件泵轮询）
    for _ in range(5):
        root.trigger_next()

    stop_event.set()
    for t in threads:
        t.join(timeout=2.0)

    # 排空剩余事件：关闭事件泵避免无限重调度，再触发最后一轮 poll
    pump.close()
    root.scheduled.clear()  # close 后清空待调度回调
    # 若仍有未消费的 poll 回调，触发一次以排空邮箱
    if root.scheduled:
        root.trigger_next()

    assert root.violation_message is None, f"工作线程调用了 Tk API: {root.violation_message}"
    # 至少处理了一些事件
    assert len(rendered) > 0
