#!/usr/bin/env python3
"""LC-03 / LC-07 / LC-08：主界面引擎的超长上下文真实请求路径。

本文件通过 ``_translate`` / ``translate_long_context_mode`` 的**真实**执行路径
捕获 provider 实际收到的请求，而不是只断言策略对象或常量的值：

- LC-03：同一输入分别普通/超长运行，超长真实请求超过 20 行与 6,000 输入
  token；普通模式仍遵守原限制。
- LC-07：1,001 行、多字节、emoji、稀疏缺失行；行号解析与绝对映射正确。
- LC-08：跨 chunk 标记、重复/缺失/越界标记、``finish_reason=length``。

夹具的模型容量刻意设得足够大（1,048,576），避免夹具自身的模型输出上限
造成"无法超过旧限制"的假失败。
"""

import re

import pytest

from src.core.translation_result import TranslationStatus
from src.core.translator import TranslatorEngine
from src.domain.translation_policy import ContextMode, TranslationContextPolicy
from src.utils.token_estimator import estimate_tokens

BIG_CONTEXT = 1_048_576


class RecordingAPI:
    """记录真实请求内容的 API 替身。

    按收到的行号标记回放译文，因此可以验证行映射是否正确。
    """

    def __init__(self, *, finish_reason: str | None = None, chunk_size: int | None = None):
        self.requests: list[str] = []
        self.system_prompts: list[str | None] = []
        self.finish_reason = finish_reason
        self.chunk_size = chunk_size
        self.cancelled = False
        self._request_local = None  # 由 last_finish_reason 提供

    # 引擎会调用的可选接口
    def configure_serial_transport(self, serial):  # pragma: no cover - 仅记录
        pass

    def reset_cancel(self):
        self.cancelled = False

    def cancel_requests(self):
        self.cancelled = True

    def last_finish_reason(self):
        return self.finish_reason

    def _translate_marked(self, text: str) -> str:
        """把 ``[LINE_nnn]原文`` 逐行转成 ``[LINE_nnn]译:原文``。"""
        out = []
        for line in text.split("\n"):
            if line.startswith("[LINE_"):
                marker, _, content = line.partition("]")
                out.append(f"{marker}]译:{content}")
            elif line.strip():
                out.append(f"译:{line}")
            else:
                out.append(line)
        return "\n".join(out)

    def translate_stream(self, prompt, callback, system_prompt=None):
        self.requests.append(prompt)
        self.system_prompts.append(system_prompt)
        result = self._translate_marked(prompt)
        if callback:
            if self.chunk_size:
                for start in range(0, len(result), self.chunk_size):
                    callback(result[start : start + self.chunk_size])
            else:
                callback(result)
        return result

    def recommended_input_budget(self, configured):
        return configured

    def recommended_concurrency(self, configured):
        return configured


class LongContextConfigManager:
    """最小配置接口；模型容量足够大以证明超长请求能超过旧上限。"""

    def __init__(self, *, context_window_tokens: int = BIG_CONTEXT, small_model_mode: bool = False):
        self._app_config = {
            "target_language": "中文",
            "translation_prompt": "请翻译以下文本",
            "batch_lines": 20,
            "batch_max_input_tokens": 6000,
            "small_model_mode": small_model_mode,
            "queue_max_in_flight_requests": 4,
            "queue_hard_request_cap": 4,
            "queue_tpm_limit": 0,
            "long_context": {"schema_version": 1, "context_window_tokens": 131072},
        }
        self._api_config = {
            "model_name": "gpt-4",
            "context_window_tokens": context_window_tokens,
        }

    def get_app_config(self):
        return dict(self._app_config)

    def get_api_config(self):
        return dict(self._api_config)

    def get_glossary_prompt(self):
        return ""


def make_engine(api, **kwargs):
    engine = TranslatorEngine(LongContextConfigManager(**kwargs))
    engine.api = api  # 预注入 fake，绕过 _init_api
    return engine


def run_translation(engine, content, *, mode: str = "long", budget: int = 131072):
    """执行一次翻译并返回 ``(result, api, requests)``。"""
    api = engine.api
    completed = []

    if mode == "long":
        engine.translate_long_context_mode(
            content,
            lambda *_a: None,
            lambda r: completed.append(r),
            wallet=TranslationContextPolicy(ContextMode.LONG, budget),
        )
    else:
        engine.translate_fast_mode(content, lambda *_a: None, lambda r: completed.append(r))

    assert len(completed) == 1, f"expected exactly one result, got {len(completed)}"
    return completed[0], api, api.requests


