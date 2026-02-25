#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
硅基流动API接口模块
继承 BaseAPI，仅保留差异配置：HTTP/2、心跳保活、增强 vision_query 日志。
"""

import httpx
import threading
import json
from typing import Dict, Any, Optional
from .base_api import BaseAPI
from ..utils.logger import get_logger

logger = get_logger(__name__)


class SiliconFlowAPI(BaseAPI):
    DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
    DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2"
    DEFAULT_MAX_KEEPALIVE = 20
    DEFAULT_MAX_CONNECTIONS = 50
    DEFAULT_TIMEOUT = 90.0

    def __init__(self, config: Dict[str, Any]):
        # SiliconFlow 特有：keepalive_expiry
        http_limits = config.get("http_limits", {})
        self._keepalive_expiry = http_limits.get("keepalive_expiry", 300.0)

        # 心跳配置（在 super().__init__ 之前设置，因为 _recreate_client 会被调用）
        self._heartbeat_enabled = config.get("enable_heartbeat", True)
        self._heartbeat_interval = config.get("heartbeat_interval", 60)
        self._heartbeat_thread = None
        self._heartbeat_stop_event = threading.Event()

        super().__init__(config)

        # 启动心跳保活线程
        if self._heartbeat_enabled:
            self._start_heartbeat()

    def _recreate_client(self):
        """重建 HTTP 客户端：启用 HTTP/2 和 keepalive_expiry。"""
        try:
            if self._current_client:
                try:
                    self._current_client.close()
                except Exception:
                    pass
            expiry = getattr(self, "_keepalive_expiry", 300.0)
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections,
                keepalive_expiry=expiry,
            )
            transport = httpx.HTTPTransport(retries=1, limits=limits)
            self._current_client = httpx.Client(
                timeout=self._timeout, transport=transport, http2=True
            )
        except Exception as e:
            logger.error("重建HTTP客户端失败: %s", e)
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections,
            )
            self._current_client = httpx.Client(timeout=self._timeout, limits=limits)

    # ── 心跳保活 ────────────────────────────────────────

    def _start_heartbeat(self):
        """使用 HEAD 请求保持连接活跃（不消耗 API 额度）。"""
        def worker():
            while not self._heartbeat_stop_event.is_set():
                if self._heartbeat_stop_event.wait(timeout=self._heartbeat_interval):
                    break
                if self._current_client and not self._cancel_event.is_set():
                    try:
                        self._current_client.head(self.base_url, timeout=3.0)
                    except Exception:
                        self._recreate_client()

        self._heartbeat_thread = threading.Thread(target=worker, daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        if self._heartbeat_thread:
            self._heartbeat_stop_event.set()
            self._heartbeat_thread.join(timeout=2.0)

    def close(self):
        self._stop_heartbeat()
        super().close()

    # ── 增强 vision_query（带 debug 日志）──────────────

    def vision_query(self, image_base64: str, mime_type: str, prompt: str,
                     model_override: str = None) -> Optional[str]:
        logger.debug("[vision_query] ====== 开始视觉查询 ======")
        logger.debug("[vision_query] mime_type=%s, base64长度=%s, model_override=%s",
                     mime_type, len(image_base64), model_override)

        if not self.api_key:
            logger.warning("[vision_query] API密钥为空")
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
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}",
                                "detail": "high",
                            },
                        },
                        {"type": "text", "text": prompt},
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
            logger.debug("[vision_query] model=%s, URL=%s/chat/completions",
                         request_data["model"], self.base_url)

            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=request_data,
            )
            logger.debug("[vision_query] 响应状态码: %s", resp.status_code)

            if resp.status_code == 200:
                result = resp.json()
                if result.get("choices"):
                    content = result["choices"][0]["message"]["content"]
                    logger.debug("[vision_query] 成功获取响应")
                    return content
                else:
                    logger.warning("[vision_query] 响应中没有choices: %s",
                                   json.dumps(result, ensure_ascii=False)[:200])
            else:
                logger.error("[vision_query] 视觉查询失败: status=%s, text=%s",
                             resp.status_code, resp.text)

        except Exception as e:
            logger.error("[vision_query] 视觉查询请求异常: %s: %s",
                         type(e).__name__, e, exc_info=True)

        return None
