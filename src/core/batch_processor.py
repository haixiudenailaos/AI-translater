#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量批处理封装（线程池版）
提供最小可用接口：set_api_handler、submit_request、get_stats、flush_pending、configure
"""

from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Dict, List, Optional


class BatchProcessor:
    def __init__(self, max_batch_size: int = 10, max_wait_time: float = 0.5,
                 max_workers: int = 4):
        self.max_batch_size = max_batch_size
        self.max_wait_time = max_wait_time

        self._handler: Optional[Callable[[List[str], List[Dict[str, Any]]], List[Optional[str]]]] = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._submitted = 0

    def set_api_handler(self, handler: Callable[[List[str], List[Dict[str, Any]]], List[Optional[str]]]) -> None:
        self._handler = handler

    def _run_single(self, text: str, context: Dict[str, Any]) -> Optional[str]:
        if not self._handler:
            return None
        results = self._handler([text], [context])
        return results[0] if results else None

    def submit_request(self, text: str, context: Dict[str, Any], priority: int = 0) -> Future:
        """提交请求并返回 Future（当前实现为单条提交到线程池）"""
        self._submitted += 1
        return self._executor.submit(self._run_single, text, context)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "submitted": self._submitted,
            "max_workers": self._executor._max_workers,
            "max_batch_size": self.max_batch_size,
            "max_wait_time": self.max_wait_time,
        }

    def flush_pending(self) -> None:
        pass

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
