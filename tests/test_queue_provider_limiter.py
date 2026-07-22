#!/usr/bin/env python3
"""共享 ProviderLimiter 单元测试（队列翻译并发优化阶段 3）。

覆盖：
- ``parse_retry_after`` 秒数和 HTTP-date 解析。
- ``try_acquire`` / ``release`` 基础并发计数。
- AIMD：429 立即减半，连续成功阈值后 +1。
- ``Retry-After`` 优先于指数退避；无 Retry-After 进入 cooldown。
- RPM/TPM 滑动窗口限流。
- ``ProviderLimiterRegistry`` 跨任务共享同一 key 的 Limiter。
"""

import time
from datetime import datetime, timedelta, timezone

from src.core.queue_provider import (
    ProviderLimiter,
    ProviderLimiterRegistry,
    ProviderRuntimeKey,
    credential_reference_for,
    parse_retry_after,
)


def _make_key(provider: str = "siliconflow") -> ProviderRuntimeKey:
    return ProviderRuntimeKey(
        provider=provider,
        normalized_base_url="https://api.siliconflow.cn/v1",
        model_name="deepseek-ai/DeepSeek-V3.2",
        credential_reference="abc12345",
        config_version="0.3:4000",
    )


def _make_limiter(
    *,
    configured_max: int = 4,
    hard_cap: int = 8,
    rpm_limit: int = 0,
    tpm_limit: int = 0,
    clock=None,
    wall_clock=None,
) -> ProviderLimiter:
    return ProviderLimiter(
        _make_key(),
        configured_max=configured_max,
        hard_cap=hard_cap,
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        clock=clock or time.monotonic,
        wall_clock=wall_clock or time.time,
    )


# ── parse_retry_after ────────────────────────────────────


class TestParseRetryAfter:
    def test_seconds_integer(self):
        assert parse_retry_after("30") == 30.0

    def test_seconds_float(self):
        assert parse_retry_after("1.5") == 1.5

    def test_empty_returns_none(self):
        assert parse_retry_after("") is None
        assert parse_retry_after(None) is None
        assert parse_retry_after("   ") is None

    def test_negative_returns_none(self):
        assert parse_retry_after("-5") is None

    def test_http_date_future(self):
        # 构造一个未来 30 秒的 HTTP-date
        target = datetime.now(timezone.utc) + timedelta(seconds=30)
        from email.utils import format_datetime

        raw = format_datetime(target, usegmt=True)
        result = parse_retry_after(raw)
        assert result is not None
        # 允许 ±2 秒误差（解析与计算之间有时间差）
        assert 28.0 <= result <= 32.0

    def test_http_date_past_clamped_to_zero(self):
        target = datetime.now(timezone.utc) - timedelta(seconds=30)
        from email.utils import format_datetime

        raw = format_datetime(target, usegmt=True)
        result = parse_retry_after(raw)
        assert result == 0.0

    def test_invalid_string_returns_none(self):
        assert parse_retry_after("not a date") is None


# ── try_acquire / release ────────────────────────────────


