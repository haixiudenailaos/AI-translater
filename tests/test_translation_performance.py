import json
import threading
import time

import pytest

from src.api.base_api import BaseAPI
from src.api.deepseek_api import DeepseekAPI
from src.config.translation_profile import (
    DEEPSEEK_V4_FLASH_MODEL,
    MAX_STABLE_TRANSLATION_BATCH_LINES,
    apply_text_translation_profile,
)
from src.core.translator import TranslatorEngine
from src.domain.errors import TranslationRequestError
from src.domain.translation import OperationStatus
from src.utils.token_estimator import estimate_tokens


class PerformanceConfig:
    def __init__(self, *, concurrency=2, batch_lines=2, input_budget=12000):
        self.app = {
            "target_language": "中文",
            "translation_prompt": "只翻译，不解释。",
            "batch_lines": batch_lines,
            "batch_max_input_tokens": input_budget,
            "translation_concurrency": concurrency,
        }
        self.api = {
            "provider": "test",
            "model_name": "test-model",
            "temperature": 0.0,
            "max_tokens": 2000,
            "context_window_tokens": 16000,
        }

    def get_app_config(self):
        return dict(self.app)

    def get_api_config(self):
        return dict(self.api)

    def get_glossary_prompt(self):
        return "术语：A -> 甲"


class ConcurrentApi:
    def __init__(self, recommended=None):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.system_prompts = []
        self.recommended = recommended

    def recommended_concurrency(self, configured):
        return self.recommended or configured

    def translate_stream_enhanced(
        self, text, callback, context, stream_id=None, system_prompt=None
    ):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.system_prompts.append(system_prompt)
        try:
            # Let a second worker enter, then finish batches out of order.
            time.sleep(0.03 if "line-0" in text else 0.01)
            translated = []
            for line in text.splitlines():
                marker, source = line[:10], line[10:]
                translated.append(f"{marker}译:{source}")
            result = "\n".join(translated)
            callback(result)
            return result
        finally:
            with self.lock:
                self.active -= 1

    def reset_cancel(self):
        pass

    def cancel_requests(self):
        pass


def test_token_budget_builds_smaller_batches():
    """PERF §9.6：_build_batch_ranges 返回 TranslationBatchPlan 列表。

    token 预算受限时每批只放 1 行，且每个 plan 携带正确的 start/end/batch_id。
    """
    from src.core.translator import TranslationBatchPlan

    config = PerformanceConfig(batch_lines=20)
    engine = TranslatorEngine(config)
    fixed = estimate_tokens(
        engine._translation_system_prompt(
            "中文", config.app["translation_prompt"], config.get_glossary_prompt()
        )
    )
    lines = ["中" * 50] * 4

    plans = engine._build_batch_ranges(lines, 20, fixed + 65)

    # §9.6：返回 TranslationBatchPlan 而非裸 tuple
    assert all(isinstance(p, TranslationBatchPlan) for p in plans)
    # 区间语义不变：每批 1 行
    assert [(p.start, p.end) for p in plans] == [(0, 1), (1, 2), (2, 3), (3, 4)]
    # batch_id 从 0 单调递增
    assert [p.batch_id for p in plans] == [0, 1, 2, 3]
    # estimated_input_tokens 含协议开销，必然大于 fixed_tokens
    assert all(p.estimated_input_tokens >= fixed for p in plans)


@pytest.mark.parametrize(
    "model",
    [
        "stepfun-ai/Step-3.5-Flash",
        "Qwen/Qwen3.5-397B-A17B",
        "deepseek-ai/DeepSeek-V3.2",
    ],
)
def test_translation_profile_preserves_model_and_forces_streaming(model):
    config = apply_text_translation_profile(
        {
            "provider": "siliconflow",
            "model_name": model,
            "enable_stream": False,
        }
    )

    assert config["model_name"] == model
    assert config["enable_stream"] is True


def test_deepseek_official_default_is_v4_flash():
    assert DeepseekAPI.DEFAULT_MODEL == DEEPSEEK_V4_FLASH_MODEL


