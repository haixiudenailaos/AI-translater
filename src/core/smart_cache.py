#!/usr/bin/env python3
"""
轻量智能缓存（内存版）
提供最小可用接口：get、set、get_stats、clear_all、optimize_cache
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Dict


class SmartCache:
    def __init__(
        self,
        max_entries: int = 1000,
        ttl_hours: int = 24,
        **_kwargs,
    ):
        # PERF-6b：原字段名 ``max_memory_size`` 实为条目数而非字节，
        # 重命名为 ``max_entries`` 以消除语义歧义。
        # ``OrderedDict`` 提供 O(1) LRU 淘汰：访问时 move_to_end，
        # 超容量时 popitem(last=False) 移除最久未访问项。
        self.max_entries = max_entries
        self.ttl_seconds = ttl_hours * 3600

        self._store: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _make_key(self, text: str, context: Dict[str, Any] | None) -> str:
        ctx = context or {}
        # 使用稳定序列化保证同一上下文生成相同key
        payload = json.dumps(
            {"t": text, "c": ctx}, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _is_expired(self, item: Dict[str, Any]) -> bool:
        return time.time() > item["expire_at"]

    def get(self, text: str, context: Dict[str, Any] | None = None) -> str | None:
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
            # PERF-6b：O(1) LRU——命中时移到末尾（最近使用）
            self._store.move_to_end(key)
            self._hits += 1
            return item["value"]

    def set(self, text: str, value: str, context: Dict[str, Any] | None = None) -> None:
        key = self._make_key(text, context)
        with self._lock:
            # PERF-6b：O(1) LRU 容量控制
            if key not in self._store and len(self._store) >= self.max_entries:
                # 优先淘汰过期的
                expired_keys = [k for k, v in self._store.items() if self._is_expired(v)]
                for k in expired_keys:
                    del self._store[k]
                # 仍超容量则淘汰最久未访问的（OrderedDict 首项）
                while len(self._store) >= self.max_entries:
                    self._store.popitem(last=False)

            self._store[key] = {
                "value": value,
                "expire_at": time.time() + self.ttl_seconds,
            }
            # 新写入的项移到末尾（最近使用）
            self._store.move_to_end(key)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "items": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "ttl_seconds": self.ttl_seconds,
                "capacity": self.max_entries,
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
