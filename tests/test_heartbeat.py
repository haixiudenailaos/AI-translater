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
from unittest.mock import patch

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


# ── PERF §10.3：延迟创建批处理器 ──────────────────────


class TestLazyBatchProcessor:
    """PERF §10.3：批处理器延迟创建测试。

    验收要求：
    - 创建 API Client 不创建无用后台 timer 或 executor。
    - ``translate_batch()`` 首次调用线程安全地初始化处理器。
    - ``close()`` 不初始化尚未使用的资源。
    - 测试连接不额外常驻一个 Client（不创建批处理器）。
    """

    def test_init_does_not_create_batch_processor(self):
        """PERF §10.3：构造 API 实例不创建批处理器（避免无用后台线程常驻）"""
        # enable_batch=True 时也应延迟创建，不在构造期创建
        api = _make_siliconflow(config={"enable_batch": True})
        try:
            # 批处理器尚未创建
            assert api.batch_processor is None, (
                "构造 API 实例时不应立即创建 BatchProcessor，"
                "主文本翻译路径使用 translate_stream_enhanced()，"
                "提前创建会带来无用 ThreadPoolExecutor 后台线程。"
            )
            # 配置已保存为延迟创建所需的 dict
            assert api._batch_config is not None
            assert api._batch_config["max_batch_size"] == 10
        finally:
            api.close()

    def test_close_without_batch_use_does_not_initialize(self):
        """PERF §10.3：close() 不初始化尚未使用的资源"""
        api = _make_siliconflow(config={"enable_batch": True})
        # 从未调用 translate_batch()
        assert api.batch_processor is None
        # close() 不应触发批处理器创建
        api.close()
        assert api.batch_processor is None, (
            "close() 不应初始化尚未使用的批处理器，避免在关闭路径创建后台线程。"
        )

    def test_test_connection_does_not_create_batch_processor(self):
        """PERF §10.3：test_connection() 不创建批处理器"""
        api = _make_siliconflow(config={"enable_batch": True})
        try:
            # test_connection 会失败（无真实 API），但不应创建批处理器
            api.test_connection()
            assert api.batch_processor is None, "test_connection() 不应触发批处理器创建。"
        finally:
            api.close()

    def test_translate_batch_creates_processor_lazily(self):
        """PERF §10.3：首次 translate_batch() 调用线程安全地初始化处理器"""
        api = _make_siliconflow(config={"enable_batch": True})
        try:
            assert api.batch_processor is None
            # 模拟 _direct_translate 避免真实网络调用
            with patch.object(api, "_direct_translate", return_value="译文"):
                results = api.translate_batch(["hello"], [{}])
            # 批处理器已创建
            assert api.batch_processor is not None, (
                "首次 translate_batch() 调用应线程安全地创建批处理器。"
            )
            # 翻译结果正确
            assert results == ["译文"]
        finally:
            api.close()

    def test_concurrent_translate_batch_creates_single_processor(self):
        """PERF §10.3：并发 translate_batch() 调用只创建一个批处理器（双重检查锁）"""
        api = _make_siliconflow(config={"enable_batch": True})
        try:
            barrier = threading.Barrier(8)

            def call_translate():
                barrier.wait()
                with patch.object(api, "_direct_translate", return_value="译文"):
                    api.translate_batch(["text"], [{}])

            threads = [threading.Thread(target=call_translate) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

            # 并发调用后只创建了一个批处理器
            assert api.batch_processor is not None
            # 验证未重复创建：记录当前实例后再次调用，应保持同一实例
            first = api.batch_processor
            with patch.object(api, "_direct_translate", return_value="译文"):
                api.translate_batch(["text"], [{}])
            assert api.batch_processor is first, "双重检查锁应确保并发调用只创建一个批处理器实例。"
        finally:
            api.close()

    def test_disabled_batch_never_creates_processor(self):
        """PERF §10.3：enable_batch=False 时 translate_batch() 不创建处理器"""
        api = _make_siliconflow(config={"enable_batch": False})
        try:
            assert api._batch_config is None
            # 模拟 _direct_translate 避免真实网络调用
            with patch.object(api, "_direct_translate", return_value="译文"):
                results = api.translate_batch(["hello"], [{}])
            # 批处理器仍未创建（走 translate_with_cache 回退路径）
            assert api.batch_processor is None
            assert results == ["译文"]
        finally:
            api.close()