def test_legacy_deepseek_official_model_is_migrated_to_v4_flash():
    config = apply_text_translation_profile(
        {
            "provider": "deepseek",
            "model_name": "deepseek-ai/DeepSeek-V3.2-Exp",
        }
    )

    assert config["model_name"] == DEEPSEEK_V4_FLASH_MODEL
    assert config["enable_stream"] is True


def test_large_configured_batches_are_split_into_stable_stream_requests():
    config = PerformanceConfig(concurrency=1, batch_lines=200, input_budget=64000)
    api = ConcurrentApi()
    engine = TranslatorEngine(config)
    engine.api = api
    completed = []
    lines = [f"line-{index}" for index in range(45)]

    engine._translate("\n".join(lines), lambda *_: None, completed.append)

    assert completed[0].status == OperationStatus.SUCCEEDED
    assert len(api.system_prompts) == 3
    assert MAX_STABLE_TRANSLATION_BATCH_LINES == 20


def test_queue_bulk_mode_uses_one_large_batch_without_stream_progress():
    config = PerformanceConfig(concurrency=1, batch_lines=10, input_budget=6000)
    api = ConcurrentApi()
    engine = TranslatorEngine(config)
    engine.api = api
    completed = []
    progress_events = []
    lines = [f"line-{index}" for index in range(45)]

    engine.translate_bulk_mode(
        "\n".join(lines),
        lambda progress, data: progress_events.append((progress, data)),
        completed.append,
    )

    assert completed[0].status == OperationStatus.SUCCEEDED
    assert completed[0].lines == [f"译:line-{index}" for index in range(45)]
    assert len(api.system_prompts) == 1
    assert progress_events
    assert all(not data.get("streaming") for _, data in progress_events)


def test_progress_callback_runs_outside_progress_lock():
    """PERF：外部 progress_callback 必须在 progress_lock 释放后调用。

    行为验证：让 batch 0 的完成 callback 阻塞，同时让 batch 1 在工作线程中
    产生流式进度。若 progress_lock 在 callback 期间被持有，batch 1 的
    batch_progress 会阻塞等锁，其流式进度无法在 batch 0 阻塞期间到达。
    修复后 batch 1 的流式进度应能及时到达，证明 callback 在锁外执行。
    """
    config = PerformanceConfig(concurrency=2, batch_lines=1)

    class OrderedApi(ConcurrentApi):
        """让包含 'line-0' 的批次先完成，其他批次延迟完成。"""

        def translate_stream_enhanced(
            self, text, callback, context, stream_id=None, system_prompt=None
        ):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.system_prompts.append(system_prompt)
            try:
                # batch 0 快速完成；batch 1 慢一点，保证 batch 0 先进入 callback
                time.sleep(0.01 if "line-0" in text else 0.05)
                translated = []
                for line in text.splitlines():
                    marker, source = line[:10], line[10:]
                    translated.append(f"{marker}译:{source}")
                result = "\n".join(translated)
                callback(result)
                return result
            finally:
                with self.lock:
                    self.active -= 1

    api = OrderedApi()
    engine = TranslatorEngine(config)
    engine.api = api

    batch0_completion_started = threading.Event()
    batch0_can_finish = threading.Event()
    batch1_stream_seen_during_block = threading.Event()

    def progress_cb(progress, data):
        if not data:
            return
        if data.get("batch_start") == 0 and not data.get("streaming"):
            # batch 0 完成事件：阻塞，等待 batch 1 的流式进度到达
            batch0_completion_started.set()
            batch1_stream_seen_during_block.wait(timeout=2.0)
            batch0_can_finish.set()
        elif data.get("batch_start") == 1 and data.get("streaming"):
            # batch 1 的流式进度：若在 batch 0 callback 阻塞期间到达，证明锁已释放
            if batch0_completion_started.is_set() and not batch0_can_finish.is_set():
                batch1_stream_seen_during_block.set()

    engine._translate(
        "\n".join(f"line-{i}" for i in range(2)),
        progress_cb,
        lambda *_: None,
    )

    assert batch1_stream_seen_during_block.is_set(), (
        "batch 1 的流式进度未能在 batch 0 callback 阻塞期间到达，"
        "说明 progress_lock 在 callback 期间被持有，阻塞了其他批次"
    )


