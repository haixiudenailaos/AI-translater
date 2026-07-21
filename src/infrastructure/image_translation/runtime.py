#!/usr/bin/env python3
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
import time
from typing import Awaitable, TypeVar

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
            loop = asyncio.new_event_loop()
            self._loop = loop

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            self._thread = threading.Thread(target=_run, name="manga-runtime", daemon=True)
            self._thread.start()
            self._started = True

    def run(self, coro: Awaitable[T], *, timeout_seconds: float | None = None) -> T:
        """在后台 loop 中运行协程并阻塞等待结果。

        Raises:
            RuntimeError: loop 未启动。
            任意异常：协程内抛出的异常原样透传给调用方。
        """
        if not self.is_running:
            self.start()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        try:
            return future.result(timeout=timeout_seconds)
        except TimeoutError:
            future.cancel()
            raise

    def schedule(self, coro: Awaitable[T]) -> "asyncio.Future[T]":
        """在后台 loop 中调度协程，不阻塞等待。"""
        if not self.is_running:
            self.start()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[return-value]

    def shutdown(self, *, timeout_seconds: float = 10.0) -> bool:
        """停止 loop 并等待线程退出（幂等）。

        P1-3：修正关闭顺序——必须先在 loop 仍在运行时调度取消所有 pending
        tasks 和 async generators 的清理，再停止 loop。旧实现在 ``loop.stop``
        + ``thread.join`` 之后才尝试 ``task.cancel()``，此时 loop 已停止，
        取消请求不会被处理，async generator 也不会运行 ``aclose``，
        造成资源泄漏和 "Task was destroyed but it is pending!" 警告。
        """
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._lock:
            if not self._started:
                return True
            loop = self._loop
            thread = self._thread

        if loop is None or thread is None:
            return True

        if loop.is_running():
            # P1-3：在 loop 仍在运行时调度 graceful shutdown 协程
            async def _graceful_shutdown() -> None:
                try:
                    pending = asyncio.all_tasks(loop)
                    # 排除自身
                    current = asyncio.current_task()
                    if current is not None and current in pending:
                        pending.discard(current)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    # P1-3：关闭 async generators，避免资源泄漏
                    await loop.shutdown_asyncgens()
                except Exception as exc:
                    logger.warning("graceful shutdown 失败: %s", exc)

            try:
                future = asyncio.run_coroutine_threadsafe(_graceful_shutdown(), loop)
                # 所有清理步骤共享同一个绝对 deadline，不能各自再等完整超时。
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception as exc:
                logger.warning("调度 graceful shutdown 失败: %s", exc)
            # graceful shutdown 完成后停止 loop
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:
                logger.warning("停止 event loop 失败: %s", exc)

        # 等待 loop 线程退出。线程仍存活时不能关闭其 event loop；否则会与
        # 正在执行的协程竞争并产生 pending-task 警告。
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            logger.warning("MangaRuntime 线程在 shutdown 后仍未退出")
            return False

        # 此时线程已经退出，可安全关闭 loop 并发布“已关闭”的状态。
        try:
            loop.close()
        except Exception as exc:
            logger.warning("关闭 event loop 失败: %s", exc)
            return False
        with self._lock:
            if self._loop is loop:
                self._started = False
                self._loop = None
                self._thread = None
        return True