#: 只匹配真实的带数字行号标记。系统提示词里提到协议的 ``[LINE_XXX]``
#: 不是行标记，`str.count("[LINE_")` 会把它算进去，导致行数虚高 1。
_LINE_MARKER_COUNT_RE = re.compile(r"\[LINE_\d+\]")


def count_source_lines(requests) -> int:
    """统计所有请求里发送的真实 ``[LINE_nnn]`` 标记数（即发送的原文行数）。"""
    return sum(len(_LINE_MARKER_COUNT_RE.findall(request)) for request in requests)


def lines_per_request(requests) -> list[int]:
    """每个请求发送的原文行数。"""
    return [len(_LINE_MARKER_COUNT_RE.findall(request)) for request in requests]


def max_request_tokens(requests) -> int:
    return max(estimate_tokens(request) for request in requests)


class TestStandardModeStillCapped:
    def test_standard_run_stays_within_old_limits(self):
        """超长模式以外的入口仍遵守 20 行 / 6,000 输入 token。"""
        api = RecordingAPI()
        engine = make_engine(api)
        content = "\n".join(f"第{i}行" + "内容" * 20 for i in range(120))

        result, api, requests = run_translation(engine, content, mode="standard")

        assert result.status == TranslationStatus.SUCCEEDED
        assert len(requests) > 1, "普通模式必须拆成多个批次"
        assert all(count <= 20 for count in lines_per_request(requests))
        assert max_request_tokens(requests) <= 6000


class TestLongContextRealRequests:
    def test_long_run_exceeds_old_line_and_token_limits(self):
        """LC-03：超长模式的真实请求超过 20 行且超过 6,000 输入 token。"""
        api = RecordingAPI()
        engine = make_engine(api)
        # 每行约 60+ token，200 行 ≈ 13,000 token，超过旧 6,000 上限与 20 行上限。
        content = "\n".join(f"第{i}行" + "内容文本" * 15 for i in range(200))

        result, api, requests = run_translation(engine, content, mode="long", budget=131072)

        assert result.status == TranslationStatus.SUCCEEDED
        assert count_source_lines(requests) == 200
        assert max(lines_per_request(requests)) > 20
        assert max_request_tokens(requests) > 6000

    def test_whole_document_in_one_request_when_budget_allows(self):
        api = RecordingAPI()
        engine = make_engine(api)
        content = "\n".join(f"第{i}行 内容" for i in range(300))

        result, api, requests = run_translation(engine, content, mode="long", budget=262144)

        assert result.status == TranslationStatus.SUCCEEDED
        assert len(requests) == 1
        assert lines_per_request(requests)[0] == 300

    def test_smaller_budget_produces_more_requests(self):
        """LC-05 运行侧：预算变小，真实请求批次数变大。"""
        content = "\n".join(f"第{i}行" + "内容文本" * 15 for i in range(200))

        api_small = RecordingAPI()
        engine_small = make_engine(api_small)
        run_translation(engine_small, content, mode="long", budget=16384)

        api_large = RecordingAPI()
        engine_large = make_engine(api_large)
        run_translation(engine_large, content, mode="long", budget=262144)

        assert len(api_small.requests) > len(api_large.requests)

    def test_model_capacity_limits_actual_batching(self):
        """模型容量小于用户预算时，实际分批按模型容量生效。"""
        api = RecordingAPI()
        engine = make_engine(api, context_window_tokens=32768)
        content = "\n".join(f"第{i}行" + "内容文本" * 15 for i in range(200))

        result, api, requests = run_translation(engine, content, mode="long", budget=1_048_576)

        assert result.status == TranslationStatus.SUCCEEDED
        # 32,768 容量下不可能把 200 行放进一个请求。
        assert len(requests) > 1
        assert max_request_tokens(requests) <= 32768

    def test_long_mode_ignores_standard_batch_lines_setting(self):
        api = RecordingAPI()
        engine = make_engine(api)
        content = "\n".join(f"第{i}行 内容" for i in range(50))

        _, _, requests = run_translation(engine, content, mode="long", budget=262144)

        assert lines_per_request(requests)[0] == 50