def test_concurrent_batches_write_back_in_source_order():
    config = PerformanceConfig(concurrency=2, batch_lines=2)
    api = ConcurrentApi()
    engine = TranslatorEngine(config)
    engine.api = api
    completed = []
    progress_events = []

    engine._translate(
        "\n".join(f"line-{index}" for index in range(6)),
        lambda progress, data: progress_events.append((progress, data)),
        completed.append,
    )

    assert api.max_active == 2
    assert completed[0].status == OperationStatus.SUCCEEDED
    assert completed[0].lines == [f"译:line-{index}" for index in range(6)]
    assert all("只翻译，不解释" in prompt for prompt in api.system_prompts)
    later_batch_streams = [
        data
        for _, data in progress_events
        if data.get("streaming") and data.get("batch_start") == 2
    ]
    assert later_batch_streams
    assert all(data["display_batch_start"] == 0 for data in later_batch_streams)


def test_main_translation_uses_only_capacity_left_by_background_queue():
    """主编辑器和后台队列共享最大在途请求数，而不是各自占满上限。"""
    from src.core.queue_provider import ProviderLimiterRegistry

    config = PerformanceConfig(concurrency=2, batch_lines=1)
    config.app.update(
        {
            "queue_max_in_flight_requests": 2,
            "queue_hard_request_cap": 2,
        }
    )
    registry = ProviderLimiterRegistry()
    api = ConcurrentApi()
    engine = TranslatorEngine(config, limiter_registry=registry)
    engine.api = api

    # 模拟后台队列已有一个真实在途请求；主编辑器局部并发虽为 2，
    # 此时也只能使用剩余的一个 Provider 槽位。
    limiter = engine._get_shared_limiter(config.get_api_config())
    assert limiter is not None
    assert limiter.try_acquire(estimated_tokens=100)
    completed = []
    try:
        engine._translate(
            "line-0\nline-1\nline-2",
            lambda *_: None,
            completed.append,
        )

        assert completed[0].status == OperationStatus.SUCCEEDED
        assert api.max_active == 1
        assert limiter.in_flight == 1
    finally:
        limiter.release()


def test_provider_recommendation_can_reduce_concurrency():
    config = PerformanceConfig(concurrency=4, batch_lines=1)
    api = ConcurrentApi(recommended=1)
    engine = TranslatorEngine(config)
    engine.api = api

    engine._translate("a\nb\nc", lambda *_: None, lambda *_: None)

    assert api.max_active == 1


class FakeResponse:
    def __init__(self, status_code=200, chunks=(), headers=None, body=""):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = headers or {}
        self._body = body
        self.text = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body.encode()

    def iter_lines(self):
        return iter(self._chunks)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.is_closed = False

    def stream(self, method, url, headers, json):
        self.requests.append(json)
        return self.responses.pop(0)

    def close(self):
        self.is_closed = True


def make_api(responses, **overrides):
    config = {
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
        "model_name": "test-model",
        "enable_cache": False,
        "enable_batch": False,
        "api_max_attempts": 3,
        "retry_base_delay": 0,
        **overrides,
    }
    api = BaseAPI(config)
    api._current_client.close()
    fake_client = FakeClient(responses)
    api._current_client = fake_client
    return api, fake_client


def sse(content):
    payload = json.dumps({"choices": [{"delta": {"content": content}}]})
    return f"data: {payload}"


def test_api_retries_429_once_and_sends_system_message():
    api, client = make_api(
        [
            FakeResponse(429, headers={"retry-after": "0"}, body="busy"),
            FakeResponse(chunks=[sse("译文"), "data: [DONE]"]),
        ]
    )
    try:
        result = api.translate_stream("原文", system_prompt="翻译规则")
        metrics = api.get_performance_metrics()

        assert result == "译文"
        assert len(client.requests) == 2
        assert client.requests[0]["messages"][0] == {"role": "system", "content": "翻译规则"}
        assert client.requests[0]["stream"] is True
        assert metrics["retries"] == 1
        assert metrics["rate_limit_errors"] == 1
        assert metrics["successful_requests"] == 1
        assert api.recommended_concurrency(4) == 1
    finally:
        api.close()