class TestAcquireRelease:
    def test_acquire_until_configured_max(self):
        limiter = _make_limiter(configured_max=3, hard_cap=8)
        assert limiter.try_acquire() is True
        assert limiter.try_acquire() is True
        assert limiter.try_acquire() is True
        # 第 4 个超出 configured_max
        assert limiter.try_acquire() is False
        assert limiter.in_flight == 3

    def test_release_frees_slot(self):
        limiter = _make_limiter(configured_max=2, hard_cap=8)
        assert limiter.try_acquire()
        assert limiter.try_acquire()
        assert not limiter.try_acquire()
        limiter.release()
        assert limiter.try_acquire()

    def test_release_idempotent(self):
        limiter = _make_limiter()
        # 多次 release 不应让 in_flight 变负
        limiter.release()
        limiter.release()
        assert limiter.in_flight == 0

    def test_release_notifies_waiter(self):
        limiter = _make_limiter(configured_max=1, hard_cap=8)
        assert limiter.try_acquire()
        # 在另一个线程等待容量
        import threading

        result = {}

        def _waiter():
            result["got"] = limiter.wait_for_capacity_or_command(timeout=2.0)

        t = threading.Thread(target=_waiter)
        t.start()
        time.sleep(0.05)  # 让 waiter 进入等待
        limiter.release()
        t.join(timeout=2.0)
        assert result.get("got") is True

    def test_blocking_acquire_waits_for_shared_slot(self):
        limiter = _make_limiter(configured_max=1, hard_cap=1)
        assert limiter.try_acquire()
        started = time.monotonic()
        acquired = {}

        import threading

        def _waiter():
            acquired["value"] = limiter.acquire(poll_interval=0.01)
            acquired["waited"] = time.monotonic() - started

        thread = threading.Thread(target=_waiter)
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()

        limiter.release()
        thread.join(timeout=2.0)

        assert acquired["value"] is True
        assert acquired["waited"] >= 0.04
        limiter.release()

    def test_blocking_acquire_can_be_cancelled(self):
        limiter = _make_limiter(configured_max=1, hard_cap=1)
        assert limiter.try_acquire()
        cancelled = [False]
        result = {}

        import threading

        thread = threading.Thread(
            target=lambda: result.setdefault(
                "value",
                limiter.acquire(cancelled=lambda: cancelled[0], poll_interval=0.01),
            )
        )
        thread.start()
        time.sleep(0.03)
        cancelled[0] = True
        thread.join(timeout=2.0)

        assert result["value"] is False
        assert limiter.in_flight == 1
        limiter.release()


# ── 主界面 + 队列公平份额 ───────────────────────────────


class TestFairConsumerShares:
    def test_main_editor_alone_uses_all_four_global_slots(self):
        limiter = _make_limiter(configured_max=4, hard_cap=4)
        limiter.register_consumer("main", priority=0)

        assert all(limiter.try_acquire(consumer_id="main") for _ in range(4))
        assert not limiter.try_acquire(consumer_id="main")

        for _ in range(4):
            limiter.release(consumer_id="main")
        limiter.unregister_consumer("main")

    def test_main_editor_and_one_queue_split_four_slots_evenly(self):
        limiter = _make_limiter(configured_max=4, hard_cap=4)
        # Register the queue first to prove main priority is independent of timing.
        limiter.register_consumer("queue:1", priority=10)
        limiter.register_consumer("main", priority=0)

        assert limiter.try_acquire(consumer_id="main")
        assert limiter.try_acquire(consumer_id="main")
        assert not limiter.try_acquire(consumer_id="main")
        assert limiter.try_acquire(consumer_id="queue:1")
        assert limiter.try_acquire(consumer_id="queue:1")
        assert not limiter.try_acquire(consumer_id="queue:1")
        assert limiter.in_flight == 4

    def test_four_queues_each_receive_one_slot(self):
        limiter = _make_limiter(configured_max=4, hard_cap=4)
        queue_ids = [f"queue:{index}" for index in range(4)]
        for queue_id in queue_ids:
            limiter.register_consumer(queue_id, priority=10)

        assert all(limiter.try_acquire(consumer_id=queue_id) for queue_id in queue_ids)
        assert all(not limiter.try_acquire(consumer_id=queue_id) for queue_id in queue_ids)
        assert limiter.in_flight == 4

    def test_consumer_without_unsent_work_releases_its_unused_share(self):
        limiter = _make_limiter(configured_max=4, hard_cap=4)
        limiter.register_consumer("main", priority=0)
        limiter.register_consumer("queue:1", priority=10)

        assert limiter.try_acquire(consumer_id="main")
        assert limiter.try_acquire(consumer_id="queue:1")
        assert limiter.try_acquire(consumer_id="queue:1")
        assert not limiter.try_acquire(consumer_id="queue:1")

        limiter.set_consumer_demand("main", False)

        assert limiter.try_acquire(consumer_id="queue:1")
        assert limiter.in_flight == 4

    def test_later_queues_inherit_slots_in_registration_order(self):
        limiter = _make_limiter(configured_max=4, hard_cap=4)
        queue_ids = [f"queue:{index}" for index in range(8)]
        for queue_id in queue_ids:
            limiter.register_consumer(queue_id, priority=10)

        assert all(limiter.try_acquire(consumer_id=queue_id) for queue_id in queue_ids[:4])
        assert all(not limiter.try_acquire(consumer_id=queue_id) for queue_id in queue_ids[4:])

        limiter.unregister_consumer(queue_ids[0])
        limiter.release(consumer_id=queue_ids[0])

        assert limiter.try_acquire(consumer_id=queue_ids[4])
        assert not limiter.try_acquire(consumer_id=queue_ids[5])

    def test_unregister_cleans_consumer_after_its_in_flight_request_finishes(self):
        limiter = _make_limiter(configured_max=1, hard_cap=1)
        limiter.register_consumer("queue:1")
        assert limiter.try_acquire(consumer_id="queue:1")

        limiter.unregister_consumer("queue:1")
        limiter.release(consumer_id="queue:1")

        assert limiter.metrics()["consumers"] == []


