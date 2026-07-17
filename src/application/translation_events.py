#!/usr/bin/env python3
"""PERF：结构化翻译事件。

后台工作线程不再直接调用 ``root.after()`` 或修改 Tk 控件，而是发布
``TranslationProgressEvent`` 到事件邮箱。Tk 主线程的事件泵排空邮箱并
驱动渲染。

设计要点（见 TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md §6.2）：
- ``frozen=True`` 防止事件在发布后被其他线程修改。
- ``slots=True`` 减少大量短生命周期事件的对象开销。
- 不包含 widget、回调、异常对象、HTTP 响应或 API Key。
- 终结事件携带用户可展示的脱敏消息，完整异常只写日志。
- ``run_id`` 用于丢弃旧任务和窗口关闭后的迟到事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TranslationEventKind(str, Enum):
    """事件类型。

    - STREAM：流式中间事件（高频，可被同批次最新事件覆盖）。
    - BATCH_COMPLETED：批次完成事件（保序，不覆盖）。
    - RUN_COMPLETED / RUN_FAILED / RUN_CANCELLED：终结事件（保序，不得丢弃）。
    """

    STREAM = "stream"
    BATCH_COMPLETED = "batch_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"

    @property
    def is_terminal(self) -> bool:
        """是否为终结事件（完成、失败或取消）。"""
        return self in (
            TranslationEventKind.RUN_COMPLETED,
            TranslationEventKind.RUN_FAILED,
            TranslationEventKind.RUN_CANCELLED,
        )


@dataclass(frozen=True, slots=True)
class TranslationProgressEvent:
    """不可变翻译进度事件。

    所有字段都是纯 Python 类型（str/int/float/tuple/None），
    不携带 widget、回调、异常或 HTTP 响应。
    """

    run_id: str
    kind: TranslationEventKind
    batch_start: int
    expected_lines: int
    completed_lines: int
    overall_progress: float
    stream_start_line: int = 0
    stream_lines: tuple[str, ...] = ()
    translated_lines: tuple[str, ...] = ()
    display_batch_start: int | None = None
    message: str | None = None
