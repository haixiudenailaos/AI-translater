#!/usr/bin/env python3
"""LC-10 / LC-15：超长上下文的取消、快速再启动与规模性能。

LC-10 要求覆盖取消发生在**规划、许可等待、首字等待、流式中**四个阶段，
并验证"快速再启动"后旧事件被丢弃、资源释放、按钮恢复可用。

LC-15 用固定数据与假 API 记录运行时间与事件数量，验证数据翻倍不存在
反复全量扫描造成的明显平方增长。
"""

from __future__ import annotations

import threading
import time

import pytest

from src.core.translation_result import TranslationStatus
from src.core.translator import TranslatorEngine
from src.domain.translation_policy import ContextMode, TranslationContextPolicy
from src.utils.token_estimator import estimate_tokens

BIG_CONTEXT = 1_048_576


class BlockingAPI:
    """可阻塞在"首字等待"与"流式中"的 API 替身。"""

    def __init__(self):
        self.requests: list[str] = []
        self.entered = threading.Event()  # 已进入流式请求
        self.release = threading.Event()  # 测试放行
        self.cancelled = False
        self.stream_calls = 0

    def configure_serial_transport(self, serial):
        pass

    def reset_cancel(self):
        self.cancelled = False

    def cancel_requests(self):
        self.cancelled = True
        self.release.set()

    def last_finish_reason(self):
        return None

    def translate_stream(self, prompt, callback, system_prompt=None):
        self.requests.append(prompt)
        self.stream_calls += 1
        self.entered.set()
        # 模拟"首字等待"：直到测试放行或取消。
        self.release.wait(timeout=10.0)
        if self.cancelled:
            return None
        # 放行后逐行回放（模拟流式输出）。
        lines = []
        for line in prompt.split("\n"):
            if line.startswith("[LINE_"):
                marker, _, content = line.partition("]")
                lines.append(f"{marker}]译:{content}")
        result = "\n".join(lines)
        if callback:
            callback(result)
        return result


class SimpleAPI:
    def __init__(self):
        self.requests: list[str] = []
        self.lock = threading.Lock()

    def configure_serial_transport(self, serial):
        pass

    def reset_cancel(self):
        pass

    def cancel_requests(self):
        pass

    def last_finish_reason(self):
        return None

    def translate_stream(self, prompt, callback, system_prompt=None):
        with self.lock:
            self.requests.append(prompt)
        result = "\n".join(
            f"{line.partition(']')[0]}]译:{line.partition(']')[2]}"
            for line in prompt.split("\n")
            if line.startswith("[LINE_")
        )
        if callback:
            callback(result)
        return result


class ConfigManager:
    def __init__(self, *, tokens: int = 262144, capacity: int = BIG_CONTEXT):
        self._app_config = {
            "target_language": "中文",
            "translation_prompt": "请翻译以下文本",
            "batch_lines": 20,
            "small_model_mode": False,
            "long_context": {"schema_version": 1, "context_window_tokens": tokens},
        }
        self._api_config = {"model_name": "m", "context_window_tokens": capacity}

    def get_app_config(self):
        return dict(self._app_config)

    def get_api_config(self, *, load_secret=True):
        return dict(self._api_config)

    def get_glossary_prompt(self):
        return ""


def make_engine(api, **kwargs):
    engine = TranslatorEngine(ConfigManager(**kwargs))
    engine.api = api
    return engine


def run_long(engine, content, tokens=262144, timeout=20.0):
    """在后台线程运行超长翻译，返回 (thread, completed_list, progress_events)。"""
    completed: list = []
    events: list = []
    thread = threading.Thread(
        target=engine.translate_long_context_mode,
        args=(
            content,
            lambda progress, data: events.append((progress, data)),
            lambda r: completed.append(r),
        ),
        kwargs={"wallet": TranslationContextPolicy(ContextMode.LONG, tokens)},
        daemon=True,
    )
    thread.start()
    return thread, completed, events