def test_api_does_not_retry_non_retryable_http_error():
    api, client = make_api([FakeResponse(401, body="unauthorized")])
    try:
        with pytest.raises(TranslationRequestError) as exc_info:
            api.translate_stream("原文")
        assert exc_info.value.status_code == 401
        assert len(client.requests) == 1
    finally:
        api.close()


def test_split_http_timeouts_are_applied():
    api, _ = make_api([], http_connect_timeout=7, http_read_timeout=181)
    try:
        assert api._http_timeout.connect == 7
        assert api._http_timeout.read == 181
    finally:
        api.close()


# ── PERF-010：BatchProcessor 行为测试 ──────────────────


class TestBatchProcessorBehavior:
    """PERF-010：max_batch_size 和 max_wait_time 的真实行为测试"""

    def test_max_batch_size_triggers_immediate_flush(self):
        """提交 max_batch_size 条请求时立即刷新，不等待 max_wait_time"""
        from src.core.batch_processor import BatchProcessor

        received_batches = []
        batch_event = threading.Event()

        def handler(texts, contexts):
            received_batches.append(list(texts))
            batch_event.set()
            return [f"translated:{t}" for t in texts]

        bp = BatchProcessor(max_batch_size=3, max_wait_time=10.0, max_workers=2)
        try:
            bp.set_api_handler(handler)
            futures = [bp.submit_request(f"text{i}", {}) for i in range(3)]
            assert batch_event.wait(timeout=2.0), "max_batch_size 未触发立即刷新"

            results = [f.result(timeout=1) for f in futures]
            assert results == ["translated:text0", "translated:text1", "translated:text2"]
            assert len(received_batches) == 1
            assert received_batches[0] == ["text0", "text1", "text2"]
        finally:
            bp.close()

    def test_max_wait_time_triggers_delayed_flush(self):
        """不足 max_batch_size 时，max_wait_time 后自动刷新"""
        from src.core.batch_processor import BatchProcessor

        received_batches = []
        batch_event = threading.Event()

        def handler(texts, contexts):
            received_batches.append(list(texts))
            batch_event.set()
            return [f"translated:{t}" for t in texts]

        bp = BatchProcessor(max_batch_size=10, max_wait_time=0.3, max_workers=2)
        try:
            bp.set_api_handler(handler)
            start = time.time()
            futures = [bp.submit_request(f"text{i}", {}) for i in range(2)]
            assert batch_event.wait(timeout=2.0), "max_wait_time 未触发延迟刷新"
            elapsed = time.time() - start
            assert elapsed >= 0.2, f"刷新过早: {elapsed:.3f}s"

            results = [f.result(timeout=1) for f in futures]
            assert results == ["translated:text0", "translated:text1"]
            assert len(received_batches) == 1
        finally:
            bp.close()

    def test_flush_immediately_processes_pending(self):
        """flush() 立即处理所有待处理请求，不等待 max_wait_time"""
        from src.core.batch_processor import BatchProcessor

        received_batches = []
        batch_event = threading.Event()

        def handler(texts, contexts):
            received_batches.append(list(texts))
            batch_event.set()
            return [f"translated:{t}" for t in texts]

        bp = BatchProcessor(max_batch_size=10, max_wait_time=10.0, max_workers=2)
        try:
            bp.set_api_handler(handler)
            futures = [bp.submit_request(f"text{i}", {}) for i in range(3)]
            start = time.time()
            bp.flush()
            assert batch_event.wait(timeout=2.0), "flush 未立即处理待处理请求"
            elapsed = time.time() - start
            assert elapsed < 1.0, f"flush 耗时过长: {elapsed:.3f}s"

            results = [f.result(timeout=1) for f in futures]
            assert results == ["translated:text0", "translated:text1", "translated:text2"]
            assert len(received_batches) == 1
        finally:
            bp.close()

    def test_results_distributed_to_correct_futures(self):
        """每条请求的结果正确返回到对应的 Future"""
        from src.core.batch_processor import BatchProcessor

        def handler(texts, contexts):
            return [f"result_{t}" for t in texts]

        bp = BatchProcessor(max_batch_size=5, max_wait_time=0.1, max_workers=2)
        try:
            bp.set_api_handler(handler)
            futures = [bp.submit_request(f"req{i}", {"idx": i}) for i in range(5)]
            bp.flush()
            results = [f.result(timeout=2) for f in futures]
            assert results == [f"result_req{i}" for i in range(5)]
        finally:
            bp.close()

    def test_handler_exception_propagates_to_futures(self):
        """handler 异常时，异常传播到所有相关 Future"""
        from src.core.batch_processor import BatchProcessor

        def handler(texts, contexts):
            raise RuntimeError("batch failed")

        bp = BatchProcessor(max_batch_size=3, max_wait_time=0.1, max_workers=2)
        try:
            bp.set_api_handler(handler)
            futures = [bp.submit_request(f"text{i}", {}) for i in range(3)]
            bp.flush()
            for f in futures:
                with pytest.raises(RuntimeError, match="batch failed"):
                    f.result(timeout=2)
        finally:
            bp.close()

    def test_multiple_batches_split_by_max_batch_size(self):
        """超过 max_batch_size 的请求分成多个批次处理"""
        from src.core.batch_processor import BatchProcessor

        received_batches = []
        lock = threading.Lock()

        def handler(texts, contexts):
            with lock:
                received_batches.append(list(texts))
            return [f"ok:{t}" for t in texts]

        bp = BatchProcessor(max_batch_size=3, max_wait_time=0.1, max_workers=2)
        try:
            bp.set_api_handler(handler)
            futures = [bp.submit_request(f"t{i}", {}) for i in range(7)]
            bp.flush()
            results = [f.result(timeout=2) for f in futures]
            assert results == [f"ok:t{i}" for i in range(7)]
            assert len(received_batches) >= 2
            all_texts = []
            for batch in received_batches:
                all_texts.extend(batch)
            assert sorted(all_texts) == [f"t{i}" for i in range(7)]
        finally:
            bp.close()


