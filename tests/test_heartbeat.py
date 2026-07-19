#!/usr/bin/env python3
"""
心跳线程安全测试

验证 R2-BUG-023 修复：心跳线程不得关闭正在被翻译线程使用的 HTTP 客户端。
- 活动请求期间心跳失败不会重建客户端
- 无活动请求时心跳失败可以重建客户端
- 流式请求持续超过多个心跳周期仍能正常完成
"""

import threading
import time

import httpx
import pytest

from src.api.siliconflow_api import SiliconFlowAPI


def _make_siliconflow(config=None) -> SiliconFlowAPI:
    """构造一个禁用心跳的 SiliconFlowAPI 实例。"""
    cfg = {
        "api_key": "test-key",
        "base_url": "https://fake.example.com/v1",
        "model_name": "test-model",
        "enable_heartbeat": False,
        "enable_batch": False,
        "enable_cache": False,
    }
    if config:
        cfg.update(config)
    return SiliconFlowAPI(cfg)


# ── 活动请求计数 ──────────────────────────────────────


class TestActiveRequestTracking:
    """R2-BUG-023：活动请求计数器"""

    def test_active_requests_starts_at_zero(self):
        """初始化时活动请求计数为 0"""
        api = _make_siliconflow()
        try:
            assert api._active_requests == 0
        finally:
            api.close()

    def test_using_client_increments_counter(self):
        """_using_client 上下文管理器增加和减少计数"""
        api = _make_siliconflow()
        try:
            assert api._active_requests == 0
            with api._using_client() as client:
                assert api._active_requests == 1
                assert client is not None
            assert api._active_requests == 0
        finally:
            api.close()

    def test_using_client_decrements_on_exception(self):
        """异常时计数器也能正确减少"""
        api = _make_siliconflow()
        try:
            with pytest.raises(RuntimeError):
                with api._using_client():
                    assert api._active_requests == 1
                    raise RuntimeError("test")
            assert api._active_requests == 0
        finally:
            api.close()


# ── 心跳安全重建 ──────────────────────────────────────


class TestHeartbeatSafeRecreate:
    """R2-BUG-023：心跳不得关闭活动客户端"""

    def test_safe_recreate_skipped_when_active(self):
        """有活动请求时 _recreate_client_if_safe 不重建"""
        api = _make_siliconflow()
        try:
            original_client = api._current_client
            assert original_client is not None

            with api._using_client():
                api._recreate_client_if_safe()
                # 客户端未被重建（因为活动请求 > 0）
                assert api._current_client is original_client
        finally:
            api.close()

    def test_safe_recreate_when_idle(self):
        """无活动请求时 _recreate_client_if_safe 正常重建"""
        api = _make_siliconflow()
        try:
            original_client = api._current_client
            api._recreate_client_if_safe()
            # 客户端已重建（因为活动请求 == 0）
            assert api._current_client is not original_client
        finally:
            api.close()

    def test_unsafe_recreate_closes_active_client(self):
        """_recreate_client（非安全版）会直接关闭活动客户端（对照测试）"""
        api = _make_siliconflow()
        try:
            original_client = api._current_client
            with api._using_client():
                # 非安全重建会直接关闭，即使有活动请求
                api._recreate_client()
                assert api._current_client is not original_client
                assert original_client.is_closed
        finally:
            api.close()


def test_cancel_does_not_close_client_created_by_the_next_run():
    """Cancellation detaches its own client before blocking on close()."""
    api = _make_siliconflow()

    class BlockingClient:
        is_closed = False

        def __init__(self):
            self.close_started = threading.Event()
            self.allow_close = threading.Event()

        def close(self):
            self.close_started.set()
            self.allow_close.wait(timeout=2.0)
            self.is_closed = True

    class FreshClient:
        is_closed = False

        def close(self):
            self.is_closed = True

    old_client = BlockingClient()
    fresh_client = FreshClient()
    api._current_client.close()
    api._current_client = old_client
    api._build_client = lambda: fresh_client

    cancel_thread = threading.Thread(target=api.cancel_requests, daemon=True)
    cancel_thread.start()
    assert old_client.close_started.wait(timeout=1.0)

    api.reset_cancel()
    assert api._get_client() is fresh_client
    old_client.allow_close.set()
    cancel_thread.join(timeout=1.0)

    assert old_client.is_closed
    assert fresh_client.is_closed is False
    api.close()


# ── 心跳与翻译并发 ────────────────────────────────────


class TestHeartbeatDuringTranslation:
    """R2-BUG-023：翻译期间心跳不会中断"""

    def test_heartbeat_failure_during_stream_does_not_close_client(self):
        """流式翻译期间心跳失败不会关闭客户端"""
        api = _make_siliconflow()

        # 模拟一个长时间运行的流式请求
        stream_started = threading.Event()
        stream_completed = threading.Event()
        heartbeat_fired = threading.Event()

        original_get_client = api._get_client

        def mock_stream_translate():
            """模拟流式翻译，期间触发心跳失败"""
            with api._using_client() as client:
                stream_started.set()
                # 等待心跳线程触发
                heartbeat_fired.wait(timeout=2.0)
                # 客户端应该仍然可用（未被心跳关闭）
                assert not client.is_closed, "客户端被心跳关闭了"
                stream_completed.set()
                return "translated"

        # 启动心跳模拟线程
        def heartbeat_worker():
            stream_started.wait(timeout=2.0)
            # 模拟心跳失败
            api._recreate_client_if_safe()
            heartbeat_fired.set()

        try:
            hb_thread = threading.Thread(target=heartbeat_worker, daemon=True)
            hb_thread.start()

            result = mock_stream_translate()
            hb_thread.join(timeout=2.0)

            assert result == "translated"
            assert stream_completed.is_set()
        finally:
            api.close()

    def test_long_stream_survives_multiple_heartbeats(self):
        """流式请求持续超过多个心跳周期仍能正常完成"""
        api = _make_siliconflow(config={"heartbeat_interval": 0.1})

        # 手动启动心跳
        api._heartbeat_stop_event.clear()
        api._start_heartbeat()

        # 模拟 HEAD 请求始终失败
        original_head = None
        if api._current_client:
            original_head = api._current_client.head

        call_count = {"n": 0}

        try:
            # 让心跳的 HEAD 请求始终失败
            def failing_head(*args, **kwargs):
                call_count["n"] += 1
                raise httpx.ConnectError("simulated heartbeat failure")

            # 替换当前客户端的 head 方法
            client = api._current_client
            if client:
                client.head = failing_head

            # 模拟一个持续 0.5 秒的流式请求（跨越多个心跳周期）
            with api._using_client() as active_client:
                # 等待多个心跳周期
                time.sleep(0.5)
                # 活动客户端应该未被关闭
                assert not active_client.is_closed, "活动客户端在多个心跳周期后被关闭"

            # 心跳应该已触发多次
            assert call_count["n"] >= 2, f"心跳只触发了 {call_count['n']} 次，预期至少 2 次"
        finally:
            api.close()
