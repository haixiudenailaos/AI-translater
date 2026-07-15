#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片翻译运行时

在独立工作线程中持有一个 asyncio event loop，避免每张图片反复创建
event loop，同时让 Manga Provider 的异步 translate() 调用能在同步
ImageTranslationProvider.translate() 协议内执行。

生命周期：
- MangaRuntime 在 Provider 首次 translate 时启动 loop 线程。
- cancel/close 时停止 loop、等待线程退出、释放资源。
- 所有操作幂等，可安全多次调用。
"""

import asyncio
import threading
from typing import Awaitable, Callable, TypeVar

from ...utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class MangaRuntime:
    """持有一个后台 asyncio event loop 的运行时"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    def start(self) -> None:
        """启动后台 loop 线程（幂等）。"""
        with self._lock:
            if self._started:
                return
            self._loop = asyncio.new_event_loop()

            def _run() -> None:
                asyncio.set_event_loop(self._loop)
                self._loop.run_forever()

            self._thread = threading.Thread(
                target=_run, name="manga-runtime", daemon=True
            )
            self._thread.start()
            self._started = True

    def run(self, coro: Awaitable[T]) -> T:
        """在后台 loop 中运行协程并阻塞等待结果。

        Raises:
            RuntimeError: loop 未启动。
            任意异常：协程内抛出的异常原样透传给调用方。
        """
        if not self.is_running:
            self.start()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return future.result()

    def schedule(self, coro: Awaitable[T]) -> "asyncio.Future[T]":
        """在后台 loop 中调度协程，不阻塞等待。"""
        if not self.is_running:
            self.start()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[return-value]

    def shutdown(self) -> None:
        """停止 loop 并等待线程退出（幂等）。"""
        with self._lock:
            if not self._started:
                return
            loop = self._loop
            thread = self._thread
            self._started = False
            self._loop = None
            self._thread = None

        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:
                logger.warning("停止 event loop 失败: %s", exc)
        if thread is not None:
            thread.join(timeout=10)
        if loop is not None:
            try:
                # 取消所有未完成任务后关闭 loop
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                loop.close()
            except Exception as exc:
                logger.warning("关闭 event loop 失败: %s", exc)