# ── PERF §9.7：移除固定批次等待后的性能测试 ──────────────


class InstantApi:
    """即时完成的 Fake API：用于验证批次间不再有固定 sleep。

    每次 ``translate_stream_enhanced`` 同步完成，无延迟。
    """

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.system_prompts = []

    def recommended_concurrency(self, configured):
        return configured

    def translate_stream_enhanced(
        self, text, callback, context, stream_id=None, system_prompt=None
    ):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.system_prompts.append(system_prompt)
        try:
            translated = []
            for line in text.splitlines():
                marker, source = line[:10], line[10:]
                translated.append(f"{marker}译:{source}")
            result = "\n".join(translated)
            callback(result)
            return result
        finally:
            with self.lock:
                self.active -= 1

    def reset_cancel(self):
        pass

    def cancel_requests(self):
        pass


def test_instant_batches_no_longer_incur_fixed_sleep():
    """§9.7：100 个即时完成 Fake 批次不再产生约 10 秒固定等待。

    旧实现 ``batch_delay=0.1`` 会让 100 批累计约 10 秒。移除后应在秒级内
    完成，耗时主要来自线程调度本身。
    """
    # 100 行，batch_lines=1 → 100 批。旧实现每批 sleep 0.1s = 10s。
    config = PerformanceConfig(concurrency=1, batch_lines=1, input_budget=12000)
    api = InstantApi()
    engine = TranslatorEngine(config)
    engine.api = api
    completed = []

    start = time.time()
    engine._translate(
        "\n".join(f"line-{i}" for i in range(100)),
        lambda *_: None,
        completed.append,
    )
    elapsed = time.time() - start

    assert completed[0].status == OperationStatus.SUCCEEDED
    # 100 批固定等待本应累计 ~10 秒；移除后应远低于此阈值。
    # 留出线程调度余量，5 秒已足够区分"是否仍有固定 sleep"。
    assert elapsed < 5.0, f"100 批耗时 {elapsed:.2f}s，疑似仍存在固定 sleep"


