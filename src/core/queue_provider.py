#!/usr/bin/env python3
"""共享 Provider 限流器与运行时（队列翻译并发优化阶段 3）。

职责（见 QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §8）：
- ``ProviderLimiter`` 是并发、速率和 cooldown 的唯一 owner。
- Worker 只上报结果（成功/429/超时/Retry-After），由 Limiter 统一决策。
- 等待 Limiter 时不得持有任务注册表锁、进度锁或 Tk 相关锁。
- AIMD：429 立即 ``current_limit //= 2``；连续成功后每次 +1，不跳回上限。
- ``Retry-After`` 同时支持秒数和 HTTP-date（RFC 7231）。
- 退避复用 ``src/application/error_handling.py`` 的 ``RetryPolicy`` 思路，
  避免维护第二套不一致实现。

范围说明（§5.3）：
- 本轮保持每任务 API 客户端（``BaseAPI`` 实例），不在队列中共享 ``httpx.Client``。
  原因：``BaseAPI.cancel_requests()`` 会关闭客户端，共享客户端需要请求级取消改造，
  风险较大，留待后续单独进行。
- Limiter 仍按 ``(provider, normalized_base_url, model_name)`` 键跨任务共享，
  实现 429/RPM/TPM/AIMD 的统一协调——这是阶段 3 的核心收益。
- 终态（完成/取消/错误/移除/重启）必须 ``close()`` 各任务自己的 API 客户端，
  避免连接和心跳线程累积。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable

from ..application.error_handling import RetryPolicy
from ..utils.logger import get_logger

logger = get_logger(__name__)

# AIMD 恢复：连续成功多少次后 current_limit +1。
# 见 §8.2「连续 20 个成功请求，且无 429、超时率低、P95 未明显恶化时，增加 1」。
_RECOVERY_SUCCESS_THRESHOLD = 20

# 无 Retry-After 时的 cooldown 基础秒数（§8.2：15-30 秒 cooldown）。
_COOLDOWN_BASE_SECONDS = 15.0
_COOLDOWN_MAX_SECONDS = 30.0

# 默认 cooldown 后的指数退避策略（复用 RetryPolicy 思路，避免第二套实现）。
_DEFAULT_BACKOFF = RetryPolicy(
    max_attempts=4,
    base_delay_seconds=_COOLDOWN_BASE_SECONDS,
    max_delay_seconds=_COOLDOWN_MAX_SECONDS,
    jitter_ratio=0.15,  # §8.2：10%-20% jitter
)


@dataclass(frozen=True, slots=True)
class ProviderRuntimeKey:
    """Provider 运行时的非敏感键（§5.3）。

    不包含明文 API Key；``credential_reference`` 是密钥存储的非敏感句柄
    （例如 keyring service name 的稳定哈希），用于区分不同账号。
    """

    provider: str
    normalized_base_url: str
    model_name: str
    credential_reference: str
    config_version: str

    def __str__(self) -> str:
        # 用于日志/指标，确保不泄漏密钥。
        return (
            f"ProviderRuntimeKey(provider={self.provider!r}, "
            f"base_url={self.normalized_base_url!r}, "
            f"model={self.model_name!r}, "
            f"credential={self.credential_reference!r}, "
            f"config_version={self.config_version!r})"
        )


@dataclass(slots=True)
class _LimiterState:
    """Limiter 内部可变状态，受 ``ProviderLimiter._lock`` 保护。"""

    current_limit: int
    configured_max: int
    hard_cap: int
    blocked_until: float = 0.0  # monotonic 时间戳，0 表示未阻塞
    consecutive_successes: int = 0
    total_429: int = 0
    total_timeouts: int = 0
    total_success: int = 0
    # cooldown 次数，用于指数退避计算（无 Retry-After 时）。
    cooldown_round: int = 0
    # 最近一次 cooldown 的截止时间，用于 ETA/UI 显示。
    cooldown_started_at: float = 0.0


def parse_retry_after(value: str | None) -> float | None:
    """解析 ``Retry-After`` 头，支持秒数和 HTTP-date（RFC 7231）。

    返回相对当前需要等待的秒数；解析失败或非正值返回 ``None``。
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    # 1. 尝试整数/浮点秒数。
    try:
        seconds = float(raw)
        if seconds >= 0:
            return seconds
        return None
    except ValueError:
        pass
    # 2. 尝试 HTTP-date。
    try:
        target_dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target_dt is None:
        return None
    # HTTP-date 是 UTC，与 time.time() 比较需转为 timestamp。
    try:
        target_ts = target_dt.timestamp()
    except (OSError, OverflowError, ValueError):
        return None
    delta = target_ts - time.time()
    return max(0.0, delta)