# ── AIMD ─────────────────────────────────────────────────


class TestAimd:
    def test_429_halves_current_limit(self):
        limiter = _make_limiter(configured_max=4, hard_cap=8)
        assert limiter.current_limit == 4
        limiter.record_rate_limited(retry_after_seconds=10.0)
        assert limiter.current_limit == 2
        # 再次 429 减半，不低于 1
        limiter.record_rate_limited(retry_after_seconds=5.0)
        assert limiter.current_limit == 1

    def test_429_blocked_until(self):
        limiter = _make_limiter(configured_max=4, hard_cap=8)
        limiter.record_rate_limited(retry_after_seconds=10.0)
        assert limiter.is_blocked() is True
        assert limiter.cooldown_remaining() > 0
        # cooldown 期间 try_acquire 失败
        assert limiter.try_acquire() is False
        assert limiter.available_capacity == 0

    def test_success_recovery_adds_one_after_threshold(self):
        from src.core.queue_provider import _RECOVERY_SUCCESS_THRESHOLD

        limiter = _make_limiter(configured_max=4, hard_cap=8)
        limiter.record_rate_limited(retry_after_seconds=1.0)
        assert limiter.current_limit == 2
        # 等待 cooldown 结束
        # 用 mock clock 推进时间
        clock = [1000.0]
        limiter2 = ProviderLimiter(
            _make_key(),
            configured_max=4,
            hard_cap=8,
            clock=lambda: clock[0],
            wall_clock=lambda: clock[0],
        )
        limiter2.record_rate_limited(retry_after_seconds=10.0)
        assert limiter2.current_limit == 2
        clock[0] += 11.0  # 推进到 cooldown 结束后
        # 连续成功达到阈值
        for _ in range(_RECOVERY_SUCCESS_THRESHOLD):
            limiter2.record_success()
        assert limiter2.current_limit == 3  # +1

    def test_recovery_does_not_exceed_configured_max(self):
        from src.core.queue_provider import _RECOVERY_SUCCESS_THRESHOLD

        clock = [1000.0]
        limiter = ProviderLimiter(
            _make_key(),
            configured_max=2,
            hard_cap=8,
            clock=lambda: clock[0],
            wall_clock=lambda: clock[0],
        )
        # 已经在 configured_max，连续成功不应超过
        for _ in range(_RECOVERY_SUCCESS_THRESHOLD * 2):
            limiter.record_success()
        assert limiter.current_limit == 2

    def test_timeout_resets_consecutive_successes(self):
        limiter = _make_limiter(configured_max=4, hard_cap=8)
        for _ in range(15):
            limiter.record_success()
        assert limiter.metrics()["consecutive_successes"] == 15
        limiter.record_timeout()
        assert limiter.metrics()["consecutive_successes"] == 0
        assert limiter.metrics()["total_timeouts"] == 1


# ── RPM/TPM ──────────────────────────────────────────────