def test_run_context_constructed_once_not_per_batch():
    """§9.7：运行上下文只构造一次，glossary prompt 不按批次数重复构造。

    验证方式：``_translate_batch`` 收到的 ``system_prompt`` 在所有批次中
    为同一字符串实例（``run_context.system_prompt`` 复用），而非每批重新
    构造的新字符串。
    """
    config = PerformanceConfig(concurrency=1, batch_lines=2, input_budget=12000)
    api = InstantApi()
    engine = TranslatorEngine(config)
    engine.api = api
    completed = []

    engine._translate(
        "\n".join(f"line-{i}" for i in range(6)),  # 3 批
        lambda *_: None,
        completed.append,
    )

    assert completed[0].status == OperationStatus.SUCCEEDED
    # 3 批均收到 system_prompt
    assert len(api.system_prompts) == 3
    # 运行上下文复用：所有批次的 system_prompt 应为同一对象
    first_prompt = api.system_prompts[0]
    assert all(p is first_prompt for p in api.system_prompts), (
        "system_prompt 在批次间未复用同一实例，说明运行上下文未被共享"
    )


def test_run_context_temperature_and_versions_reused_across_batches():
    """PERF §9.5 D2/D3：temperature / prompt_version / glossary_version 一次构造并复用。

    验证 ``_translate_batch`` 的 cache_context 中这三项在所有批次中值一致，
    且 prompt_version / glossary_version 为预期的 SHA256[:16] 哈希，
    证明它们来自 ``_translate`` 一次构造的 ``TranslationRunContext``，而非
    每批重读 api_config 和重算哈希。
    """
    import hashlib

    class ContextRecordingApi(ConcurrentApi):
        def __init__(self):
            super().__init__()
            self.contexts = []

        def translate_stream_enhanced(
            self, text, callback, context, stream_id=None, system_prompt=None
        ):
            self.contexts.append(dict(context))
            return super().translate_stream_enhanced(
                text,
                callback,
                context,
                stream_id=stream_id,
                system_prompt=system_prompt,
            )

    config = PerformanceConfig(concurrency=1, batch_lines=2, input_budget=12000)
    api = ContextRecordingApi()
    engine = TranslatorEngine(config)
    engine.api = api
    completed = []

    engine._translate(
        "\n".join(f"line-{i}" for i in range(6)),  # 3 批
        lambda *_: None,
        completed.append,
    )

    assert completed[0].status == OperationStatus.SUCCEEDED
    assert len(api.contexts) == 3

    expected_temp = config.api["temperature"]
    expected_prompt_version = hashlib.sha256(
        config.app["translation_prompt"].encode("utf-8")
    ).hexdigest()[:16]
    expected_glossary_version = hashlib.sha256(
        config.get_glossary_prompt().encode("utf-8")
    ).hexdigest()[:16]

    for ctx in api.contexts:
        assert ctx["temperature"] == expected_temp
        assert ctx["prompt_version"] == expected_prompt_version
        assert ctx["glossary_version"] == expected_glossary_version


def test_run_context_is_frozen_dataclass():
    """§9.5：TranslationRunContext 是不可变 dataclass。"""
    from src.core.translator import TranslationRunContext

    ctx = TranslationRunContext(
        provider="test",
        model_name="test-model",
        target_language="中文",
        base_prompt="",
        glossary_prompt="",
        system_prompt=None,
        is_hunyuan=False,
    )
    with pytest.raises(Exception):
        ctx.target_language = "English"  # type: ignore[misc]


def test_batch_plan_is_frozen_dataclass():
    """§9.6：TranslationBatchPlan 是不可变 dataclass。"""
    from src.core.translator import TranslationBatchPlan

    plan = TranslationBatchPlan(batch_id=0, start=0, end=10, estimated_input_tokens=500)
    with pytest.raises(Exception):
        plan.end = 20  # type: ignore[misc]
