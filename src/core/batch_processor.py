#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量批处理封装（线程池版）
提供最小可用接口：set_api_handler、submit_request、get_stats、configure
"""

from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Dict, List, Optional
import threading


class BatchProcessor:
    def __init__(self, max_batch_size: int = 10, max_wait_time: float = 0.5,
                 max_workers: int = 4):
        self.max_batch_size = max_batch_size
        self.max_wait_time = max_wait_time
        self.max_workers = max_workers

        self._handler: Optional[Callable[[List[str], List[Dict[str, Any]]], List[Optional[str]]]] = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._submitted = 0
        self._batches = 0
        self._pending = []
        self._lock = threading.Lock()
        self._flush_timer = None
        self._closed = False

    def close(self):
        """BUG-005：关闭线程池，幂等可安全多次调用。"""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            pending = self._pending
            self._pending = []
            if self._flush_timer:
                self._flush_timer.cancel()
                self._flush_timer = None
        for _, _, future in pending:
            future.cancel()
        try:
            # cancel_futures=True 取消尚未开始的待办任务
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            # 某些旧版本 Python 不支持 cancel_futures 参数
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass

    def set_api_handler(self, handler: Callable[[List[str], List[Dict[str, Any]]], List[Optional[str]]]) -> None:
        self._handler = handler

    def _run_batch(self, requests) -> None:
        if not self._handler:
            for _, _, future in requests:
                future.set_result(None)
            return
        texts = [text for text, _, _ in requests]
        contexts = [context for _, context, _ in requests]
        try:
            results = self._handler(texts, contexts) or []
            for index, (_, _, future) in enumerate(requests):
                future.set_result(results[index] if index < len(results) else None)
        except Exception as exc:
            for _, _, future in requests:
                future.set_exception(exc)

    def _flush_pending(self):
        with self._lock:
            requests = self._pending[:self.max_batch_size]
            self._pending = self._pending[len(requests):]
            self._flush_timer = None
            if self._pending:
                self._flush_timer = threading.Timer(self.max_wait_time, self._flush_pending)
                self._flush_timer.daemon = True
                self._flush_timer.start()
        if requests and not self._closed:
            self._batches += 1
            self._executor.submit(self._run_batch, requests)

    def flush(self):
        """PERF-010：立即处理所有待处理请求，不等待 max_wait_time 超时。

        在批量提交场景（如 translate_batch）中，最后不足 max_batch_size
        的尾批无需等待 max_wait_time 即可立即发出。
        """
        while True:
            with self._lock:
                if self._flush_timer:
                    self._flush_timer.cancel()
                    self._flush_timer = None
                if not self._pending:
                    return
                requests = self._pending[:self.max_batch_size]
                self._pending = self._pending[len(requests):]
            if requests and not self._closed:
                self._batches += 1
                self._executor.submit(self._run_batch, requests)

    def submit_request(self, text: str, context: Dict[str, Any]) -> Future:
        """提交请求；在等待窗口内聚合为一次 provider 调用。"""
        future = Future()
        with self._lock:
            if self._closed:
                future.cancel()
                return future
            self._pending.append((text, context, future))
            self._submitted += 1
            should_flush = len(self._pending) >= self.max_batch_size
            if self._flush_timer is None and not should_flush:
                self._flush_timer = threading.Timer(self.max_wait_time, self._flush_pending)
                self._flush_timer.daemon = True
                self._flush_timer.start()
        if should_flush:
            self._flush_pending()
        return future

    def get_stats(self) -> Dict[str, Any]:
        return {
            "submitted": self._submitted,
            "max_workers": self.max_workers,
            "max_batch_size": self.max_batch_size,
            "max_wait_time": self.max_wait_time,
            "batches": self._batches,
        }

    def configure(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)


def get_batch_processor(max_batch_size: int = 10, max_wait_time: float = 0.5,
                        max_workers: int = 4, **_kwargs) -> BatchProcessor:
    return BatchProcessor(
        max_batch_size=max_batch_size,
        max_wait_time=max_wait_time,
        max_workers=max_workers,
    )