class TestRpmTpm:
    def test_rpm_rejects_beyond_limit(self):
        limiter = _make_limiter(configured_max=10, hard_cap=20, rpm_limit=2)
        assert limiter.try_acquire()
        assert limiter.try_acquire()
        # 第 3 个超出 RPM
        assert limiter.try_acquire() is False

    def test_rpm_window_resets(self):
        wall = [1000.0]
        limiter = ProviderLimiter(
            _make_key(),
            configured_max=10,
            hard_cap=20,
            rpm_limit=2,
            clock=lambda: wall[0],
            wall_clock=lambda: wall[0],
        )
        assert limiter.try_acquire()
        assert limiter.try_acquire()
        assert not limiter.try_acquire()
        # 推进 60 秒，窗口重置
        wall[0] += 61.0
        assert limiter.try_acquire()

    def test_tpm_rejects_beyond_limit(self):
        limiter = _make_limiter(configured_max=10, hard_cap=20, tpm_limit=1000)
        # 第一次：500 tokens
        assert limiter.try_acquire(estimated_tokens=500)
        # 第二次：600 tokens，总 1100 超出 1000
        assert limiter.try_acquire(estimated_tokens=600) is False


# ── Registry ─────────────────────────────────────────────


class TestRegistry:
    def test_same_key_returns_same_limiter(self):
        registry = ProviderLimiterRegistry()
        key = _make_key()
        l1 = registry.get_or_create(key, configured_max=4, hard_cap=8)
        l2 = registry.get_or_create(key, configured_max=4, hard_cap=8)
        assert l1 is l2

    def test_different_keys_returns_different_limiters(self):
        registry = ProviderLimiterRegistry()
        key1 = _make_key(provider="siliconflow")
        key2 = _make_key(provider="deepseek")
        l1 = registry.get_or_create(key1, configured_max=4, hard_cap=8)
        l2 = registry.get_or_create(key2, configured_max=4, hard_cap=8)
        assert l1 is not l2

    def test_different_providers_share_one_application_wide_budget(self):
        registry = ProviderLimiterRegistry()
        limiter_a = registry.get_or_create(
            _make_key(provider="siliconflow"),
            configured_max=4,
            hard_cap=4,
        )
        limiter_b = registry.get_or_create(
            _make_key(provider="deepseek"),
            configured_max=4,
            hard_cap=4,
        )
        limiter_a.register_consumer("main", priority=0)
        limiter_b.register_consumer("queue:1", priority=10)

        assert limiter_a.try_acquire(consumer_id="main")
        assert limiter_a.try_acquire(consumer_id="main")
        assert not limiter_a.try_acquire(consumer_id="main")
        assert limiter_b.try_acquire(consumer_id="queue:1")
        assert limiter_b.try_acquire(consumer_id="queue:1")
        assert not limiter_b.try_acquire(consumer_id="queue:1")
        assert limiter_a.in_flight + limiter_b.in_flight == 4

    def test_update_configured_max_on_existing(self):
        registry = ProviderLimiterRegistry()
        key = _make_key()
        l1 = registry.get_or_create(key, configured_max=4, hard_cap=8)
        # 再次获取，configured_max 不同
        l2 = registry.get_or_create(key, configured_max=6, hard_cap=8)
        assert l1 is l2
        assert l2.current_limit <= 6  # 应被新的 configured_max 约束

    def test_global_setting_can_raise_previous_hard_cap(self):
        registry = ProviderLimiterRegistry()
        key = _make_key()
        limiter = registry.get_or_create(key, configured_max=4, hard_cap=4)

        updated = registry.get_or_create(key, configured_max=8, hard_cap=8)

        assert updated is limiter
        assert updated.metrics()["hard_cap"] == 8
        assert updated.current_limit == 8


# ── credential_reference_for ─────────────────────────────


def test_credential_reference_stable_and_non_sensitive():
    cfg = {"api_key": "sk-abc123"}
    ref1 = credential_reference_for(cfg)
    ref2 = credential_reference_for(cfg)
    assert ref1 == ref2  # 稳定
    assert len(ref1) == 8  # SHA256 前 8 位
    assert "sk-abc123" not in ref1  # 不泄漏密钥


def test_credential_reference_empty_for_no_key():
    assert credential_reference_for({}) == ""
    assert credential_reference_for({"api_key": ""}) == ""
    assert credential_reference_for({"api_key": "   "}) == ""