class TestSmallModelModeExclusivity:
    def test_programmatic_entry_returns_explicit_config_error(self):
        """小模型模式与超长模式互斥：程序入口返回明确配置错误。"""
        api = RecordingAPI()
        engine = make_engine(api, small_model_mode=True)

        with pytest.raises(Exception) as excinfo:
            engine.translate_long_context_mode(
                "hello",
                lambda *_a: None,
                lambda _r: None,
                wallet=TranslationContextPolicy(ContextMode.LONG, 131072),
            )

        message = str(excinfo.value)
        assert "小模型" in message
        assert "超长" in message
        # 没有发出任何请求，也没有静默降级为逐行。
        assert api.requests == []

    def test_small_model_mode_alone_still_translates_line_by_line(self):
        api = RecordingAPI()
        engine = make_engine(api, small_model_mode=True)
        content = "a\nb\nc"

        result, api, requests = run_translation(engine, content, mode="standard")

        assert result.status == TranslationStatus.SUCCEEDED
        assert all(count == 1 for count in lines_per_request(requests))


class TestLineMarkersAndMapping:
    def test_over_999_lines_round_trip(self):
        """LC-07：1,001 行时行号标记与绝对映射正确。"""
        api = RecordingAPI()
        engine = make_engine(api)
        content = "\n".join(f"第{i}行" for i in range(1001))

        result, api, requests = run_translation(engine, content, mode="long", budget=262144)

        assert result.status == TranslationStatus.SUCCEEDED
        assert len(result.lines) == 1001
        assert result.lines[0] == "译:第0行"
        assert result.lines[999] == "译:第999行"
        assert result.lines[1000] == "译:第1000行"

    def test_multibyte_and_emoji_are_preserved(self):
        api = RecordingAPI()
        engine = make_engine(api)
        lines = ["日本語のテキスト", "한국어 텍스트", "emoji 🎌🎋 line", "中文内容"]
        content = "\n".join(lines)

        result, _, _ = run_translation(engine, content, mode="long", budget=131072)

        assert result.status == TranslationStatus.SUCCEEDED
        for original in lines:
            assert any(original in line for line in result.lines), f"missing {original!r}"

    def test_sparse_missing_lines_map_back_to_absolute_positions(self):
        """稀疏行按绝对索引写回，不把模型标记当文档行号。"""
        api = RecordingAPI()
        engine = make_engine(api)
        # 只翻译第 1、5、9 行（绝对索引 1, 5, 9）。
        sparse_content = "\n".join(["line1", "line5", "line9"])

        result, api, requests = run_translation(engine, sparse_content, mode="long", budget=131072)

        assert result.status == TranslationStatus.SUCCEEDED
        # 批内标记是相对位置 001/002/003。
        assert "[LINE_001]" in requests[0]
        assert "[LINE_003]" in requests[0]
        assert result.lines == ["译:line1", "译:line5", "译:line9"]

    def test_markers_split_across_chunks_are_parsed(self):
        """LC-08：标记被拆到任意 chunk 边界仍能解析。"""
        api = RecordingAPI(chunk_size=3)
        engine = make_engine(api)
        content = "\n".join(f"第{i}行" for i in range(30))

        result, _, _ = run_translation(engine, content, mode="long", budget=131072)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.lines == [f"译:第{i}行" for i in range(30)]

    def test_duplicate_and_out_of_range_markers_do_not_corrupt_output(self):
        """重复、越界标记不得写错行；缺失行保持失败。"""

        class MessyAPI(RecordingAPI):
            def translate_stream(self, prompt, callback, system_prompt=None):
                self.requests.append(prompt)
                # 第 2 行重复两次，行号 99 越界，第 3 行缺失。
                result = "[LINE_001]译:A\n[LINE_002]译:B\n[LINE_002]译:B2\n[LINE_099]译:越界"
                if callback:
                    callback(result)
                return result

        api = MessyAPI()
        engine = make_engine(api)
        content = "A\nB\nC"

        result, _, _ = run_translation(engine, content, mode="long", budget=131072)

        assert result.lines[0] == "译:A"
        # 重复标记以最后一次为准，且不会写错行。
        assert result.lines[1] == "译:B2"
        assert result.lines[2] == ""  # C 缺失，不得被越界标记填充
        assert result.status == TranslationStatus.PARTIAL
        assert 2 in result.failed_indices


