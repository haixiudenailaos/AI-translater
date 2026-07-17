#!/usr/bin/env python3
"""PERF：Tk 主线程事件泵。

按固定间隔（默认 50ms，对应 20 次/秒）在 Tk 主线程排空事件邮箱并驱动渲染。

约束（见 TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md §6.4）：
- ``start`` / ``_poll`` / ``close`` 只由 Tk 主线程调用。
- 工作线程只能调用 ``mailbox.publish()``。
- 终结事件先于本轮残留流式事件处理；终结事件发布时已经移除同批次流式状态。
- ``close`` 必须由 ``TranslationController.close()`` 在窗口关闭时调用，
  并确保 ``run_id`` 已失效。

事件泵取代了旧的 ``TkUpdateCoalescer``：
- 旧方案在工作线程中调用 ``root.after()``（跨线程 Tk 调用）。
- 新方案工作线程只调用 ``mailbox.publish()``，只有事件泵在主线程调用 ``after``。
"""

from __future__ import annotations

from typing import Callable

from ..application.translation_events import TranslationProgressEvent
from .translation_event_mailbox import TranslationEventMailbox


class TkTranslationEventPump:
    """Tk 主线程事件泵：周期性排空邮箱并调用渲染回调。"""

    def __init__(
        self,
        root,
        mailbox: TranslationEventMailbox,
        render: Callable[[TranslationProgressEvent], None],
        interval_ms: int = 50,
    ) -> None:
        self._root = root
        self._mailbox = mailbox
        self._render = render
        # 默认 50ms 对应 20 次/秒，满足"不超过 25 次/秒"的验收标准。
        self._interval_ms = interval_ms
        self._after_id = None
        self._closed = False

    def start(self) -> None:
        """启动事件泵。幂等：已启动或已关闭时不再调度。"""
        if self._closed or self._after_id is not None:
            return
        self._after_id = self._root.after(self._interval_ms, self._poll)

    def _poll(self) -> None:
        """排空邮箱并渲染。只在 Tk 主线程执行。"""
        self._after_id = None
        if self._closed:
            return
        terminal, stream = self._mailbox.drain()
        # 终结事件先处理，确保完成/失败/取消状态优先于中间快照。
        for event in terminal:
            self._render(event)
        for event in stream:
            self._render(event)
        # 继续下一轮调度。
        self.start()

    def close(self) -> None:
        """关闭事件泵，取消待执行的 after 回调。幂等。"""
        self._closed = True
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except Exception:
                # Tk 可能已被销毁，忽略取消失败。
                pass
            self._after_id = None

    @property
    def is_closed(self) -> bool:
        """事件泵是否已关闭。"""
        return self._closed