class ProviderLimiter:
    """单一 Provider 的并发/速率/cooldown owner（§8）。

    线程模型：
    - ``acquire_slot`` / ``release_slot``：Worker 在提交请求前后调用，
      维护在途计数，不超过 ``current_limit``。
    - ``record_success`` / ``record_rate_limited`` / ``record_timeout``：
      Worker 上报结果，Limiter 更新 AIMD 状态。
    - ``current_limit`` / ``is_blocked`` / ``cooldown_remaining``：
      Coordinator 在调度前查询，决定派发数量。

    约束：等待 ``acquire_slot`` 时不持有任务注册表锁或 Tk 锁。
    本实现采用非阻塞 ``try_acquire``，由 Coordinator 决定是否等待，
    避免在 Limiter 内部阻塞调度线程。
    """

    def __init__(
        self,
        key: ProviderRuntimeKey,
        *,
        configured_max: int,
        hard_cap: int,
        rpm_limit: int = 0,
        tpm_limit: int = 0,
        backoff: RetryPolicy | None = None,
        clock: callable = time.monotonic,
        wall_clock: callable = time.time,
    ) -> None:
        if configured_max < 1 or hard_cap < 1:
            raise ValueError("configured_max 和 hard_cap 必须 >= 1")
        if configured_max > hard_cap:
            raise ValueError("configured_max 不能超过 hard_cap")
        self._key = key
        self._clock = clock
        self._wall_clock = wall_clock
        self._backoff = backoff or _DEFAULT_BACKOFF
        self._lock = threading.Lock()
        self._state = _LimiterState(
            current_limit=min(configured_max, hard_cap),
            configured_max=configured_max,
            hard_cap=hard_cap,
        )
        self._in_flight = 0
        # RPM/TPM token bucket（可选，0 表示不限制）。
        self._rpm_limit = max(0, int(rpm_limit))
        self._tpm_limit = max(0, int(tpm_limit))
        self._rpm_window_start = self._wall_clock()
        self._rpm_count = 0
        self._tpm_window_start = self._wall_clock()
        self._tpm_tokens = 0
        # 唤醒通道：cooldown 结束或槽位释放时通知 Coordinator。
        self._wake_event = threading.Event()

    # ── 查询（Coordinator 调度前调用） ───────────────────

    @property
    def key(self) -> ProviderRuntimeKey:
        return self._key

    @property
    def current_limit(self) -> int:
        with self._lock:
            return self._state.current_limit

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def available_capacity(self) -> int:
        """当前可派发槽位 = current_limit - in_flight（不小于 0）。"""
        with self._lock:
            if self._clock() < self._state.blocked_until:
                return 0
            return max(0, self._state.current_limit - self._in_flight)

    def is_blocked(self) -> bool:
        with self._lock:
            return self._clock() < self._state.blocked_until

    def cooldown_remaining(self) -> float:
        """距离 cooldown 结束的剩余秒数，未阻塞返回 0。"""
        with self._lock:
            remaining = self._state.blocked_until - self._clock()
            return max(0.0, remaining)

    def metrics(self) -> dict:
        """返回当前指标的不可变快照（用于 UI/日志）。"""
        with self._lock:
            s = self._state
            return {
                "provider": self._key.provider,
                "model": self._key.model_name,
                "current_limit": s.current_limit,
                "configured_max": s.configured_max,
                "hard_cap": s.hard_cap,
                "in_flight": self._in_flight,
                "blocked": self._clock() < s.blocked_until,
                "cooldown_remaining": max(0.0, s.blocked_until - self._clock()),
                "consecutive_successes": s.consecutive_successes,
                "total_429": s.total_429,
                "total_timeouts": s.total_timeouts,
                "total_success": s.total_success,
            }

    # ── 槽位获取（Worker 调用） ──────────────────────────

    def try_acquire(self, *, estimated_tokens: int = 0) -> bool:
        """非阻塞尝试获取一个请求槽位。

        成功返回 True；被 cooldown 阻塞、达到 current_limit 或 RPM/TPM 超限
        返回 False。Coordinator 在派发前调用，避免在 Limiter 内部阻塞。

        RPM/TPM（可选，配置为 0 时不限制）：
        - RPM：滑动 60s 窗口内的请求计数；
        - TPM：滑动 60s 窗口内的预估 token 总量。
        窗口到期时自动重置。``estimated_tokens`` 在获取时预扣，超限则拒绝。
        """
        with self._lock:
            now = self._clock()
            wall_now = self._wall_clock()
            if now < self._state.blocked_until:
                return False
            if self._in_flight >= self._state.current_limit:
                return False
            # RPM 检查
            if self._rpm_limit > 0:
                if wall_now - self._rpm_window_start >= 60.0:
                    self._rpm_window_start = wall_now
                    self._rpm_count = 0
                if self._rpm_count + 1 > self._rpm_limit:
                    return False
            # TPM 检查
            if self._tpm_limit > 0:
                if wall_now - self._tpm_window_start >= 60.0:
                    self._tpm_window_start = wall_now
                    self._tpm_tokens = 0
                if self._tpm_tokens + max(0, int(estimated_tokens)) > self._tpm_limit:
                    return False
            # 通过：扣减预算
            self._in_flight += 1
            if self._rpm_limit > 0:
                self._rpm_count += 1
            if self._tpm_limit > 0:
                self._tpm_tokens += max(0, int(estimated_tokens))
            return True

    def acquire(
        self,
        *,
        estimated_tokens: int = 0,
        cancelled: Callable[[], bool] | None = None,
        poll_interval: float = 0.1,
    ) -> bool:
        """等待并获取一个请求槽位。

        主编辑器拥有自己的批次执行池，不能像队列 Coordinator 一样在派发前
        轮询容量，因此通过此入口在真正发起网络请求前等待共享额度。等待期间
        不持有调用方锁，并定期检查取消状态。
        """
        interval = max(0.01, float(poll_interval))
        while cancelled is None or not cancelled():
            if self.try_acquire(estimated_tokens=estimated_tokens):
                return True
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()
        return False

    def release(self) -> None:
        """释放一个请求槽位。幂等：与 try_acquire 配对调用。"""
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            # 释放槽位后通知等待的 Coordinator。
        self._wake_event.set()

    # ── 结果上报（Worker 调用） ──────────────────────────

    def record_success(self) -> None:
        """上报一次成功请求，可能触发 AIMD 恢复（+1）。"""
        with self._lock:
            s = self._state
            s.total_success += 1
            s.consecutive_successes += 1
            # 恢复窗口：连续成功达到阈值，且未在 cooldown，每次 +1。
            if (
                s.consecutive_successes >= _RECOVERY_SUCCESS_THRESHOLD
                and s.current_limit < s.configured_max
                and self._clock() >= s.blocked_until
            ):
                s.current_limit = min(s.configured_max, s.current_limit + 1)
                s.consecutive_successes = 0
                logger.info(
                    "ProviderLimiter 恢复并发至 %d（上限 %d）: %s",
                    s.current_limit,
                    s.configured_max,
                    self._key,
                )
                # 恢复后重置 cooldown 轮次，让下次 429 重新从基础退避开始。
                s.cooldown_round = 0

    def record_rate_limited(self, *, retry_after_seconds: float | None = None) -> None:
        """上报一次 429。立即减半 current_limit 并设置 blocked_until。

        - 有 ``Retry-After``：整个 Provider 在该时间前不提交新请求。
        - 无 ``Retry-After``：进入 15-30s cooldown + 指数退避 + jitter。
        - 已在途请求允许结束，不关闭客户端（§8.2）。
        """
        with self._lock:
            s = self._state
            s.total_429 += 1
            s.consecutive_successes = 0
            # AIMD 降级：立即减半，不低于 1。
            s.current_limit = max(1, s.current_limit // 2)
            now = self._clock()
            if retry_after_seconds is not None and retry_after_seconds > 0:
                wait_seconds = float(retry_after_seconds)
                s.cooldown_round = 0  # 明确的 Retry-After 不计入指数退避轮次
            else:
                # 无 Retry-After：指数退避 + jitter，基于 cooldown_round。
                s.cooldown_round += 1
                # 复用 RetryPolicy.delay_for 的指数退避 + jitter 逻辑。
                wait_seconds = self._backoff.delay_for(s.cooldown_round - 1)
                # RetryPolicy 在 max_delay 上叠加 jitter，最终值可能略超出
                # Provider cooldown 的 30 秒硬上限；共享限流器需再次裁剪。
                wait_seconds = min(_COOLDOWN_MAX_SECONDS, wait_seconds)
            s.blocked_until = now + wait_seconds
            s.cooldown_started_at = now
            logger.warning(
                "ProviderLimiter 收到 429，current_limit=%d，cooldown %.1fs: %s",
                s.current_limit,
                wait_seconds,
                self._key,
            )

    def record_timeout(self) -> None:
        """上报一次超时。计入指标但暂不直接降级（避免与 429 叠加过激）。"""
        with self._lock:
            s = self._state
            s.total_timeouts += 1
            # 超时连续多次可视为 Provider 压力，重置成功计数，
            # 阻止立即恢复，但不直接减半（让 429 主导降级）。
            s.consecutive_successes = 0

    def reset_pressure(self) -> None:
        """重置 AIMD 状态到初始值（配置变化或测试用）。"""
        with self._lock:
            s = self._state
            s.current_limit = min(s.configured_max, s.hard_cap)
            s.blocked_until = 0.0
            s.consecutive_successes = 0
            s.cooldown_round = 0
        self._wake_event.set()

    def update_configured_max(self, new_max: int) -> None:
        """配置变化时更新 configured_max，current_limit 同步调整。"""
        if new_max < 1:
            return
        with self._lock:
            s = self._state
            s.configured_max = min(new_max, s.hard_cap)
            # current_limit 不超过新的 configured_max；也不低于 1。
            s.current_limit = max(1, min(s.current_limit, s.configured_max))
        self._wake_event.set()

    # ── 等待（Coordinator 调用） ─────────────────────────

    def wait_for_capacity_or_command(self, timeout: float) -> bool:
        """阻塞等待直到有容量可用或超时。

        Coordinator 在无容量且无完成事件时调用，避免忙轮询。
        返回 True 表示可能已有容量（需重新查询）；False 表示超时。
        """
        self._wake_event.clear()
        # 二次检查：可能在 clear 之前已被 set。
        if self.available_capacity > 0:
            return True
        return self._wake_event.wait(timeout=timeout)


class ProviderLimiterRegistry:
    """按 ``ProviderRuntimeKey`` 共享 ``ProviderLimiter``（§8.1）。

    多个任务使用同一 Provider 时共享同一个 Limiter，实现 429/RPM/TPM/AIMD
    的跨任务统一协调。线程安全。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limiters: dict[ProviderRuntimeKey, ProviderLimiter] = {}

    def get_or_create(
        self,
        key: ProviderRuntimeKey,
        *,
        configured_max: int,
        hard_cap: int,
        rpm_limit: int = 0,
        tpm_limit: int = 0,
    ) -> ProviderLimiter:
        with self._lock:
            limiter = self._limiters.get(key)
            if limiter is None:
                limiter = ProviderLimiter(
                    key,
                    configured_max=configured_max,
                    hard_cap=hard_cap,
                    rpm_limit=rpm_limit,
                    tpm_limit=tpm_limit,
                )
                self._limiters[key] = limiter
            else:
                # 已存在：更新 configured_max（用户可能改了设置）。
                limiter.update_configured_max(configured_max)
            return limiter

    def all_metrics(self) -> list[dict]:
        with self._lock:
            return [limiter.metrics() for limiter in self._limiters.values()]

    def close_all(self) -> None:
        """关闭时清理。Limiter 本身不持有客户端，此处仅清理引用。"""
        with self._lock:
            self._limiters.clear()


def credential_reference_for(api_config: dict) -> str:
    """从 API 配置生成非敏感的 credential_reference。

    用于 ``ProviderRuntimeKey``，区分不同账号但不泄漏密钥。
    使用 ``api_key`` 的 SHA256 前 8 位作为稳定句柄；无 key 时返回空串。
    """
    import hashlib

    key = (api_config.get("api_key") or "").strip()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def provider_runtime_key_for(api_config: dict) -> ProviderRuntimeKey:
    """为主编辑器和队列构造完全一致的 Provider 运行时键。"""
    from ..config.translation_profile import normalize_openai_base_url

    provider = api_config.get("provider", "siliconflow")
    base_url = api_config.get("base_url", "")
    try:
        normalized_url = normalize_openai_base_url(base_url) if base_url else ""
    except ValueError:
        # Never retain an unvalidated URL in the runtime key.  Besides making
        # invalid configurations look usable, that would preserve a remote
        # plain-HTTP endpoint next to credential-related runtime metadata.
        normalized_url = ""
    config_version = f"{api_config.get('temperature', '')}:{api_config.get('max_tokens', '')}"
    return ProviderRuntimeKey(
        provider=provider,
        normalized_base_url=normalized_url,
        model_name=api_config.get("model_name", ""),
        credential_reference=credential_reference_for(api_config),
        config_version=config_version,
    )
