#!/usr/bin/env python3
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

import json
import random
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Generator, List

import httpx

from ..config.translation_profile import normalize_openai_base_url
from ..core.smart_cache import SmartCache
from ..domain.errors import TranslationRequestError
from ..utils.log_sanitizer import sanitize_for_log
from ..utils.logger import get_logger
from ..utils.token_estimator import estimate_tokens

logger = get_logger(__name__)

# P1-5：响应正文日志的最大长度（截断后附加 ...truncated 标记）
_RESPONSE_LOG_MAX_LENGTH = 500


def _summarize_response_body(body: str) -> str:
    """P1-5：对 HTTP 响应正文做有界摘要，避免超长 JSON 主体占满日志。

    截断到 ``_RESPONSE_LOG_MAX_LENGTH`` 字符并脱敏（API Key 等），
    超长时附加 ``...(truncated)`` 标记。
    """
    if not body:
        return ""
    if len(body) > _RESPONSE_LOG_MAX_LENGTH:
        body = body[:_RESPONSE_LOG_MAX_LENGTH] + "...(truncated)"
    return str(sanitize_for_log(body))


class BaseAPI:
    """OpenAI 兼容 API 的公共基类"""

    # 子类可覆盖的默认值
    DEFAULT_BASE_URL: str = ""
    DEFAULT_MODEL: str = ""
    DEFAULT_MAX_KEEPALIVE: int = 10
    DEFAULT_MAX_CONNECTIONS: int = 20
    DEFAULT_TIMEOUT: float = 60.0
    DEFAULT_CONNECTION_TEST_TIMEOUT: float = 30.0
    # 只有明确验证过 HTTP/2 的 provider 才开启（见 SiliconFlowAPI）。
    SUPPORTS_HTTP2: bool = False

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        raw_base_url = config.get("base_url", self.DEFAULT_BASE_URL)
        # Validate at the transport boundary too.  Callers can construct an
        # API directly from a file, queue task, or migration and must not be
        # able to send a Bearer token to a remote plain-HTTP endpoint.
        self.base_url = normalize_openai_base_url(raw_base_url) if raw_base_url else ""
        self.api_key = config.get("api_key", "").strip()
        self.model_name = config.get("model_name", self.DEFAULT_MODEL)
        self.temperature = config.get("temperature", 0.3)
        self._cancel_event = threading.Event()
        self._current_client: httpx.Client | None = None
        # 退役实例（``retire()``）不再重建，而是拒绝后续请求。
        self._retired = False
        # 逐行/单并发模式下切到 HTTP/1.1：小模型逐行翻译会在一条连接上连续
        # 开关大量短流，打包后的 Windows exe 里 h2 在这种模式下会以
        # ConnectionTerminated / WinError 10038 中断整个连接。
        self._http2_enabled = self.SUPPORTS_HTTP2

        # R2-BUG-023：活动请求计数器和客户端锁
        # 心跳线程通过检查 _active_requests 判断是否有翻译请求正在进行，
        # 避免在流式翻译过程中关闭客户端导致连接中断。
        self._client_lock = threading.Lock()
        self._active_requests = 0

        # 每次请求的局部结果（当前只有流式 ``finish_reason``）。
        # 必须按线程隔离：同一实例被多个批次线程并发使用，放在实例上会让
        # 并发请求读到彼此的状态。
        self._request_local = threading.local()

        self._max_attempts = max(1, min(5, int(config.get("api_max_attempts", 3))))
        self._retry_base_delay = max(0.0, float(config.get("retry_base_delay", 1.0)))
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "request_attempts": 0,
            "successful_requests": 0,
            "retries": 0,
            "rate_limit_errors": 0,
            "input_tokens_estimated": 0,
            "output_tokens_estimated": 0,
            "ttft_seconds": 0.0,
            "generation_seconds": 0.0,
            "request_seconds": 0.0,
        }
        self._rate_limit_pressure = 0.0
        # Batch-D: hook fired on every retryable HTTP response, before retry delay.
        # Installed by queue_scheduler to notify the shared ProviderLimiter early.
        self._on_retryable_response: Callable[[int, float | None], None] | None = None

        # HTTP 连接池配置
        http_limits = config.get("http_limits", {})
        self._max_keepalive = http_limits.get(
            "max_keepalive_connections", self.DEFAULT_MAX_KEEPALIVE
        )
        self._max_connections = http_limits.get("max_connections", self.DEFAULT_MAX_CONNECTIONS)
        legacy_timeout = float(config.get("http_timeout", self.DEFAULT_TIMEOUT))
        self._connect_timeout = float(config.get("http_connect_timeout", 10.0))
        self._read_timeout = float(config.get("http_read_timeout", legacy_timeout))
        self._write_timeout = float(config.get("http_write_timeout", 60.0))
        self._pool_timeout = float(config.get("http_pool_timeout", 10.0))
        self._timeout = self._read_timeout
        connection_test_timeout = float(
            config.get("connection_test_timeout", self.DEFAULT_CONNECTION_TEST_TIMEOUT)
        )
        self._connection_test_timeout = httpx.Timeout(
            connect=min(self._connect_timeout, connection_test_timeout),
            read=min(self._read_timeout, connection_test_timeout),
            write=min(self._write_timeout, connection_test_timeout),
            pool=min(self._pool_timeout, connection_test_timeout),
        )
        self._http_timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._write_timeout,
            pool=self._pool_timeout,
        )

        # 初始化持久客户端
        self._recreate_client()

        # 请求头
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

        # 缓存（BUG-008：主翻译路径调用 translate_stream() 绕过缓存，默认不创建）
        self.enable_cache = config.get("enable_cache", False)
        if self.enable_cache:
            cc = config.get("cache_config", {})
            if not isinstance(cc, dict):
                cc = {}
            self.cache = SmartCache(
                max_entries=cc.get("max_entries", cc.get("max_memory_size", 1000)),
                ttl_hours=cc.get("ttl_hours", 24),
                cache_dir=cc.get("cache_dir"),
                namespace=f"{config.get('provider', '')}:{config.get('model_name', '')}",
            )
        else:
            self.cache = None

        self.enable_stream = config.get("enable_stream", True)
        self.stream_callbacks: Dict[str, Callable] = {}

    # ── HTTP 客户端管理 ─────────────────────────────────

    def _build_client(self) -> httpx.Client:
        """Build a fresh HTTP client without publishing it to other threads."""
        try:
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections,
            )
            return httpx.Client(timeout=self._http_timeout, limits=limits)
        except Exception as e:
            logger.error("重建HTTP客户端失败: %s", e)
            return httpx.Client(timeout=self._http_timeout)

    @staticmethod
    def _close_client(client: httpx.Client | None) -> None:
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:
            logger.warning("关闭 HTTP 客户端失败: %s", exc)

    def _replace_client_locked(self) -> httpx.Client | None:
        """Atomically publish a fresh client while holding ``_client_lock``."""
        old_client = self._current_client
        self._current_client = self._build_client()
        return old_client

    def _recreate_client(self) -> None:
        """Replace the shared client atomically, then close the detached client."""
        with self._client_lock:
            if self._retired:
                return
            old_client = self._replace_client_locked()
        self._close_client(old_client)

    def _get_client(self) -> httpx.Client:
        """获取当前 HTTP 客户端。

        R2-BUG-008：不仅判断是否为 None，还要判断 is_closed。
        取消操作会关闭并置空客户端，下次请求必须重建，否则复用已关闭客户端
        会导致连续失败。

        退役实例（``retire()``）不再重建，而是抛出 ``TranslationRequestError``：
        它的运行已被 UI 丢弃，继续发请求只会消耗额度。
        """
        old_client: httpx.Client | None = None
        with self._client_lock:
            if self._retired:
                raise TranslationRequestError("API 客户端已因配置变更退役，本次请求不再发送")
            client = self._current_client
            if client is None or getattr(client, "is_closed", False):
                old_client = self._replace_client_locked()
                client = self._current_client
        self._close_client(old_client)
        assert client is not None
        return client

    @contextmanager
    def _using_client(self) -> Generator[httpx.Client, None, None]:
        """上下文管理器：跟踪活动请求，防止心跳线程关闭活动客户端。

        R2-BUG-023：翻译线程在使用客户端期间增加 _active_requests 计数，
        心跳线程通过 _recreate_client_if_safe() 检查此计数，仅在无活动请求时
        才重建客户端。这样流式翻译持续超过多个心跳周期也不会被中断。
        """
        old_client: httpx.Client | None = None
        with self._client_lock:
            if self._retired:
                raise TranslationRequestError("API 客户端已因配置变更退役，本次请求不再发送")
            client = self._current_client
            if client is None or getattr(client, "is_closed", False):
                old_client = self._replace_client_locked()
                client = self._current_client
            self._active_requests += 1
        self._close_client(old_client)
        try:
            assert client is not None
            yield client
        finally:
            with self._client_lock:
                if self._active_requests > 0:
                    self._active_requests -= 1

    def _recreate_client_if_safe(self):
        """仅在无活动请求时重建客户端（供心跳线程使用）。

        R2-BUG-023：心跳失败时如果直接关闭活动客户端，会中断正在进行的
        流式翻译。此方法检查活动请求计数，仅在没有活动请求时才重建。
        有活动请求时跳过并记录日志，等下一个心跳周期再尝试。
        """
        with self._client_lock:
            if self._retired:
                return
            if self._active_requests > 0:
                logger.debug("心跳检测到 %d 个活动请求，跳过客户端重建", self._active_requests)
                return
            old_client = self._replace_client_locked()
        self._close_client(old_client)

    def cancel_requests(self):
        """取消所有进行中的请求。

        R2-BUG-008：关闭客户端后立即置空，确保下次 _get_client() 会重建。
        旧实现只关闭不置空，_get_client() 仅判断 None，导致复用已关闭客户端。
        """
        with self._client_lock:
            self._cancel_event.set()
            client = self._current_client
            self._current_client = None
        self._close_client(client)

    def reset_cancel(self):
        """BUG-005：只重置取消标记，不承担客户端重建/丢弃职责。

        R2-BUG-008：取消时客户端已被置空，下次 _get_client() 会自动重建，
        因此此处无需重建客户端。
        """
        self._cancel_event.clear()

    def close(self):
        """BUG-005：释放 HTTP 客户端，幂等可安全多次调用。"""
        with self._client_lock:
            client = self._current_client
            self._current_client = None
        self._close_client(client)

    def retire(self) -> None:
        """永久停用此实例：关闭客户端并拒绝后续请求。幂等。

        配置变更会让引擎构造一个新的 API 实例，但此前启动的批次线程仍持有
        旧实例。旧实例若能继续重建连接，它会跑完整个运行——而 UI 早已按新
        ``run_id`` 丢弃它的进度事件，于是进度条永久静止、额度被白白消耗。
        与 ``close()`` 的区别：``close()`` 之后允许惰性重建，``retire()`` 之后
        不允许。
        """
        with self._client_lock:
            self._retired = True
            client = self._current_client
            self._current_client = None
        self._cancel_event.set()
        self._close_client(client)

    def configure_serial_transport(self, serial: bool) -> None:
        """逐行单并发运行时改用 HTTP/1.1；恢复批量并发时切回默认协议。

        小模型逐行模式会在同一条连接上连续开关成百上千条很短的流。打包后的
        Windows exe 中，h2 在这种模式下会以 ``ConnectionTerminated`` /
        ``WinError 10038`` 掉整条连接，表现为翻译中途长时间停顿后失败。
        HTTP/1.1 每个请求独立成流，不受此影响。

        不支持 HTTP/2 的 provider 上是纯 no-op。切换必然重建客户端，
        因此值未变化时不做任何事（保持幂等，避免拔掉在途连接）。
        """
        if not self.SUPPORTS_HTTP2:
            return
        target = self.SUPPORTS_HTTP2 and not serial
        with self._client_lock:
            if self._retired or self._http2_enabled == target:
                return
            self._http2_enabled = target
            old_client = self._replace_client_locked()
        self._close_client(old_client)
        logger.info(
            "HTTP 传输已切换为 %s（逐行单并发=%s）", "HTTP/2" if target else "HTTP/1.1", serial
        )

    # ── 连接测试 ────────────────────────────────────────

    def test_connection(self) -> bool:
        if not self.api_key:
            logger.warning("API密钥为空")
            return False

        def _check_once(client: httpx.Client) -> bool | None:
            try:
                # Only the response status is needed to prove that the endpoint,
                # credentials, and model are accepted.  ``Client.post`` eagerly
                # reads the generated body; some providers send HTTP 200 headers
                # first and then take more than a few seconds to finish that body.
                # This made a healthy connection look like a read timeout,
                # especially during a packaged application's cold start.
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "temperature": 0.0,
                        "stream": False,
                    },
                    timeout=self._connection_test_timeout,
                ) as resp:
                    if resp.status_code == 200:
                        return True
                    if resp.status_code in (401, 403):
                        logger.error("连接失败：鉴权错误（API Key 可能无效或权限不足）")
                        return False
                    if resp.status_code == 429:
                        logger.info("已连接：触发速率限制（429）")
                        return True
                    if resp.status_code in (400, 404):
                        try:
                            resp.read()
                            body = resp.json()
                            raw = str(body.get("error") or body.get("message") or resp.text)
                        except Exception:
                            raw = resp.text
                        kw = [
                            "model",
                            "not found",
                            "unknown",
                            "invalid",
                            "unsupported",
                            "模型",
                            "不存在",
                            "未知",
                            "无效",
                            "不支持",
                            "未找到",
                        ]
                        if any(k in raw.lower() for k in kw):
                            logger.error("连接失败：模型不可用或不存在")
                            return False
                    return None
            except Exception as exc:
                logger.warning("连接测试异常: %s", exc)
                return None

        for attempt in range(2):
            # R2-BUG-023：跟踪活动请求
            with self._using_client() as client:
                result = _check_once(client)
            if result is True:
                return True
            if result is False:
                return False
            if attempt == 0:
                self._recreate_client()

        logger.error("连接测试失败：服务不可达或接口异常")
        return False

    # ── 翻译核心 ────────────────────────────────────────

    def translate(self, text: str) -> str | None:
        if not self.api_key:
            logger.warning("API密钥为空")
            return None
        return self._direct_translate(text)

    def _direct_translate(self, text: str, context: Dict[str, Any] | None = None) -> str | None:
        if not self.api_key:
            logger.warning("API密钥为空")
            return None
        if self._cancel_event.is_set():
            return None

        context = context or {}
        messages = []
        if context.get("system_prompt"):
            messages.append({"role": "system", "content": context["system_prompt"]})
        messages.append({"role": "user", "content": text})
        request_data = {
            "model": context.get("model", self.model_name),
            "messages": messages,
            "temperature": context.get("temperature", self.temperature),
            "stream": False,
        }

        for attempt in range(2):
            if self._cancel_event.is_set():
                return None
            try:
                # R2-BUG-023：跟踪活动请求
                with self._using_client() as client:
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
                        # P1-5：响应正文走有界摘要，避免超长 JSON 占满日志
                        logger.error(
                            "API请求失败: %s - %s",
                            resp.status_code,
                            _summarize_response_body(resp.text),
                        )
                if attempt == 0:
                    self._recreate_client_if_safe()
            except httpx.ConnectError:
                if attempt == 0:
                    self._recreate_client_if_safe()
                    continue
                return None
            except Exception as e:
                if not self._cancel_event.is_set():
                    logger.error("翻译请求失败: %s", e)
                return None
        return None

    # ── 流式翻译 ────────────────────────────────────────

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in (408, 425, 429) or 500 <= status_code <= 599

    def _retry_delay(self, attempt: int, response=None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(30.0, max(0.0, float(retry_after)))
                except ValueError:
                    pass
        base = min(20.0, self._retry_base_delay * (2**attempt))
        # Batch-D: ±10% jitter prevents thundering-herd retry synchronisation
        jitter = random.uniform(-0.1, 0.1) * base
        return max(0.0, base + jitter)

    def _record_attempt(self, input_tokens: int) -> None:
        with self._metrics_lock:
            self._metrics["request_attempts"] += 1
            self._metrics["input_tokens_estimated"] += input_tokens

    def _record_retry(self, *, rate_limited: bool = False) -> None:
        with self._metrics_lock:
            self._metrics["retries"] += 1
            if rate_limited:
                self._metrics["rate_limit_errors"] += 1
                self._rate_limit_pressure = min(8.0, self._rate_limit_pressure + 1.0)

    def _record_rate_limit(self) -> None:
        with self._metrics_lock:
            self._metrics["rate_limit_errors"] += 1
            self._rate_limit_pressure = min(8.0, self._rate_limit_pressure + 1.0)

    def _record_success(
        self, *, started_at: float, first_token_at: float, output_text: str
    ) -> None:
        finished_at = time.perf_counter()
        output_tokens = estimate_tokens(output_text)
        with self._metrics_lock:
            self._metrics["successful_requests"] += 1
            self._metrics["output_tokens_estimated"] += output_tokens
            self._metrics["ttft_seconds"] += max(0.0, first_token_at - started_at)
            self._metrics["generation_seconds"] += max(0.0, finished_at - first_token_at)
            self._metrics["request_seconds"] += max(0.0, finished_at - started_at)
            self._rate_limit_pressure = max(0.0, self._rate_limit_pressure - 0.25)

    def recommended_concurrency(self, configured: int) -> int:
        """Reduce new request fan-out while the provider is returning 429s."""
        with self._metrics_lock:
            pressure = self._rate_limit_pressure
        if pressure >= 0.5:
            return 1
        return max(1, configured)

    def recommended_input_budget(self, configured: int) -> int:
        """Use a smaller next-run batch budget after token/rate pressure."""
        with self._metrics_lock:
            pressure = self._rate_limit_pressure
        return max(512, int(configured * 0.75)) if pressure >= 0.5 else configured

    def set_on_retryable_response(self, hook: Callable[[int, float | None], None] | None) -> None:
        """Batch-D: Set a hook called on every retryable HTTP response.

        Called BEFORE the internal retry delay with
        ``(status_code: int, retry_after_seconds: float | None)``.
        ``status_code == 0`` means a network/timeout exception (no HTTP status).
        Pass ``None`` to clear.
        """
        self._on_retryable_response = hook

    def last_finish_reason(self) -> str | None:
        """返回**本线程上次**流式请求的 ``finish_reason``。

        实现指南 §5.1/§6.1：超长请求的截断（``finish_reason == "length"``）必须
        作为结构化信息传给结果判断，不能仅凭 HTTP 成功宣告整批成功。

        该值按线程保存：同一 API 实例会被多个批次线程并发使用，把
        ``last_finish_reason`` 放在实例上会让并发请求互相读取对方的状态。
        """
        return getattr(self._request_local, "last_finish_reason", None)

    def translate_stream(
        self,
        text: str,
        callback: Callable[[str], None] | None = None,
        system_prompt: str | None = None,
    ) -> str | None:
        if self._cancel_event.is_set():
            return None

        # 每次请求开始清空本线程的截断标记，避免上一次请求的状态泄漏到本次。
        self._request_local.last_finish_reason = None
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": text})
        input_tokens = estimate_tokens((system_prompt or "") + "\n" + text)

        for attempt in range(self._max_attempts):
            started_at = time.perf_counter()
            first_token_at = None
            content_parts: List[str] = []
            self._record_attempt(input_tokens)
            try:
                # R2-BUG-023：使用 _using_client 跟踪活动请求，
                # 防止心跳线程在流式翻译期间关闭客户端
                with self._using_client() as client:
                    if self._cancel_event.is_set():
                        return None

                    with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=self.headers,
                        json={
                            "model": self.model_name,
                            "messages": messages,
                            "temperature": self.temperature,
                            "stream": True,
                            # DeepSeek V4 enables high-effort reasoning by default.
                            # Translation only needs the final text; allowing the
                            # hidden reasoning stream can make a large batch appear
                            # stuck for a minute before its first translated token.
                            **(
                                {"thinking": {"type": "disabled"}}
                                if self.model_name.lower()
                                in {"deepseek-flash", "deepseek-v4-flash"}
                                and self.config.get("disable_thinking", True)
                                else {}
                            ),
                        },
                    ) as response:
                        if response.status_code != 200:
                            response.read()
                            status_code = response.status_code
                            rate_limited = status_code == 429
                            if (
                                self._is_retryable_status(status_code)
                                and attempt < self._max_attempts - 1
                            ):
                                self._record_retry(rate_limited=rate_limited)
                                # Batch-D: notify shared limiter before sleep
                                _hook = self._on_retryable_response
                                if _hook is not None:
                                    _ra_raw = response.headers.get("retry-after")
                                    _ra: float | None = None
                                    if _ra_raw:
                                        try:
                                            _ra = max(0.0, min(60.0, float(_ra_raw)))
                                        except ValueError:
                                            pass
                                    try:
                                        _hook(status_code, _ra)
                                    except Exception:
                                        pass
                                delay = self._retry_delay(attempt, response)
                                logger.warning(
                                    "流式请求失败 (HTTP %s)，%.1f 秒后重试 (%s/%s)",
                                    status_code,
                                    delay,
                                    attempt + 1,
                                    self._max_attempts,
                                )
                                self._cancel_event.wait(delay)
                                continue
                            if rate_limited:
                                self._record_rate_limit()
                            # P1-5：响应正文走有界摘要 + 脱敏，用于异常消息
                            body = _summarize_response_body(response.text)
                            # 队列并发优化（§8.2）：把 Retry-After 透传给上层
                            # 共享 ProviderLimiter，使其在 Provider 范围统一 cooldown。
                            retry_after_seconds: float | None = None
                            if rate_limited:
                                raw_retry = response.headers.get("retry-after")
                                if raw_retry:
                                    try:
                                        retry_after_seconds = max(0.0, min(60.0, float(raw_retry)))
                                    except ValueError:
                                        retry_after_seconds = None
                            raise TranslationRequestError(
                                f"API 请求失败 (HTTP {status_code}): {body}",
                                status_code=status_code,
                                retry_after_seconds=retry_after_seconds,
                            )

                        # PERF-001：用列表累积避免字符串拼接 O(n²)
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
                                        choice = choices[0]
                                        # 只在本次请求内记录，供上层（translator）
                                        # 判断是否被输出容量截断。
                                        finish_reason = choice.get("finish_reason")
                                        if isinstance(finish_reason, str) and finish_reason:
                                            self._request_local.last_finish_reason = finish_reason
                                        delta = choice.get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            if first_token_at is None:
                                                first_token_at = time.perf_counter()
                                            content_parts.append(content)
                                            if callback:
                                                callback(content)
                                except json.JSONDecodeError:
                                    continue
                        result = "".join(content_parts)
                        if not result:
                            if attempt < self._max_attempts - 1:
                                self._record_retry()
                                self._cancel_event.wait(self._retry_delay(attempt))
                                continue
                            raise TranslationRequestError("API 未返回任何翻译内容")
                        self._record_success(
                            started_at=started_at,
                            first_token_at=first_token_at or time.perf_counter(),
                            output_text=result,
                        )
                        return result

            except TranslationRequestError:
                raise
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
                if self._cancel_event.is_set():
                    return None
                # 已产生内容时不重试，避免把同一批译文重复回放到 UI。
                if not content_parts and attempt < self._max_attempts - 1:
                    self._record_retry()
                    # Batch-D: notify shared limiter (status_code=0 means timeout/network)
                    _net_hook = self._on_retryable_response
                    if _net_hook is not None:
                        try:
                            _net_hook(0, None)
                        except Exception:
                            pass
                    self._recreate_client_if_safe()
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "网络请求失败: %s，%.1f 秒后重试 (%s/%s)",
                        e,
                        delay,
                        attempt + 1,
                        self._max_attempts,
                    )
                    self._cancel_event.wait(delay)
                    continue
                raise TranslationRequestError(f"网络请求失败: {e}") from e
            except Exception as e:
                if self._cancel_event.is_set():
                    return None
                detail = str(e).strip() or e.__class__.__name__
                # 部分 HTTP/2/代理异常不会被 httpx 归类为 NetworkError，
                # 甚至没有错误文本。尚未收到内容时可安全重放整个请求。
                if not content_parts and attempt < self._max_attempts - 1:
                    self._record_retry()
                    self._recreate_client_if_safe()
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "流式读取失败: %s，%.1f 秒后重试 (%s/%s)",
                        detail,
                        delay,
                        attempt + 1,
                        self._max_attempts,
                    )
                    self._cancel_event.wait(delay)
                    continue
                raise TranslationRequestError(f"流式翻译异常: {detail}") from e
        raise TranslationRequestError("API 重试耗尽")

    # ── 视觉查询 ────────────────────────────────────────

    def vision_query(
        self,
        image_base64: str,
        mime_type: str,
        prompt: str,
        model_override: str | None = None,
    ) -> str | None:
        if not self.api_key:
            logger.warning("API密钥为空")
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
                    # P1-5：响应正文走有界摘要
                    logger.error(
                        "视觉查询失败: %s - %s",
                        resp.status_code,
                        _summarize_response_body(resp.text),
                    )
        except Exception as e:
            if not self._cancel_event.is_set():
                logger.error("视觉查询请求失败: %s", e)
        return None

    # ── 缓存辅助 ──────────────────────────────────────

    def translate_with_cache(self, text: str, context: Dict[str, Any] | None = None) -> str | None:
        if self.cache:
            cached = self.cache.get(text, context)
            if cached:
                return cached
        result = self.translate(text)
        if result and self.cache:
            self.cache.set(text, result, context)
        return result

    def translate_stream_enhanced(
        self,
        text: str,
        callback: Callable[[str], None] | None = None,
        context: Dict[str, Any] | None = None,
        stream_id: str | None = None,
        system_prompt: str | None = None,
    ) -> str | None:
        if not self.enable_stream:
            direct_context = dict(context or {})
            direct_context["system_prompt"] = system_prompt
            result = self._direct_translate(text, direct_context)
            if callback and result:
                callback(result)
            return result
        if self.cache:
            cached = self.cache.get(text, context)
            if cached:
                if callback:
                    # 缓存命中应在返回前同步回放，避免后台回调在任务已经
                    # 完成后继续修改 UI。
                    callback(cached)
                return cached
        if stream_id and callback:
            self.stream_callbacks[stream_id] = callback
        try:
            result = self.translate_stream(text, callback, system_prompt=system_prompt)
            # PERF-009：不缓存取消、空响应的翻译结果
            if result and self.cache and not self._cancel_event.is_set():
                self.cache.set(text, result, context)
            return result
        finally:
            if stream_id:
                self.stream_callbacks.pop(stream_id, None)

    def _simulate_stream_output(
        self, text: str, callback: Callable[[str], None], chunk_size: int = 3, delay: float = 0.05
    ):
        def worker():
            for i in range(0, len(text), chunk_size):
                if self._cancel_event.is_set():
                    break
                callback(text[i : i + chunk_size])
                time.sleep(delay)

        threading.Thread(target=worker, daemon=True).start()

    def cancel_stream(self, stream_id: str):
        self.stream_callbacks.pop(stream_id, None)

    def get_cache_stats(self) -> Dict[str, Any]:
        return self.cache.get_stats() if self.cache else {}

    def clear_cache(self):
        if self.cache:
            self.cache.clear_all()

    def optimize_cache(self):
        if self.cache:
            self.cache.optimize_cache()

    def get_enhanced_stats(self) -> Dict[str, Any]:
        stats = {
            "cache_enabled": self.enable_cache,
            "stream_enabled": self.enable_stream,
            "active_streams": len(self.stream_callbacks),
        }
        if self.cache:
            stats["cache_stats"] = self.get_cache_stats()
        stats["performance"] = self.get_performance_metrics()
        return stats

    def get_performance_metrics(self) -> Dict[str, Any]:
        with self._metrics_lock:
            metrics = dict(self._metrics)
            pressure = self._rate_limit_pressure
        successes = metrics["successful_requests"]
        generation_seconds = metrics["generation_seconds"]
        metrics["average_ttft_seconds"] = metrics["ttft_seconds"] / successes if successes else 0.0
        metrics["average_request_seconds"] = (
            metrics["request_seconds"] / successes if successes else 0.0
        )
        metrics["output_tokens_per_second"] = (
            metrics["output_tokens_estimated"] / generation_seconds
            if generation_seconds > 0
            else 0.0
        )
        metrics["rate_limit_pressure"] = pressure
        return metrics