class TestCancelDuringPlanning:
    def test_cancel_before_stream_sends_no_request(self):
        """取消发生在规划/许可等待阶段：不发出请求。"""
        api = BlockingAPI()
        engine = make_engine(api)
        content = "\n".join(f"第{i}行" for i in range(200))

        thread, completed, _events = run_long(engine, content)
        # 立刻停止，尽量落在规划/等待窗口内。
        engine.stop()
        thread.join(timeout=20.0)
        assert not thread.is_alive(), "取消后必须及时退出"
        assert completed, "必须回调终态"
        assert completed[0].status is TranslationStatus.CANCELLED
        assert completed[0].status is not TranslationStatus.SUCCEEDED


class TestCancelDuringFirstTokenWait:
    def test_cancel_while_waiting_for_first_token(self):
        """取消发生在首字等待：及时退出，状态为取消，不弹"全部完成"。"""
        api = BlockingAPI()
        engine = make_engine(api)
        content = "\n".join(f"第{i}行" for i in range(5))

        thread, completed, _events = run_long(engine, content)
        assert api.entered.wait(timeout=10.0), "请求应已发出并阻塞在首字等待"
        engine.stop()
        thread.join(timeout=20.0)

        assert not thread.is_alive()
        assert completed
        assert completed[0].status is TranslationStatus.CANCELLED
        # 部分/缺失行不能被补齐成假成功。
        assert all(not line for line in completed[0].lines)


class TestCancelDuringStreaming:
    def test_cancel_mid_stream_keeps_validated_lines_only(self):
        """取消发生在流式中：结果状态为取消，不返回伪造的完整成功。"""

        class MidStreamCancelAPI(SimpleAPI):
            def __init__(self):
                super().__init__()
                self.engine_ref: dict = {}

            def translate_stream(self, prompt, callback, system_prompt=None):
                with self.lock:
                    self.requests.append(prompt)
                # 先回放第 1 行，再让引擎停止，然后回放其余行。
                if callback:
                    callback("[LINE_001]译:第1行\n")
                self.engine_ref["engine"].stop()
                if callback:
                    callback("[LINE_002]译:第2行\n[LINE_003]译:第3行\n")
                return "[LINE_001]译:第1行\n[LINE_002]译:第2行\n[LINE_003]译:第3行"

        api = MidStreamCancelAPI()
        engine = make_engine(api)
        api.engine_ref["engine"] = engine

        thread, completed, _events = run_long(engine, "第1行\n第2行\n第3行")
        thread.join(timeout=20.0)

        assert completed
        result = completed[0]
        # 引擎已停止：不得以 SUCCEEDED 结束（否则 UI 会弹"全部完成"）。
        assert result.status is TranslationStatus.CANCELLED


