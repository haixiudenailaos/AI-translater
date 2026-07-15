import json
import threading
import time

import pytest

from src.api.base_api import BaseAPI
from src.config.translation_profile import (
    DEEPSEEK_V4_FLASH_MODEL,
    MAX_STABLE_TRANSLATION_BATCH_LINES,
    apply_text_translation_profile,
)
from src.api.deepseek_api import DeepseekAPI
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
    config = PerformanceConfig(batch_lines=20)
    engine = TranslatorEngine(config)
    fixed = estimate_tokens(engine._translation_system_prompt(
        "中文", config.app["translation_prompt"], config.get_glossary_prompt()
    ))
    lines = ["中" * 50] * 4

    ranges = engine._build_batch_ranges(lines, 20, fixed + 65)

    assert ranges == [(0, 1), (1, 2), (2, 3), (3, 4)]


@pytest.mark.parametrize("model", [
    "stepfun-ai/Step-3.5-Flash",
    "Qwen/Qwen3.5-397B-A17B",
    "deepseek-ai/DeepSeek-V3.2",
])
def test_translation_profile_preserves_model_and_forces_streaming(model):
    config = apply_text_translation_profile({
        "provider": "siliconflow",
        "model_name": model,
        "enable_stream": False,
    })

    assert config["model_name"] == model
    assert config["enable_stream"] is True


def test_deepseek_official_default_is_v4_flash():
    assert DeepseekAPI.DEFAULT_MODEL == DEEPSEEK_V4_FLASH_MODEL


def test_legacy_deepseek_official_model_is_migrated_to_v4_flash():
    config = apply_text_translation_profile({
        "provider": "deepseek",
        "model_name": "deepseek-ai/DeepSeek-V3.2-Exp",
    })

    assert config["model_name"] == DEEPSEEK_V4_FLASH_MODEL
    assert config["enable_stream"] is True


def test_large_configured_batches_are_split_into_stable_stream_requests():
    config = PerformanceConfig(concurrency=1, batch_lines=200, input_budget=64000)
    api = ConcurrentApi()
    engine = TranslatorEngine(config)
    engine.api = api
    completed = []
    lines = [f"line-{index}" for index in range(45)]

    engine._translate(
        "\n".join(lines), lambda *_: None, completed.append, batch_delay=0
    )

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
        batch_delay=0,
    )

    assert api.max_active == 2
    assert completed[0].status == OperationStatus.SUCCEEDED
    assert completed[0].lines == [f"译:line-{index}" for index in range(6)]
    assert all("只翻译，不解释" in prompt for prompt in api.system_prompts)
    later_batch_streams = [
        data for _, data in progress_events
        if data.get("streaming") and data.get("batch_start") == 2
    ]
    assert later_batch_streams
    assert all(data["display_batch_start"] == 0 for data in later_batch_streams)


def test_provider_recommendation_can_reduce_concurrency():
    config = PerformanceConfig(concurrency=4, batch_lines=1)
    api = ConcurrentApi(recommended=1)
    engine = TranslatorEngine(config)
    engine.api = api

    engine._translate("a\nb\nc", lambda *_: None, lambda *_: None, batch_delay=0)

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
    api, client = make_api([
        FakeResponse(429, headers={"retry-after": "0"}, body="busy"),
        FakeResponse(chunks=[sse("译文"), "data: [DONE]"]),
    ])
    try:
        result = api.translate_stream("原文", system_prompt="翻译规则")
        metrics = api.get_performance_metrics()

        assert result == "译文"
        assert len(client.requests) == 2
        assert client.requests[0]["messages"][0] == {
            "role": "system", "content": "翻译规则"
        }
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