class TestTruncationHandling:
    def test_finish_reason_length_marks_batch_incomplete(self):
        """LC-08：``finish_reason=length`` 时不能宣告整批成功。"""

        class TruncatingAPI(RecordingAPI):
            def translate_stream(self, prompt, callback, system_prompt=None):
                self.requests.append(prompt)
                # 只返回第 1 行，模拟输出被截断。
                result = "[LINE_001]译:A"
                if callback:
                    callback(result)
                return result

        api = TruncatingAPI(finish_reason="length")
        engine = make_engine(api)

        result, _, _ = run_translation(engine, "A\nB\nC", mode="long", budget=131072)

        assert result.output_truncated is True
        assert result.finish_reason == "length"
        assert result.status is not TranslationStatus.SUCCEEDED
        assert "截断" in (result.error_message or "")
        assert 1 in result.failed_indices and 2 in result.failed_indices

    def test_finish_reason_stop_is_a_normal_success(self):
        api = RecordingAPI(finish_reason="stop")
        engine = make_engine(api)

        result, _, _ = run_translation(engine, "A\nB", mode="long", budget=131072)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.output_truncated is False

    def test_absent_finish_reason_is_treated_as_unknown_not_truncated(self):
        """能力未知时明确是估算，不编造截断信号。"""
        api = RecordingAPI(finish_reason=None)
        engine = make_engine(api)

        result, _, _ = run_translation(engine, "A\nB", mode="long", budget=131072)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.output_truncated is False

    def test_api_without_finish_reason_support_still_works(self):
        class LegacyAPI:
            def __init__(self):
                self.requests = []

            def configure_serial_transport(self, serial):  # pragma: no cover
                pass

            def reset_cancel(self):  # pragma: no cover
                pass

            def cancel_requests(self):  # pragma: no cover
                pass

            def translate_stream(self, prompt, callback, system_prompt=None):
                self.requests.append(prompt)
                result = prompt.replace("第", "译:第")
                if callback:
                    callback(result)
                return result

        api = LegacyAPI()
        engine = make_engine(api)

        result, _, _ = run_translation(engine, "A\nB", mode="long", budget=131072)

        assert result.status == TranslationStatus.SUCCEEDED
        assert result.output_truncated is False


class TestBudgetFailuresAreActionable:
    def test_single_oversized_line_reports_actionable_error(self):
        api = RecordingAPI()
        engine = make_engine(api)
        huge = "字" * 200_000

        result, api, requests = run_translation(engine, huge, mode="long", budget=8192)

        assert result.status is TranslationStatus.FAILED
        assert "第 1 行" in (result.error_message or "")
        assert "单次上下文大小" in (result.error_message or "")
        # 不得硬发越预算请求，也不得截断原文。
        assert requests == []

    def test_budget_below_safety_margin_fails_before_sending(self):
        api = RecordingAPI()
        engine = make_engine(api)

        result, api, requests = run_translation(engine, "hello", mode="long", budget=512)

        assert result.status is TranslationStatus.FAILED
        assert requests == []


class TestCacheContext:
    def test_long_and_standard_requests_are_distinguishable_by_content(self):
        """策略改变导致请求内容改变时必然区分缓存；完全相同时允许复用。"""
        content = "A\nB"

        api_standard = RecordingAPI()
        engine_standard = make_engine(api_standard)
        run_translation(engine_standard, content, mode="standard")

        api_long = RecordingAPI()
        engine_long = make_engine(api_long)
        run_translation(engine_long, content, mode="long", budget=131072)

        # 同样的两行，普通模式 20 行上限内也是一批 —— 请求内容一致即可复用缓存。
        assert api_standard.requests == api_long.requests

    def test_different_batching_changes_request_content(self):
        content = "\n".join(f"第{i}行" + "内容文本" * 15 for i in range(200))

        api_a = RecordingAPI()
        run_translation(make_engine(api_a), content, mode="long", budget=262144)

        api_b = RecordingAPI()
        run_translation(make_engine(api_b), content, mode="long", budget=32768)

        assert len(api_a.requests) != len(api_b.requests)
        assert api_a.requests != api_b.requests