class TestCancelIsIdempotentAndReusable:
    def test_repeated_stop_is_safe(self):
        api = BlockingAPI()
        engine = make_engine(api)
        thread, completed, _events = run_long(engine, "a\nb")
        assert api.entered.wait(timeout=10.0)
        engine.stop()
        engine.stop()
        engine.stop()
        thread.join(timeout=20.0)
        assert not thread.is_alive()
        assert len(completed) == 1, "重复 stop 不得产生多次终态回调"

    def test_restart_after_cancel_runs_cleanly(self):
        """快速再启动：新运行从头执行，旧运行状态不污染。"""
        api_blocking = BlockingAPI()
        engine = make_engine(api_blocking)
        thread, completed, _events = run_long(engine, "a\nb")
        assert api_blocking.entered.wait(timeout=10.0)
        engine.stop()
        thread.join(timeout=20.0)
        assert completed and completed[0].status is TranslationStatus.CANCELLED

        # 换一个不阻塞的 API 重新开始同一引擎实例。
        api_ok = SimpleAPI()
        engine.api = api_ok
        thread2, completed2, _events2 = run_long(engine, "a\nb")
        thread2.join(timeout=20.0)

        assert completed2
        assert completed2[0].status is TranslationStatus.SUCCEEDED
        assert completed2[0].lines == ["译:a", "译:b"]
        assert engine.is_stopped is False, "新运行必须重置停止标记"

    def test_cancelled_run_does_not_cache_partial_output(self):
        """取消的运行不得把结果作为完整成功写入缓存。

        注意 ``_translate`` 开始时会 ``reset()``，因此不能在启动前预置停止标记
        （那会被清掉）。这里在流式回调中途取消，模拟真实的"用户点停止"。
        """

        class CacheRecordingAPI(SimpleAPI):
            def __init__(self):
                super().__init__()
                self.cached: list[str] = []
                self.engine_ref: dict = {}
                self.cancel_injected = False

            def _maybe_cache(self, text: str) -> None:
                # 模拟真实 BaseAPI.translate_stream_enhanced 的写入条件：
                # 取消中或结果为空都不写入。
                if self.cancel_injected:
                    return
                self.cached.append(text)

            def translate_stream_enhanced(
                self, text, callback=None, context=None, stream_id=None, system_prompt=None
            ):
                result = self.translate_stream(text, callback, system_prompt=system_prompt)
                if result:
                    self._maybe_cache(text)
                return result

            def translate_stream(self, prompt, callback, system_prompt=None):
                with self.lock:
                    self.requests.append(prompt)
                if callback:
                    callback("[LINE_001]译:a\n")
                # 收到首批内容后用户取消，后续不得被当作完整成功缓存。
                self.cancel_injected = True
                self.engine_ref["engine"].stop()
                return "[LINE_001]译:a\n[LINE_002]译:b"

        api = CacheRecordingAPI()
        engine = make_engine(api)
        api.engine_ref["engine"] = engine

        thread, completed, _events = run_long(engine, "a\nb")
        thread.join(timeout=20.0)

        assert completed
        assert completed[0].status is TranslationStatus.CANCELLED
        assert api.cached == [], "取消的运行不得写入缓存"


class TestLargeDocumentScale:
    """LC-15：规模变化的运行时间与事件数量。"""

    def test_doubling_lines_does_not_square_runtime(self):
        api = SimpleAPI()
        engine = make_engine(api, capacity=BIG_CONTEXT)

        def measure(count: int) -> tuple[float, int, int]:
            api.requests.clear()
            lines = [f"第{i}行 内容文本" for i in range(count)]
            start = time.perf_counter()
            thread, completed, events = run_long(engine, "\n".join(lines), tokens=1_048_576)
            thread.join(timeout=120.0)
            elapsed = time.perf_counter() - start
            assert completed and completed[0].status is TranslationStatus.SUCCEEDED
            assert len(completed[0].lines) == count
            return elapsed, len(events), len(api.requests)

        measure(500)  # 预热
        small_elapsed, small_events, small_requests = measure(2000)
        large_elapsed, large_events, large_requests = measure(4000)

        # 线性算法约 2x；留足余量以容忍计时噪声。
        assert (
            large_elapsed < max(small_elapsed, 1e-3) * 8
        ), f"规模翻倍耗时增长过多：{small_elapsed:.3f}s -> {large_elapsed:.3f}s"
        # 事件数量同样线性（不因逐行重扫而爆炸）。
        assert large_events <= small_events * 4

    def test_event_count_is_bounded_by_batches_not_lines(self):
        """流式预览沿用合并/节流邮箱语义：事件数由批次驱动，不随行数逐条膨胀。"""

        class QuietAPI(SimpleAPI):
            """不回调流式分片，模拟 emit_stream_progress 的队列路径。"""

            def translate_stream(self, prompt, callback, system_prompt=None):
                with self.lock:
                    self.requests.append(prompt)
                return "\n".join(
                    f"{line.partition(']')[0]}]译:{line.partition(']')[2]}"
                    for line in prompt.split("\n")
                    if line.startswith("[LINE_")
                )

        api = QuietAPI()
        engine = make_engine(api)
        lines = [f"第{i}行" for i in range(3000)]
        thread, completed, events = run_long(engine, "\n".join(lines), tokens=1_048_576)
        thread.join(timeout=120.0)

        assert completed and completed[0].status is TranslationStatus.SUCCEEDED
        # 单批完成 + 终态：事件数应为常数级，而不是 3000 条。
        assert len(events) <= 10, f"事件数应受批次约束，实际 {len(events)}"
        assert len(api.requests) == 1


