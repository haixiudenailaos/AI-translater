#!/usr/bin/env python3
"""轻量翻译缓存。

默认使用进程内 LRU。传入 ``cache_dir`` 时，已缓存的译文会持久化到
该目录，供应用下次启动复用。磁盘文件只保存哈希键、译文和过期时间，
不保存原文或完整请求上下文。
"""

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_PERSISTENCE_SCHEMA_VERSION = 1


class SmartCache:
    def __init__(
        self,
        max_entries: int = 1000,
        ttl_hours: int = 24,
        cache_dir: Path | str | None = None,
        namespace: str = "default",
        **_kwargs,
    ):
        # PERF-6b：``max_entries`` 是条目数，不是字节数；OrderedDict 提供
        # O(1) 的 LRU 访问和淘汰。
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_hours) * 3600)

        self._store: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._persistence_path = self._initialize_persistence_path(cache_dir, namespace)
        self._load_persisted_entries()

    @property
    def persistence_path(self) -> Path | None:
        """磁盘缓存文件路径；未启用持久化缓存时为 ``None``。"""
        return self._persistence_path

    @staticmethod
    def _initialize_persistence_path(cache_dir: Path | str | None, namespace: str) -> Path | None:
        if cache_dir is None or str(cache_dir).strip() == "":
            return None
        try:
            directory = Path(cache_dir).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            # provider/model 来自外部配置，不能直接作为文件名；短哈希同时
            # 让不同模型的缓存彼此隔离。
            digest = hashlib.sha256(str(namespace).encode("utf-8")).hexdigest()[:16]
            return directory / f"translation-cache-{digest}.json"
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("初始化持久化缓存目录失败，已退回内存缓存: %s", exc)
            return None

    def _load_persisted_entries(self) -> None:
        """尽力恢复有效缓存；损坏文件不影响翻译主流程。"""
        path = self._persistence_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = payload.get("entries", []) if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                raise ValueError("entries 不是列表")
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("读取持久化缓存失败，已忽略旧缓存 %s: %s", path, exc)
            return

        now = time.time()
        with self._lock:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("key")
                value = entry.get("value")
                expire_at = entry.get("expire_at")
                if (
                    not isinstance(key, str)
                    or not isinstance(value, str)
                    or not isinstance(expire_at, int | float)
                    or expire_at <= now
                ):
                    continue
                self._store[key] = {"value": value, "expire_at": float(expire_at)}
                self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)

    def _persist_locked(self) -> None:
        """原子写入缓存快照；调用方必须持有 ``_lock``。"""
        path = self._persistence_path
        if path is None:
            return
        payload = {
            "schema_version": _PERSISTENCE_SCHEMA_VERSION,
            "entries": [
                {"key": key, "value": item["value"], "expire_at": item["expire_at"]}
                for key, item in self._store.items()
            ],
        }
        try:
            # 延迟导入避免内存缓存的启动路径引入文件处理依赖。
            from ..utils.file_handler import write_json_atomic

            write_json_atomic(path, payload)
        except Exception as exc:  # noqa: BLE001 - 缓存写入不得阻断翻译
            logger.warning("写入持久化缓存失败 %s: %s", path, exc)

    def _make_key(self, text: str, context: Dict[str, Any] | None) -> str:
        ctx = context or {}
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
                del self._store[key]
                self._misses += 1
                self._persist_locked()
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return item["value"]

    def set(self, text: str, value: str, context: Dict[str, Any] | None = None) -> None:
        key = self._make_key(text, context)
        with self._lock:
            if key not in self._store and len(self._store) >= self.max_entries:
                expired_keys = [k for k, v in self._store.items() if self._is_expired(v)]
                for expired_key in expired_keys:
                    del self._store[expired_key]
                while len(self._store) >= self.max_entries:
                    self._store.popitem(last=False)

            self._store[key] = {
                "value": value,
                "expire_at": time.time() + self.ttl_seconds,
            }
            self._store.move_to_end(key)
            self._persist_locked()

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "items": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "ttl_seconds": self.ttl_seconds,
                "capacity": self.max_entries,
                "persistent": self._persistence_path is not None,
                "persistence_path": str(self._persistence_path or ""),
            }

    def clear_all(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._persist_locked()

    def optimize_cache(self) -> None:
        """清理过期项。"""
        with self._lock:
            expired = [k for k, v in self._store.items() if self._is_expired(v)]
            for key in expired:
                del self._store[key]
            if expired:
                self._persist_locked()
