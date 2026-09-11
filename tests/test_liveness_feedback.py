#!/usr/bin/env python3
"""长请求存活反馈单元测试。

诊断依据：线程栈显示两个批次线程正常阻塞在 ``ssl._sslobj.read()`` 等待
DeepSeek 生成 token，13 秒后请求正常返回 200。即翻译并未卡死，但状态栏
在整个静默期内不变，用户无法区分"正在等模型"和"程序卡死"。

这些测试锁定存活提示的行为，不依赖真实 Tk 主循环。
"""

from __future__ import annotations

from src.ui.translation_controller import TranslationController


class FakeRoot:
    """记录 after/after_cancel 调用的最小 Tk root 替身。"""

    def __init__(self):
        self.scheduled: dict[str, tuple[int, object]] = {}
        self.cancelled: list[str] = []
        self._next = 0

    def after(self, delay_ms, callback):
        self._next += 1
        token = f"after#{self._next}"
        self.scheduled[token] = (delay_ms, callback)
        return token

    def after_cancel(self, token):
        self.cancelled.append(token)
        self.scheduled.pop(token, None)


def _make_controller(monkeypatch, *, now: float = 1000.0):
    """构造只装配存活提示所需状态的控制器。"""
    controller = TranslationController.__new__(TranslationController)
    controller.root = FakeRoot()
    controller.is_translating = False
    controller._last_stream_activity_at = None
    controller._liveness_after_id = None
    controller._liveness_base_message = ""
    messages: list[str] = []
    controller.status_updater = messages.append
    clock = {"now": now}
    monkeypatch.setattr("src.ui.translation_controller.time.monotonic", lambda: clock["now"])
    return controller, messages, clock


def test_ticker_reports_elapsed_wait_after_a_silent_period(monkeypatch):
    controller, messages, clock = _make_controller(monkeypatch)
    controller.is_translating = True
    controller._start_liveness_ticker("正在翻译...")

    # 刚开始：不显示秒数，避免抖动。
    assert messages == ["正在翻译..."]

    # 静默 12 秒后应显示等待时长，让用户看到程序仍在运行。
    clock["now"] += 12
    controller._tick_liveness()

    assert messages[-1] == "正在翻译...（等待模型响应 12s）"


def test_stream_activity_resets_the_elapsed_counter(monkeypatch):
    controller, messages, clock = _make_controller(monkeypatch)
    controller.is_translating = True
    controller._start_liveness_ticker("正在翻译...")

    clock["now"] += 30
    controller._tick_liveness()
    assert "30s" in messages[-1]

    # 收到流式数据：计时归零，回到不带秒数的基础文案。
    controller._note_stream_activity()
    controller._tick_liveness()
    assert messages[-1] == "正在翻译..."


def test_ticker_stops_when_translation_finishes(monkeypatch):
    controller, _messages, clock = _make_controller(monkeypatch)
    controller.is_translating = True
    controller._start_liveness_ticker("正在翻译...")
    assert controller._liveness_after_id is not None

    controller.is_translating = False
    controller._tick_liveness()

    # 结束后不得再调度下一帧，否则 after 回调会永久续期。
    assert controller._liveness_after_id is None


def test_stop_is_idempotent_and_cancels_the_pending_callback(monkeypatch):
    controller, _messages, _clock = _make_controller(monkeypatch)
    controller.is_translating = True
    controller._start_liveness_ticker("正在翻译...")
    token = controller._liveness_after_id

    controller._stop_liveness_ticker()
    controller._stop_liveness_ticker()

    assert controller._liveness_after_id is None
    assert token in controller.root.cancelled


def test_restarting_a_run_does_not_leak_the_previous_callback(monkeypatch):
    controller, _messages, _clock = _make_controller(monkeypatch)
    controller.is_translating = True
    controller._start_liveness_ticker("正在翻译...")
    first = controller._liveness_after_id

    controller._start_liveness_ticker("正在重新翻译...")

    assert first in controller.root.cancelled
    assert controller._liveness_after_id != first
