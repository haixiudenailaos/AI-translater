#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 抽象基类
提取 SiliconFlowAPI 和 DeepseekAPI 的公共逻辑：
- HTTP 客户端管理（连接池、重建、关闭）
- 请求头构建
- 缓存和批处理初始化
- test_connection / cancel_requests / reset_cancel
- translate / translate_stream / vision_query（含重试）
- 缓存/批处理辅助方法
"""

import httpx
import json
import time
import threading
from typing import Dict, Any, Optional, List, Callable
from ..core.smart_cache import SmartCache
from ..core.batch_processor import get_batch_processor
from ..utils.logger import get_logger

logger = get_logger(__name__)


class BaseAPI:
    """OpenAI 兼容 API 的公共基类"""

    # 子类可覆盖的默认值
    DEFAULT_BASE_URL: str = ""
    DEFAULT_MODEL: str = ""
    DEFAULT_MAX_KEEPALIVE: int = 10
    DEFAULT_MAX_CONNECTIONS: int = 20
    DEFAULT_TIMEOUT: float = 60.0

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.base_url = config.get("base_url", self.DEFAULT_BASE_URL)
        self.api_key = config.get("api_key", "").strip()
        self.model_name = config.get("model_name", self.DEFAULT_MODEL)
        self.max_tokens = config.get("max_tokens", 2048)
        self.temperature = config.get("temperature", 0.3)
        self._cancel_event = threading.Event()
        self._current_client: Optional[httpx.Client] = None

        # HTTP 连接池配置
        http_limits = config.get("http_limits", {})
        self._max_keepalive = http_limits.get("max_keepalive_connections", self.DEFAULT_MAX_KEEPALIVE)
        self._max_connections = http_limits.get("max_connections", self.DEFAULT_MAX_CONNECTIONS)
        self._timeout = config.get("http_timeout", self.DEFAULT_TIMEOUT)

        # 初始化持久客户端
        self._recreate_client()

        # 请求头
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

        # 缓存
        self.enable_cache = config.get("enable_cache", True)
        if self.enable_cache:
            cc = config.get("cache_config", {})
            self.cache = SmartCache(
                max_memory_size=cc.get("max_memory_size", 1000),
                ttl_hours=cc.get("ttl_hours", 24),
            )
        else:
            self.cache = None

        # 批处理
        self.enable_batch = config.get("enable_batch", True)
        if self.enable_batch:
            bc = config.get("batch_config", {})
            self.batch_processor = get_batch_processor(
                max_batch_size=bc.get("max_batch_size", 10),
                max_wait_time=bc.get("max_wait_time", 0.5),
                max_workers=bc.get("max_workers", 4),
            )
            self.batch_processor.set_api_handler(self._batch_translate_handler)
        else:
            self.batch_processor = None

        self.enable_stream = config.get("enable_stream", True)
        self.stream_callbacks: Dict[str, Callable] = {}

    # ── HTTP 客户端管理 ─────────────────────────────────

    def _recreate_client(self):
        """重建持久 HTTP 客户端（连接池）。子类可覆盖以定制传输层。"""
        try:
            if self._current_client:
                try:
                    self._current_client.close()
                except Exception:
                    pass
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections,
            )
            self._current_client = httpx.Client(timeout=self._timeout, limits=limits)
        except Exception as e:
            logger.error("重建HTTP客户端失败: %s", e)
            self._current_client = httpx.Client(timeout=self._timeout)

    def _get_client(self) -> httpx.Client:
        if not self._current_client:
            self._recreate_client()
        return self._current_client

    def cancel_requests(self):
        self._cancel_event.set()
        if self._current_client:
            try:
                self._current_client.close()
            except Exception:
                pass

    def reset_cancel(self):
        self._cancel_event.clear()
        self._current_client = None

    def close(self):
        """释放资源。子类可扩展。"""
        if self._current_client:
            try:
                self._current_client.close()
            except Exception:
                pass
            self._current_client = None

    # ── 连接测试 ────────────────────────────────────────

    def test_connection(self) -> bool:
        if not self.api_key:
            logger.warning("API密钥为空")
            return False

        def _check_once(client: httpx.Client) -> Optional[bool]:
            try:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "temperature": 0.0,
                        "stream": False,
                    },
                    timeout=min(self._timeout, 3.0),
                )
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                        return bool(body.get("choices"))
                    except Exception:
                        return True
                if resp.status_code in (401, 403):
                    logger.error("连接失败：鉴权错误（API Key 可能无效或权限不足）")
                    return False
                if resp.status_code == 429:
                    logger.info("已连接：触发速率限制（429）")
                    return True
                if resp.status_code in (400, 404):
                    try:
                        body = resp.json()
                        raw = str(body.get("error") or body.get("message") or resp.text)
                    except Exception:
                        raw = resp.text
                    kw = ["model", "not found", "unknown", "invalid", "unsupported",
                          "模型", "不存在", "未知", "无效", "不支持", "未找到"]
                    if any(k in raw.lower() for k in kw):
                        logger.error("连接失败：模型不可用或不存在")
                        return False
                return None
            except Exception:
                return None

        for attempt in range(2):
            result = _check_once(self._get_client())
            if result is True:
                return True
            if result is False:
                return False
            if attempt == 0:
                self._recreate_client()

        logger.error("连接测试失败：服务不可达或接口异常")
        return False

    # ── 翻译核心 ────────────────────────────────────────

    def translate(self, text: str) -> Optional[str]:
        if not self.api_key:
            logger.warning("API密钥为空")
            return None
        return self._direct_translate(text)

    def _direct_translate(self, text: str, context: Dict[str, Any] = None) -> Optional[str]:
        if not self.api_key:
            logger.warning("API密钥为空")
            return None
        if self._cancel_event.is_set():
            return None

        request_data = {
            "model": context.get("model", self.model_name) if context else self.model_name,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": context.get("max_tokens", self.max_tokens) if context else self.max_tokens,
            "temperature": context.get("temperature", self.temperature) if context else self.temperature,
            "stream": False,
        }

        for attempt in range(2):
            if self._cancel_event.is_set():
                return None
            try:
                client = self._get_client()
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=request_data,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("choices"):
                        return result["choices"][0]["message"]["content"]
                else:
                    logger.error("API请求失败: %s - %s", resp.status_code, resp.text)
                if attempt == 0:
                    self._recreate_client()
            except httpx.ConnectError:
                if attempt == 0:
                    self._recreate_client()
                    continue
                return None
            except Exception as e:
                if not self._cancel_event.is_set():
                    logger.error("翻译请求失败: %s", e)
                return None
        return None

    # ── 流式翻译 ────────────────────────────────────────

    def translate_stream(self, text: str, callback=None) -> Optional[str]:
        if self._cancel_event.is_set():
            return None

        max_retries = 5
        for retry in range(max_retries):
            try:
                client = self._get_client()
                if self._cancel_event.is_set():
                    return None

                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": text}],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                        "stream": True,
                    },
                ) as response:
                    if response.status_code != 200:
                        if retry < max_retries - 1:
                            logger.warning("流式请求失败 (HTTP %s)，正在重试 (%s/%s)...",
                                           response.status_code, retry + 1, max_retries)
                            self._recreate_client()
                            time.sleep(1)
                            continue
                        logger.error("流式请求失败: %s次重试后仍失败 (HTTP %s)",
                                     max_retries, response.status_code)
                        return None

                    full_content = ""
                    for line in response.iter_lines():
                        if self._cancel_event.is_set():
                            return None
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                choices = data.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        full_content += content
                                        if callback:
                                            callback(content)
                            except json.JSONDecodeError:
                                continue
                    return full_content

            except httpx.ConnectError as e:
                if self._cancel_event.is_set():
                    return None
                if retry < max_retries - 1:
                    logger.warning("连接错误: %s，正在重试 (%s/%s)...", e, retry + 1, max_retries)
                    self._recreate_client()
                    time.sleep(1)
                else:
                    logger.error("流式翻译失败: %s次重试后仍无法连接 - %s", max_retries, e)
                    return None
            except httpx.TimeoutException as e:
                if self._cancel_event.is_set():
                    return None
                if retry < max_retries - 1:
                    logger.warning("请求超时: %s，正在重试 (%s/%s)...", e, retry + 1, max_retries)
                    self._recreate_client()
                    time.sleep(1)
                else:
                    logger.error("流式翻译失败: %s次重试后仍超时 - %s", max_retries, e)
                    return None
            except Exception as e:
                if self._cancel_event.is_set():
                    return None
                if retry < max_retries - 1:
                    logger.warning("流式翻译异常: %s，正在重试 (%s/%s)...", e, retry + 1, max_retries)
                    self._recreate_client()
                    time.sleep(1)
                else:
                    logger.error("流式翻译失败: %s次重试后仍失败 - %s", max_retries, e)
                    return None
        return None

    # ── 视觉查询 ────────────────────────────────────────

    def vision_query(self, image_base64: str, mime_type: str, prompt: str,
                     model_override: str = None) -> Optional[str]:
        if not self.api_key:
            logger.warning("API密钥为空")
            return None
        if self._cancel_event.is_set():
            return None
        try:
            client = self._get_client()
            if self._cancel_event.is_set():
                return None
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                        },
                    ],
                }
            ]
            request_data = {
                "model": model_override or self.model_name,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": 0.3,
                "stream": False,
            }
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=request_data,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("choices"):
                    return result["choices"][0]["message"]["content"]
            else:
                logger.error("视觉查询失败: %s - %s", resp.status_code, resp.text)
        except Exception as e:
            if not self._cancel_event.is_set():
                logger.error("视觉查询请求失败: %s", e)
        return None

    # ── 缓存 / 批处理辅助 ──────────────────────────────

    def _batch_translate_handler(self, texts: List[str], contexts: List[Dict[str, Any]]) -> List[Optional[str]]:
        results = []
        for text, context in zip(texts, contexts):
            if self.cache:
                cached = self.cache.get(text, context)
                if cached:
                    results.append(cached)
                    continue
            result = self._direct_translate(text, context)
            if result and self.cache:
                self.cache.set(text, result, context)
            results.append(result)
        return results

    def translate_with_cache(self, text: str, context: Dict[str, Any] = None) -> Optional[str]:
        if self.cache:
            cached = self.cache.get(text, context)
            if cached:
                return cached
        result = self.translate(text)
        if result and self.cache:
            self.cache.set(text, result, context)
        return result

    def translate_batch(self, texts: List[str], contexts: List[Dict[str, Any]] = None,
                        priority: int = 0) -> List[Optional[str]]:
        if not self.batch_processor:
            return [self.translate_with_cache(t, contexts[i] if contexts and i < len(contexts) else None)
                    for i, t in enumerate(texts)]
        futures = []
        for i, text in enumerate(texts):
            ctx = contexts[i] if contexts and i < len(contexts) else {}
            futures.append(self.batch_processor.submit_request(text, ctx, priority=priority))
        results = []
        for f in futures:
            try:
                results.append(f.result(timeout=60))
            except Exception as e:
                logger.error("批处理翻译失败: %s", e)
                results.append(None)
        return results

    def translate_stream_enhanced(self, text: str, callback: Callable[[str], None] = None,
                                  context: Dict[str, Any] = None, stream_id: str = None) -> Optional[str]:
        if not self.enable_stream:
            result = self.translate_with_cache(text, context)
            if callback and result:
                callback(result)
            return result
        if self.cache:
            cached = self.cache.get(text, context)
            if cached:
                if callback:
                    self._simulate_stream_output(cached, callback)
                return cached
        if stream_id and callback:
            self.stream_callbacks[stream_id] = callback
        result = self.translate_stream(text, callback)
        if result and self.cache:
            self.cache.set(text, result, context)
        if stream_id and stream_id in self.stream_callbacks:
            del self.stream_callbacks[stream_id]
        return result

    def _simulate_stream_output(self, text: str, callback: Callable[[str], None],
                                chunk_size: int = 3, delay: float = 0.05):
        def worker():
            for i in range(0, len(text), chunk_size):
                if self._cancel_event.is_set():
                    break
                callback(text[i:i + chunk_size])
                time.sleep(delay)
        threading.Thread(target=worker, daemon=True).start()

    def cancel_stream(self, stream_id: str):
        self.stream_callbacks.pop(stream_id, None)

    def get_cache_stats(self) -> Dict[str, Any]:
        return self.cache.get_stats() if self.cache else {}

    def get_batch_stats(self) -> Dict[str, Any]:
        return self.batch_processor.get_stats() if self.batch_processor else {}

    def clear_cache(self):
        if self.cache:
            self.cache.clear_all()

    def optimize_cache(self):
        if self.cache:
            self.cache.optimize_cache()

    def flush_batch(self):
        if self.batch_processor:
            self.batch_processor.flush_pending()

    def get_enhanced_stats(self) -> Dict[str, Any]:
        stats = {
            "cache_enabled": self.enable_cache,
            "batch_enabled": self.enable_batch,
            "stream_enabled": self.enable_stream,
            "active_streams": len(self.stream_callbacks),
        }
        if self.cache:
            stats["cache_stats"] = self.get_cache_stats()
        if self.batch_processor:
            stats["batch_stats"] = self.get_batch_stats()
        return stats
