#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量智能缓存（内存版）
提供最小可用接口：get、set、get_stats、clear_all、optimize_cache
"""

import time
import json
import threading
import hashlib
from typing import Any, Dict, Optional


class SmartCache:
    def __init__(
        self,
        max_memory_size: int = 1000,
        max_file_size: int = 10000,  # 预留参数，不做文件缓存
        cache_dir: Optional[str] = None,  # 预留参数
        ttl_hours: int = 24,
    ):
        self.max_memory_size = max_memory_size
        self.max_file_size = max_file_size
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_hours * 3600

        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _make_key(self, text: str, context: Optional[Dict[str, Any]]) -> str:
        ctx = context or {}
        # 使用稳定序列化保证同一上下文生成相同key
        payload = json.dumps({"t": text, "c": ctx}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _is_expired(self, item: Dict[str, Any]) -> bool:
        return time.time() > item["expire_at"]

    def get(self, text: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        key = self._make_key(text, context)
        with self._lock:
            item = self._store.get(key)
            if not item:
                self._misses += 1
                return None
            if self._is_expired(item):
                # 过期清理
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return item["value"]

    def set(self, text: str, value: str, context: Optional[Dict[str, Any]] = None) -> None:
        key = self._make_key(text, context)
        with self._lock:
            # 简单的容量控制：超过容量时随机淘汰一个（此处直接pop一个最旧键）
            if len(self._store) >= self.max_memory_size:
                try:
                    # 淘汰一个过期的，否则淘汰任意一个
                    expired_keys = [k for k, v in self._store.items() if self._is_expired(v)]
                    if expired_keys:
                        del self._store[expired_keys[0]]
                    else:
                        # pop 任意一个键（Python3.7+为插入顺序，近似FIFO）
                        self._store.pop(next(iter(self._store)))
                except Exception:
                    pass

            self._store[key] = {
                "value": value,
                "expire_at": time.time() + self.ttl_seconds,
            }

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "items": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "ttl_seconds": self.ttl_seconds,
                "capacity": self.max_memory_size,
            }

    def clear_all(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def optimize_cache(self) -> None:
        """清理过期项"""
        with self._lock:
            expired = [k for k, v in self._store.items() if self._is_expired(v)]
            for k in expired:
                del self._store[k]