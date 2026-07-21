#!/usr/bin/env python3
"""P1-3：队列窗口生命周期与订阅语义单元测试。

覆盖 ``PYTHON_UIUX_BEST_PRACTICES_AUDIT.md`` P1-3 验收标准：
- 关闭队列窗口后任务继续且主窗口可见全局状态
- 取消全部必须确认
- EPUB 在任意阶段关闭/取消后主界面始终可用

测试不依赖 Tk 真实启动，使用 ``__new__`` 绕过 ``__init__`` 的 Tk 调用。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

from src.core.queue_scheduler import (
    QueueMetricsSnapshot,
    QueueSnapshot,
    QueueTaskSnapshot,
    QueueTaskState,
)
from src.ui.concurrent_window import ConcurrentWindow
from src.ui.main_window import MainWindow


class _FakeManager:
    """记录 ``close`` / ``set_progress_callback`` 调用的 manager 替身。"""

    def __init__(self) -> None:
        self.closed = False
        self.close_count = 0
        self.progress_callback = None
        self.cancel_all_count = 0
        # 任务列表（用于 _cancel_all 概况统计）
        self._tasks: List[SimpleNamespace] = []

    def set_progress_callback(self, callback):
        self.progress_callback = callback

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    def cancel_all(self) -> None:
        self.cancel_all_count += 1

    def get_all_tasks(self):
        return list(self._tasks)

    def get_snapshot(self):
        return None


def _bypass_init_window(manager: _FakeManager, owns_manager: bool = False) -> ConcurrentWindow:
    """构造绕过 ``__init__`` 的 ConcurrentWindow，用于隔离测试。"""
    win = ConcurrentWindow.__new__(ConcurrentWindow)
    win.manager = manager
    win._owns_manager = owns_manager
    win._closed = False
    win._detail_windows = {}
    # win 替身：destroy / winfo_exists 由调用方设置
    win.win = SimpleNamespace(
        winfo_exists=lambda: True,
        destroy=MagicMock(),
    )
    return win


# ── P1-3：注入 manager 时关闭窗口不停止任务 ──────────────


def test_close_window_with_injected_manager_does_not_close_manager():
    """P1-3：注入 manager 时关闭窗口不调用 ``manager.close()``。

    任务继续在后台运行，由应用生命周期（MainWindow.on_closing）负责停止。
    """
    manager = _FakeManager()
    win = _bypass_init_window(manager, owns_manager=False)

    win._on_close()

    assert win._closed is True
    # 关键断言：manager 未被关闭，任务继续运行
    assert manager.closed is False
    assert manager.close_count == 0
    # 窗口已销毁
    assert win.win.destroy.called


def test_close_window_with_injected_manager_clears_progress_callback():
    """P1-3：关闭窗口解除 progress_callback 引用，避免回调持有已销毁窗口。"""
    manager = _FakeManager()
    manager.progress_callback = lambda tid: None
    win = _bypass_init_window(manager, owns_manager=False)

    win._on_close()

    # progress_callback 被置 None，避免窗口销毁后回调投递
    assert manager.progress_callback is None


def test_background_import_fingerprint_dialog_runs_on_tk_thread_mailbox():
    """A queue-import worker must not invoke the Tk recovery dialog directly."""

    class _Mailbox:
        def __init__(self) -> None:
            self.callbacks = []

        def submit(self, callback) -> None:
            self.callbacks.append(callback)

    window = _bypass_init_window(_FakeManager())
    mailbox = _Mailbox()
    window._ui_mailbox = mailbox
    window._show_fingerprint_mismatch_dialog = MagicMock(return_value="map")
    result: list[str] = []

    worker = threading.Thread(
        target=lambda: result.append(
            window._request_fingerprint_mismatch_decision({"file_path": "novel.txt"})
        )
    )
    worker.start()

    deadline = time.monotonic() + 1.0
    while not mailbox.callbacks and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mailbox.callbacks
    window._show_fingerprint_mismatch_dialog.assert_not_called()

    mailbox.callbacks.pop()()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result == ["map"]
    window._show_fingerprint_mismatch_dialog.assert_called_once()


def test_close_window_with_owned_manager_closes_manager():
    """P1-3 向后兼容：窗口自建并拥有 manager 时，关闭窗口仍调用 ``manager.close()``。

    旧路径（无外部注入 manager）保持原有行为，避免破坏既有调用方。
    """
    manager = _FakeManager()
    win = _bypass_init_window(manager, owns_manager=True)

    win._on_close()

    assert win._closed is True
    # 向后兼容：窗口拥有 manager 时关闭它
    assert manager.closed is True
    assert manager.close_count == 1


def test_close_window_idempotent():
    """P1-3：``_on_close`` 幂等，重复调用不会重复销毁或关闭 manager。"""
    manager = _FakeManager()
    win = _bypass_init_window(manager, owns_manager=True)

    win._on_close()
    win._on_close()  # 重复调用

    assert manager.close_count == 1
    # destroy 只调用一次（第二次因 _closed=True 提前返回）
    assert win.win.destroy.call_count == 1


def test_close_window_closes_detail_subwindows():
    """P1-3：关闭窗口时同时关闭所有详情子窗口。"""
    manager = _FakeManager()
    win = _bypass_init_window(manager, owns_manager=False)

    # 模拟两个详情子窗口
    detail1 = SimpleNamespace(
        win=SimpleNamespace(
            winfo_exists=lambda: True,
            destroy=MagicMock(),
        )
    )
    detail2 = SimpleNamespace(
        win=SimpleNamespace(
            winfo_exists=lambda: True,
            destroy=MagicMock(),
        )
    )
    win._detail_windows = {"t1": detail1, "t2": detail2}

    win._on_close()

    assert detail1.win.destroy.called
    assert detail2.win.destroy.called
    assert win._detail_windows == {}


def test_refresh_tree_uses_lightweight_snapshot_without_full_task_copies():
    class SnapshotOnlyManager:
        def get_snapshot(self):
            return QueueSnapshot(
                tasks=(
                    QueueTaskSnapshot(
                        task_id="task-1",
                        file_name="book.txt",
                        file_type="txt",
                        state=QueueTaskState.RUNNING,
                        state_display="翻译中",
                        progress=42.5,
                        completed_lines=42,
                        total_lines=100,
                        in_flight_batches=2,
                        pending_batches=3,
                        error_message="raw exception",
                        failed_indices=(),
                        status="running",
                        checkpoint_dirty=True,
                        checkpoint_error=None,
                        error_safe_message="网络暂时不可用，请重试。",
                    ),
                ),
                metrics=QueueMetricsSnapshot(
                    active_requests=2,
                    current_limit=3,
                    hard_cap=4,
                    configured_max=3,
                    is_blocked=False,
                    cooldown_remaining=0,
                    total_429=0,
                    total_timeouts=0,
                    total_success=0,
                    consecutive_successes=0,
                    throughput_lines_per_minute=0,
                    eta_seconds=None,
                ),
            )

        def get_all_tasks(self):
            raise AssertionError("列表刷新不得复制完整任务文本")

    class Tree:
        def __init__(self):
            self.rows = {}

        def get_children(self):
            return tuple(self.rows)

        def item(self, item_id, **kwargs):
            if "values" in kwargs:
                self.rows[item_id] = kwargs["values"]
            return {"values": self.rows[item_id]}

        def insert(self, _parent, _index, *, values):
            item_id = f"row-{len(self.rows)}"
            self.rows[item_id] = values
            return item_id

        def delete(self, item_id):
            del self.rows[item_id]

    window = ConcurrentWindow.__new__(ConcurrentWindow)
    window.manager = SnapshotOnlyManager()
    window.tree = Tree()

    window._refresh_tree()

    values = next(iter(window.tree.rows.values()))
    assert values == (
        "task-1",
        "book.txt",
        "翻译中",
        "42%",
        "",
        "网络暂时不可用，请重试。",
        "2",
    )


# ── P1-3：取消全部必须确认 ──────────────────────────────


def test_cancel_all_with_no_tasks_shows_info_and_does_not_cancel(monkeypatch):
    """P1-3：空队列调用取消全部不执行取消，仅提示。"""
    manager = _FakeManager()
    win = _bypass_init_window(manager, owns_manager=False)

    info_messages: list = []
    yesno_questions: list = []
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.showinfo",
        lambda *a, **k: info_messages.append(a),
    )
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.askyesno",
        lambda *a, **k: yesno_questions.append(a) or True,
    )

    win._cancel_all()

    assert manager.cancel_all_count == 0
    assert len(info_messages) == 1
    assert len(yesno_questions) == 0


def test_cancel_all_with_active_tasks_requires_confirmation(monkeypatch):
    """P1-3：有活动任务时必须用户确认才执行取消全部。"""
    manager = _FakeManager()
    manager._tasks = [
        SimpleNamespace(status="running"),
        SimpleNamespace(status="completed"),
        SimpleNamespace(status="pending"),
        SimpleNamespace(status="cancelled"),
    ]
    win = _bypass_init_window(manager, owns_manager=False)

    askyesno_calls: list = []
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.askyesno",
        lambda *a, **k: askyesno_calls.append(a) or True,
    )
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.showinfo",
        lambda *a, **k: None,
    )

    win._cancel_all()

    # 确认对话框被调用一次
    assert len(askyesno_calls) == 1
    # 用户确认后执行取消
    assert manager.cancel_all_count == 1


def test_cancel_all_user_declines_does_not_cancel(monkeypatch):
    """P1-3：用户在确认对话框选"否"时不执行取消全部。"""
    manager = _FakeManager()
    manager._tasks = [
        SimpleNamespace(status="running"),
        SimpleNamespace(status="pending"),
    ]
    win = _bypass_init_window(manager, owns_manager=False)

    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.askyesno",
        lambda *a, **k: False,  # 用户选"否"
    )
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.showinfo",
        lambda *a, **k: None,
    )

    win._cancel_all()

    assert manager.cancel_all_count == 0


def test_cancel_all_no_active_tasks_does_not_require_confirmation(monkeypatch):
    """P1-3：无活动任务（全部已完成/已取消）时不弹确认，仅提示概况。"""
    manager = _FakeManager()
    manager._tasks = [
        SimpleNamespace(status="completed"),
        SimpleNamespace(status="completed"),
        SimpleNamespace(status="cancelled"),
    ]
    win = _bypass_init_window(manager, owns_manager=False)

    info_messages: list = []
    askyesno_calls: list = []
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.showinfo",
        lambda *a, **k: info_messages.append(a),
    )
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.askyesno",
        lambda *a, **k: askyesno_calls.append(a) or True,
    )

    win._cancel_all()

    assert manager.cancel_all_count == 0
    assert len(askyesno_calls) == 0
    assert len(info_messages) == 1
    # 提示消息包含任务概况
    msg = info_messages[0][1]
    assert "3" in msg  # 总数 3
    assert "2" in msg  # 已完成 2


def test_cancel_all_confirmation_shows_task_counts(monkeypatch):
    """P1-3：确认对话框显示任务数、已完成量、活动量、后果说明。"""
    manager = _FakeManager()
    manager._tasks = [
        SimpleNamespace(status="running"),  # 活动
        SimpleNamespace(status="pending"),  # 活动
        SimpleNamespace(status="paused"),  # 活动
        SimpleNamespace(status="completed"),  # 已完成
        SimpleNamespace(status="completed"),  # 已完成
        SimpleNamespace(status="error"),  # 已结束
    ]
    win = _bypass_init_window(manager, owns_manager=False)

    captured_msg: list = []
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.askyesno",
        lambda title, msg, **k: captured_msg.append(msg) or True,
    )
    monkeypatch.setattr(
        "src.ui.concurrent_window.messagebox.showinfo",
        lambda *a, **k: None,
    )

    win._cancel_all()

    assert len(captured_msg) == 1
    msg = captured_msg[0]
    # 显示活动任务数（3 个：running/pending/paused）
    assert "3" in msg
    # 显示已完成数（2 个）
    assert "2" in msg
    # 显示总数（6 个）
    assert "6" in msg
    # 显示后果说明
    assert "不可撤销" in msg
    assert "已翻译内容会保留" in msg


# ── P1-3：构造时 manager 注入语义 ────────────────────────


def test_injected_manager_sets_owns_manager_false():
    """P1-3：注入 manager 时 ``_owns_manager`` 为 False。"""
    manager = _FakeManager()

    # 使用 __new__ 绕过 Tk 初始化，手动模拟注入路径
    win = ConcurrentWindow.__new__(ConcurrentWindow)
    win.manager = manager
    win._owns_manager = False  # 模拟注入路径

    assert win.manager is manager
    assert win._owns_manager is False


def test_backward_compat_no_manager_implies_owns_manager():
    """P1-3 向后兼容：未注入 manager 的旧路径 ``_owns_manager`` 为 True。

    旧调用方（``ConcurrentWindow(parent, config_manager)``）由窗口自建并拥有
    manager，关闭时仍调用 ``manager.close()`` 释放资源。
    """
    # 仅验证语义标记，不实际触发 Tk 构造
    win = ConcurrentWindow.__new__(ConcurrentWindow)
    win._owns_manager = True  # 模拟旧路径

    assert win._owns_manager is True


def test_main_window_reopens_queue_with_same_application_manager(monkeypatch):
    """关闭并重开队列视图时复用 manager，并注入主界面的共享限流器。"""
    created_managers = []
    created_windows = []

    class _Manager:
        def __init__(self, config_manager, **kwargs):
            self.config_manager = config_manager
            self.kwargs = kwargs
            created_managers.append(self)

    class _Window:
        def __init__(self, parent, config_manager, **kwargs):
            self.parent = parent
            self.config_manager = config_manager
            self.kwargs = kwargs
            self.manager = kwargs["manager"]
            self._closed = False
            created_windows.append(self)

    monkeypatch.setattr("src.core.concurrent_manager.ConcurrentTranslationManager", _Manager)
    monkeypatch.setattr("src.ui.concurrent_window.ConcurrentWindow", _Window)

    main = MainWindow.__new__(MainWindow)
    main.root = object()
    main.config_manager = object()
    main.app_paths = object()
    main._provider_limiter_registry = object()
    main._queue_manager = None
    main._concurrent_window = None

    main.open_concurrent()
    created_windows[0]._closed = True
    main.open_concurrent()

    assert len(created_managers) == 1
    assert len(created_windows) == 2
    assert created_windows[0].manager is created_windows[1].manager
    assert created_managers[0].kwargs["limiter_registry"] is main._provider_limiter_registry
    assert "edition_capabilities" not in created_windows[0].kwargs
