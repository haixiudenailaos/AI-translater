#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量批处理封装（线程池版）
提供最小可用接口：set_api_handler、submit_request、get_stats、flush_pending、configure
以及工厂方法：get_batch_processor
"""

from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Dict, List, Optional


class BatchProcessor:
    def __init__(
        self,
        max_batch_size: int = 10,
        max_wait_time: float = 0.5,
        max_workers: int = 4,
        enable_priority: bool = True,
        enable_deduplication: bool = True,
    ):
        self.max_batch_size = max_batch_size
        self.max_wait_time = max_wait_time
        self.enable_priority = enable_priority
        self.enable_deduplication = enable_deduplication

        self._handler: Optional[Callable[[List[str], List[Dict[str, Any]]], List[Optional[str]]]] = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._submitted = 0

    def set_api_handler(self, handler: Callable[[List[str], List[Dict[str, Any]]], List[Optional[str]]]) -> None:
        """SiliconFlowAPI 会设置其批处理处理器"""
        self._handler = handler

    def _run_single(self, text: str, context: Dict[str, Any]) -> Optional[str]:
        if not self._handler:
            return None
        # 以单元素批次调用，复用现有处理器逻辑
        results = self._handler([text], [context])
        return results[0] if results else None

    def submit_request(self, text: str, context: Dict[str, Any], priority: int = 0) -> Future:
        """提交请求并返回 Future（最小实现不做真正合批）"""
        self._submitted += 1
        return self._executor.submit(self._run_single, text, context)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "submitted": self._submitted,
            "max_workers": self._executor._max_workers,  # 类型: int
            "max_batch_size": self.max_batch_size,
            "max_wait_time": self.max_wait_time,
        }

    def flush_pending(self) -> None:
        """最小实现无显式队列，这里为 no-op"""
        pass

    def configure(self, **kwargs) -> None:
        """更新部分配置参数"""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)


def get_batch_processor(
    max_batch_size: int = 10,
    max_wait_time: float = 0.5,
    max_workers: int = 4,
    enable_priority: bool = True,
    enable_deduplication: bool = True,
) -> BatchProcessor:
    # 为简单起见，每次调用返回新的处理器实例
    return BatchProcessor(
        max_batch_size=max_batch_size,
        max_wait_time=max_wait_time,
        max_workers=max_workers,
        enable_priority=enable_priority,
        enable_deduplication=enable_deduplication,
    )