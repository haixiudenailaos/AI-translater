#!/usr/bin/env python3
"""P0-3：自动查漏补译 run_id 绑定测试。

覆盖审查文档 P0-3 的核心契约：
- ``_translate_missing_worker`` 接受 ``run_id`` 参数，与全文/选中行 worker 一致。
- 进度回调、终态回调、异常路径的 ``_publish_run_failed`` 都正确绑定 ``run_id``。
- 旧查漏 run 的迟到事件不能覆盖新任务（由 ``_render_event`` 的 run_id 校验保证）。
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from src.application.translation_events import (
    TranslationEventKind,
    TranslationProgressEvent,
)
from src.core.translation_result import BatchTranslationResult, TranslationStatus
from src.ui.translation_controller import TranslationController


# ── 测试桩 ──────────────────────────────────────────────


class _FakeTranslator:
    """捕获 translate_fast_mode 的回调，便于在测试中触发。"""

    def __init__(self):
        self.progress_cb = None
        self.terminal_cb = None
        self.raise_exc: Exception | None = None

    def translate_fast_mode(self, content, progress_cb, terminal_cb):
        self.progress_cb = progress_cb
        self.terminal_cb = terminal_cb
        if self.raise_exc is not None:
            raise self.raise_exc


class _MailboxSpy:
    """记录所有 publish 调用，便于断言事件携带的 run_id。"""

    def __init__(self):
        self.events: list[TranslationProgressEvent] = []

    def publish(self, event):
        self.events.append(event)


def _make_controller_for_worker(translator, mailbox):
    """构造仅含必要属性的控制器，绕过 Tk 初始化。"""
    controller = TranslationController.__new__(TranslationController)
    controller.translator = translator
    controller._mailbox = mailbox
    controller._pending_results = {}
    controller._results_lock = threading.Lock()
    controller._document = None
    return controller


# ── worker 签名 ─────────────────────────────────────────


def test_missing_worker_accepts_run_id_argument():
    """P0-3：_translate_missing_worker 接受 (content, run_id) 两参数。

    与 _translate_worker / _translate_selected_worker 保持同一签名，
    避免回调签名再次漂移。
    """
    import inspect

    sig = inspect.signature(TranslationController._translate_missing_worker)
    params = list(sig.parameters.keys())
    # 第一个参数是 self，后续应为 content, run_id
    assert params == ["self", "content", "run_id"], (
        f"_translate_missing_worker 签名应为 (self, content, run_id)，实际为 {params}"
    )


# ── 成功路径：run_id 通过闭包绑定到回调 ─────────────────


def test_missing_worker_success_binds_run_id_to_callbacks():
    """P0-3：查漏 worker 成功路径把 run_id 通过闭包绑定到所有回调。

    验证 _publish_progress_event 和 _publish_terminal_event 在被调用时
    携带 worker 启动时的 run_id，而非读取可变的 self._current_run_id。
    """
    translator = _FakeTranslator()
    mailbox = _MailboxSpy()
    controller = _make_controller_for_worker(translator, mailbox)

    run_id = "missing-run-1"
    controller._translate_missing_worker("content", run_id)

    # 触发一次进度回调
    assert translator.progress_cb is not None
    translator.progress_cb(50, {"batch_start": 0, "streaming": False, "translated_lines": ["x"]})

    # 触发终态回调
    assert translator.terminal_cb is not None
    result = BatchTranslationResult(
        status=TranslationStatus.SUCCEEDED,
        lines=["x"],
        failed_indices=[],
    )
    translator.terminal_cb(result)

    # 所有事件都应携带正确的 run_id
    assert len(mailbox.events) == 2  # 进度 + 终态
    for event in mailbox.events:
        assert event.run_id == run_id, (
            f"事件 run_id 应为 {run_id}，实际为 {event.run_id}"
        )


def test_missing_worker_streaming_progress_binds_run_id():
    """P0-3：查漏 worker 流式进度事件也绑定 run_id。"""
    translator = _FakeTranslator()
    mailbox = _MailboxSpy()
    controller = _make_controller_for_worker(translator, mailbox)

    run_id = "missing-stream-1"
    controller._translate_missing_worker("content", run_id)

    translator.progress_cb(
        30,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["流式A", "流式B"],
        },
    )

    assert len(mailbox.events) == 1
    event = mailbox.events[0]
    assert event.run_id == run_id
    assert event.kind is TranslationEventKind.STREAM


# ── 异常路径：_publish_run_failed 绑定 run_id ────────────


def test_missing_worker_exception_publishes_run_failed_with_run_id():
    """P0-3：查漏 worker 异常时通过 _publish_run_failed 发布 RUN_FAILED 事件，
    且事件携带正确的 run_id。

    这是 P0-3 的核心修复点：原实现漏传 run_id，导致异常路径再次
    产生 TypeError，终态事件无法发布。
    """
    translator = _FakeTranslator()
    translator.raise_exc = RuntimeError("模拟翻译失败")
    mailbox = _MailboxSpy()
    controller = _make_controller_for_worker(translator, mailbox)

    run_id = "missing-fail-1"
    # 不应抛异常（worker 应捕获并发布 RUN_FAILED）
    controller._translate_missing_worker("content", run_id)

    # 应发布一个 RUN_FAILED 事件，携带正确 run_id
    assert len(mailbox.events) == 1
    event = mailbox.events[0]
    assert event.run_id == run_id
    assert event.kind is TranslationEventKind.RUN_FAILED
    assert "模拟翻译失败" in (event.message or "")


# ── 旧 run 的迟到事件被丢弃 ────────────────────────────


def test_render_event_drops_stale_run_events():
    """P0-3：旧查漏 run 的迟到事件不能覆盖新任务。

    _render_event 在 run_id 不匹配时直接返回，不调用任何渲染方法。
    此契约由 _current_run_id 校验保证，与 worker 类型无关。
    """
    controller = TranslationController.__new__(TranslationController)
    controller._current_run_id = "new-run"
    controller._current_mode = "missing"
    controller._streaming_preview_rows = set()

    # 构造一个旧 run 的事件
    stale_event = TranslationProgressEvent(
        run_id="old-run",
        kind=TranslationEventKind.BATCH_COMPLETED,
        batch_start=0,
        expected_lines=1,
        completed_lines=1,
        overall_progress=100.0,
        stream_start_line=0,
        stream_lines=(),
        translated_lines=("迟到译文",),
        display_batch_start=None,
        message=None,
    )

    # 用 mock 验证不调用任何 _dispatch_* 方法
    controller._dispatch_stream_event = MagicMock()
    controller._dispatch_batch_completed_event = MagicMock()
    controller._dispatch_terminal_event = MagicMock()

    controller._render_event(stale_event)

    # 所有分发方法都不应被调用
    controller._dispatch_stream_event.assert_not_called()
    controller._dispatch_batch_completed_event.assert_not_called()
    controller._dispatch_terminal_event.assert_not_called()


def test_render_event_accepts_current_run_events():
    """P0-3：当前 run 的事件正常分发到对应处理方法。"""
    controller = TranslationController.__new__(TranslationController)
    controller._current_run_id = "current-run"
    controller._current_mode = "missing"
    controller._streaming_preview_rows = set()

    event = TranslationProgressEvent(
        run_id="current-run",
        kind=TranslationEventKind.BATCH_COMPLETED,
        batch_start=0,
        expected_lines=1,
        completed_lines=1,
        overall_progress=100.0,
        stream_start_line=0,
        stream_lines=(),
        translated_lines=("译文",),
        display_batch_start=None,
        message=None,
    )

    controller._dispatch_stream_event = MagicMock()
    controller._dispatch_batch_completed_event = MagicMock()
    controller._dispatch_terminal_event = MagicMock()

    controller._render_event(event)

    # BATCH_COMPLETED 应分发到对应方法
    controller._dispatch_batch_completed_event.assert_called_once_with(event)
    controller._dispatch_stream_event.assert_not_called()
    controller._dispatch_terminal_event.assert_not_called()


# ── 三个 worker 共用同一回调模式 ────────────────────────


def test_three_workers_share_same_callback_signature_pattern():
    """P0-3：全文、选中行、查漏三个 worker 共用同一回调模式。

    验证三个 worker 都接受 (content, run_id) 参数，都使用闭包绑定 run_id
    到 _publish_progress_event / _publish_terminal_event / _publish_run_failed。
    """
    import inspect

    full_sig = inspect.signature(TranslationController._translate_worker)
    selected_sig = inspect.signature(TranslationController._translate_selected_worker)
    missing_sig = inspect.signature(TranslationController._translate_missing_worker)

    full_params = list(full_sig.parameters.keys())
    selected_params = list(selected_sig.parameters.keys())
    missing_params = list(missing_sig.parameters.keys())

    expected = ["self", "content", "run_id"]
    assert full_params == expected, f"全文 worker 签名应为 {expected}，实际为 {full_params}"
    assert selected_params == expected, f"选中行 worker 签名应为 {expected}，实际为 {selected_params}"
    assert missing_params == expected, f"查漏 worker 签名应为 {expected}，实际为 {missing_params}"


# ── 终态结果通过 run_id 取回 ───────────────────────────


def test_terminal_event_stores_result_by_run_id():
    """P0-3：终态事件通过 run_id 暂存结果，主线程通过同一 run_id 取回。

    _publish_terminal_event 把结果存入 _pending_results[run_id]，
    _take_pending_result(run_id) 取回并移除。
    """
    translator = _FakeTranslator()
    mailbox = _MailboxSpy()
    controller = _make_controller_for_worker(translator, mailbox)

    run_id = "missing-result-1"
    controller._translate_missing_worker("content", run_id)

    result = BatchTranslationResult(
        status=TranslationStatus.SUCCEEDED,
        lines=["x"],
        failed_indices=[],
    )
    translator.terminal_cb(result)

    # 结果通过 run_id 取回
    retrieved = controller._take_pending_result(run_id)
    assert retrieved is result
    # 再次取回应为 None
    assert controller._take_pending_result(run_id) is None


# ── 线程安全：worker 在子线程执行不读取可变 run_id ────


def test_missing_worker_thread_safe_run_id_binding():
    """P0-3：worker 在子线程执行时，run_id 通过参数传入，不依赖 self._current_run_id。

    模拟主线程立即启动新 run（_current_run_id 已变），
    旧 worker 的回调仍应携带旧 run_id。
    """
    translator = _FakeTranslator()
    mailbox = _MailboxSpy()
    controller = _make_controller_for_worker(translator, mailbox)

    old_run_id = "missing-old"
    controller._current_run_id = old_run_id

    # 在子线程中启动 worker
    worker_thread = threading.Thread(
        target=controller._translate_missing_worker,
        args=("content", old_run_id),
    )
    worker_thread.start()
    worker_thread.join()

    # 主线程立即启动新 run（_current_run_id 变化）
    controller._current_run_id = "new-run-after-missing"

    # 旧 worker 的回调仍应携带旧 run_id
    translator.progress_cb(50, {"batch_start": 0, "streaming": False, "translated_lines": ["x"]})

    assert len(mailbox.events) == 1
    assert mailbox.events[0].run_id == old_run_id, (
        "旧 worker 回调应携带启动时的 run_id，而非可变的 self._current_run_id"
    )
