#!/usr/bin/env python3
"""
硅基流动API接口模块
继承 BaseAPI，仅保留差异配置：HTTP/2、心跳保活、增强 vision_query 日志。
"""

import json
import threading
from typing import Any, Dict

import httpx

from ..utils.logger import get_logger
from .base_api import BaseAPI, _summarize_response_body

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

    def _build_client(self) -> httpx.Client:
        """Build an HTTP/2 client without publishing it to other threads."""
        try:
            expiry = getattr(self, "_keepalive_expiry", 300.0)
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections,
                keepalive_expiry=expiry,
            )
            # 重试由 BaseAPI 统一处理，transport 不再隐式重复请求。
            transport = httpx.HTTPTransport(retries=0, limits=limits, http2=True)
            return httpx.Client(
                timeout=self._http_timeout, transport=transport, http2=True
            )
        except Exception as e:
            logger.error("重建HTTP客户端失败: %s", e)
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections,
            )
            return httpx.Client(timeout=self._http_timeout, limits=limits)

    # ── 心跳保活 ────────────────────────────────────────

    def _start_heartbeat(self):
        """使用 HEAD 请求保持连接活跃（不消耗 API 额度）。

        R2-BUG-023：心跳失败时不得直接关闭可能处于活动状态的客户端。
        通过 _recreate_client_if_safe() 检查活动请求计数，仅在没有翻译
        请求进行时才重建。流式翻译持续超过多个心跳周期也不会被中断。
        """

        def worker():
            while not self._heartbeat_stop_event.is_set():
                if self._heartbeat_stop_event.wait(timeout=self._heartbeat_interval):
                    break
                if self._current_client and not self._cancel_event.is_set():
                    try:
                        self._current_client.head(self.base_url, timeout=3.0)
                    except Exception:
                        # R2-BUG-023：仅在没有活动请求时重建客户端
                        self._recreate_client_if_safe()

        self._heartbeat_thread = threading.Thread(target=worker, daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        if self._heartbeat_thread:
            self._heartbeat_stop_event.set()
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

    def close(self):
        """BUG-005：关闭心跳线程和父类资源，幂等可安全多次调用。"""
        self._stop_heartbeat()
        super().close()

    # ── 增强 vision_query（带 debug 日志）──────────────

    def vision_query(
        self, image_base64: str, mime_type: str, prompt: str, model_override: str = None
    ) -> str | None:
        logger.debug("[vision_query] ====== 开始视觉查询 ======")
        logger.debug(
            "[vision_query] mime_type=%s, base64长度=%s, model_override=%s",
            mime_type,
            len(image_base64),
            model_override,
        )

        if not self.api_key:
            logger.warning("[vision_query] API密钥为空")
            return None
        if self._cancel_event.is_set():
            return None

        try:
            # R2-BUG-023：跟踪活动请求
            with self._using_client() as client:
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
                logger.debug(
                    "[vision_query] model=%s, URL=%s/chat/completions",
                    request_data["model"],
                    self.base_url,
                )

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
                        logger.warning(
                            "[vision_query] 响应中没有choices: %s",
                            json.dumps(result, ensure_ascii=False)[:200],
                        )
                else:
                    logger.error(
                        "[vision_query] 视觉查询失败: status=%s, text=%s",
                        resp.status_code,
                        # P1-5：响应正文走有界摘要
                        _summarize_response_body(resp.text),
                    )

        except Exception as e:
            logger.error(
                "[vision_query] 视觉查询请求异常: %s: %s", type(e).__name__, e, exc_info=True
            )

        return None