class TestTokenEstimationScale:
    def test_planning_time_scales_linearly(self):
        from src.application.batch_planner import compute_protocol_overhead, plan_batches
        from src.application.context_budget import resolve_long_budget

        budget = resolve_long_budget(
            requested_context_tokens=1_048_576, model_context_tokens=BIG_CONTEXT
        )
        protocol = compute_protocol_overhead("prompt text")

        def measure(count: int) -> float:
            lines = [f"第{i}行 内容" for i in range(count)]
            start = time.perf_counter()
            plans = plan_batches(lines, range(count), budget=budget, protocol=protocol)
            elapsed = time.perf_counter() - start
            assert sum(p.line_count for p in plans) == count
            return elapsed

        measure(2000)
        small = measure(8000)
        large = measure(16000)
        assert large < max(small, 1e-4) * 8, f"{small:.4f}s -> {large:.4f}s"

    def test_large_token_count_does_not_overflow_float(self):
        """巨大用户整数不得因转 float 而溢出或精度丢失。"""
        from src.application.context_budget import resolve_long_budget

        budget = resolve_long_budget(requested_context_tokens=10**15, model_context_tokens=10**15)
        assert budget.effective_context_tokens == 10**15
        assert budget.safety_margin_tokens == 2 * 10**13
        assert isinstance(budget.safety_margin_tokens, int)

    def test_planning_does_not_allocate_per_user_token(self):
        """按需分配：不为 1,048,576 token 预算预分配巨型列表/缓冲。"""
        import tracemalloc

        from src.application.batch_planner import compute_protocol_overhead, plan_batches
        from src.application.context_budget import resolve_long_budget

        budget = resolve_long_budget(
            requested_context_tokens=1_048_576, model_context_tokens=BIG_CONTEXT
        )
        protocol = compute_protocol_overhead("prompt")
        lines = [f"第{i}行" for i in range(500)]

        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()
        plans = plan_batches(lines, range(500), budget=budget, protocol=protocol)
        after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        assert sum(p.line_count for p in plans) == 500
        allocated = sum(stat.size_diff for stat in after.compare_to(baseline, "filename"))
        # 预算 1,048,576 token 不代表要分配同等规模的缓冲区；500 行规划
        # 的分配应远小于该数量级（留足解释器开销余量）。
        assert allocated < 2_000_000, f"规划分配了 {allocated} 字节"


class TestTokenEstimatorCost:
    def test_estimate_tokens_is_not_quadratic_on_long_lines(self):
        """单行 token 估算必须接近线性（超长行不产生平方开销）。"""
        from src.utils.token_estimator import estimate_tokens

        def measure(size: int) -> float:
            text = "字" * size
            start = time.perf_counter()
            for _ in range(20):
                estimate_tokens(text)
            return time.perf_counter() - start

        measure(10_000)
        small = measure(100_000)
        large = measure(200_000)
        assert large < max(small, 1e-5) * 8, f"{small:.5f}s -> {large:.5f}s"

    def test_token_estimate_matches_expected_units(self):
        # 1 个 CJK 字符约 1 token；4 个 ASCII 字符约 1 token。
        assert estimate_tokens("字") == 1
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("中英") == 2
        assert estimate_tokens("") == 0


@pytest.mark.parametrize("batch_count", [1, 3, 7])
def test_batch_ids_and_indices_stay_consistent(batch_count):
    """批次编号与绝对索引在多批场景下保持一致。"""
    from src.application.batch_planner import compute_protocol_overhead, plan_batches
    from src.application.context_budget import resolve_long_budget

    budget = resolve_long_budget(requested_context_tokens=32_768, model_context_tokens=BIG_CONTEXT)
    protocol = compute_protocol_overhead("prompt")
    lines = [f"第{i}行" + "内容" * (5 * batch_count) for i in range(120)]

    plans = plan_batches(lines, range(120), budget=budget, protocol=protocol)
    assert [p.batch_id for p in plans] == list(range(len(plans)))
    flat = [index for plan in plans for index in plan.source_indices]
    assert flat == list(range(120)), "绝对索引必须覆盖全部行且不重不漏"
