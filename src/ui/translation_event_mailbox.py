#!/usr/bin/env python3
"""PERF：有界翻译事件邮箱。

后台工作线程 ``publish`` 事件，Tk 主线程 ``drain`` 排空邮箱。

合并策略（见 TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md §6.3）：
- 流式（STREAM）事件按 ``(run_id, batch_start)`` 合并，只保留每个批次最新一个，
  上界为活动批次数，而不是 chunk 数量。
- 终结事件（RUN_COMPLETED/RUN_FAILED/RUN_CANCELLED）和 BATCH_COMPLETED 事件
  进入 FIFO 队列，保序且不丢弃。
- 终结事件发布时自动清除同批次的残留流式状态。

线程模型：
- ``publish`` 可由多个工作线程并发调用。
- ``drain`` 和 ``discard_run`` 只由 Tk 主线程调用（与事件泵排空同步）。
"""

from __future__ import annotations

from collections import deque
from threading import Lock
from typing import Dict, Tuple

from ..application.translation_events import (
    TranslationEventKind,
    TranslationProgressEvent,
)

# 流式事件键：(run_id, batch_start)
_StreamKey = Tuple[str, int]


class TranslationEventMailbox:
    """有界合并邮箱：最新流式状态字典 + 终结事件队列。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._latest_stream: Dict[_StreamKey, TranslationProgressEvent] = {}
        self._terminal: deque[TranslationProgressEvent] = deque()

    def publish(self, event: TranslationProgressEvent) -> None:
        """发布事件。工作线程调用，线程安全。

        - STREAM 事件覆盖同批次之前的流式快照。
        - ``BATCH_COMPLETED`` 追加到 FIFO 队列，并清除同批次流式状态。
        - ``RUN_*`` 终态会清除该 run 的全部流式状态，避免其他批次的
          半成品预览在失败/取消后继续渲染。
        """
        key: _StreamKey = (event.run_id, event.batch_start)
        with self._lock:
            if event.kind is TranslationEventKind.STREAM:
                self._latest_stream[key] = event
                return
            if event.kind.is_terminal:
                self._latest_stream = {
                    stream_key: value
                    for stream_key, value in self._latest_stream.items()
                    if stream_key[0] != event.run_id
                }
            else:
                # BATCH_COMPLETED 清除同批次流式状态，避免终态处理后又
                # 被残留流式快照覆盖。
                self._latest_stream.pop(key, None)
            self._terminal.append(event)

    def drain(self) -> Tuple[
        Tuple[TranslationProgressEvent, ...],
        Tuple[TranslationProgressEvent, ...],
    ]:
        """排空邮箱，返回 (终结事件, 流式事件)。

        只由 Tk 主线程调用。
        终结事件先于本轮残留流式事件返回，确保终结状态不被中间快照覆盖。
        """
        with self._lock:
            terminal = tuple(self._terminal)
            self._terminal.clear()
            stream = tuple(self._latest_stream.values())
            self._latest_stream.clear()
        return terminal, stream

    def discard_run(self, run_id: str) -> None:
        """丢弃指定 run_id 的所有事件。

        新任务开始或窗口关闭时调用，避免旧任务的迟到事件污染新状态。
        """
        with self._lock:
            self._latest_stream = {
                key: value for key, value in self._latest_stream.items() if key[0] != run_id
            }
            self._terminal = deque(event for event in self._terminal if event.run_id != run_id)

    def pending_terminal_count(self) -> int:
        """诊断用：当前待处理的终结事件数量。"""
        with self._lock:
            return len(self._terminal)

    def pending_stream_count(self) -> int:
        """诊断用：当前待处理的流式快照数量（等于活动批次数上界）。"""
        with self._lock:
            return len(self._latest_stream)
