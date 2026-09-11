#!/usr/bin/env python3
"""LC-06 / LC-11：超长上下文的边界与不可满足场景。

覆盖实现指南 §5.2 / §5.3 的边界要求：

- 超大术语表（占满预算的协议开销）。
- 输入**恰好**到边界（不越界、不吞原文）。
- 单行过大（可操作错误，不死循环、不截断原文）。
- 已知输出容量较小（按输出容量收紧，而不是塞满原文）。
- TPM 不可满足 vs 额度暂时用尽（区分两者）。
"""

from __future__ import annotations

import pytest

from src.application.batch_planner import (
    BatchPlanningError,
    compute_protocol_overhead,
    plan_batches,
)
from src.application.context_budget import (
    is_unsatisfiable_for_provider,
    resolve_long_budget,
)
from src.domain.translation_policy import ContextMode
from src.utils.token_estimator import estimate_tokens

BIG_CONTEXT = 1_048_576


def budget(requested: int, capacity: int = BIG_CONTEXT, **kwargs):
    return resolve_long_budget(
        requested_context_tokens=requested, model_context_tokens=capacity, **kwargs
    )


class TestHugeGlossary:
    """超大术语表把协议开销推到预算上限。"""

    def test_huge_glossary_consumes_protocol_budget(self):
        """协议开销按批计费：术语表越大，同一预算下的批次数越多。"""
        glossary_prompt = "\n".join(f"- 术语{i} → Term{i}" for i in range(2000))
        protocol = compute_protocol_overhead(glossary_prompt)
        assert protocol.fixed_tokens > 10_000

        lines = [f"第{i}行 内容" for i in range(4000)]
        # 预算必须大于协议开销，否则连空批都放不下（那是另一种错误路径）。
        big_budget = budget(131_072)
        assert protocol.fixed_tokens < big_budget.batch_budget_tokens
        plans = plan_batches(
            lines,
            range(len(lines)),
            budget=big_budget,
            protocol=protocol,
        )
        without = plan_batches(lines, range(len(lines)), budget=big_budget)

        # 协议开销被真实计入：同一批原文，带术语表的输入估算恰好高出
        # 固定的协议 token 数（同一份原文 → 同一份行标记开销）。
        assert len(plans) == len(without), "该预算下两者都装得下，仅输入估算不同"
        assert (
            plans[0].estimated_input_tokens - without[0].estimated_input_tokens
            == protocol.fixed_tokens
        )
        for plan in plans:
            assert plan.estimated_input_tokens <= big_budget.batch_budget_tokens

        # 收紧预算到刚好容纳正文但不容纳协议开销时，带术语表的一侧必须先拆批。
        tight = budget(plans[0].estimated_input_tokens)
        tight_with = plan_batches(lines, range(len(lines)), budget=tight, protocol=protocol)
        tight_without = plan_batches(lines, range(len(lines)), budget=tight)
        assert len(tight_with) > len(tight_without)
        for plan in tight_with:
            assert plan.estimated_input_tokens is not None

    def test_glossary_larger_than_budget_raises_actionable_error(self):
        glossary_prompt = "x" * 500_000  # 约 125,000 token
        protocol = compute_protocol_overhead(glossary_prompt)
        with pytest.raises(BatchPlanningError) as excinfo:
            plan_batches(["short"], [0], budget=budget(8192), protocol=protocol)
        message = str(excinfo.value)
        assert "协议开销" in message
        assert "单次上下文大小" in message


class TestExactBudgetBoundary:
    """输入恰好落在边界上：不越界，也不白白浪费额度。"""

    def test_line_just_fitting_both_input_and_output_is_accepted(self):
        """恰好满足 I + O <= 输入额度的单行必须被接受。"""
        protocol = compute_protocol_overhead("")
        b = budget(16384)
        # 单行的输出额度是 max(4096, ceil(tokens*1.5))，因此可容纳的最长
        # 待译文本约等于 输入额度 / 2.5。留出标记与换行的余量。
        safe_tokens = (b.batch_budget_tokens - 100) * 2 // 5
        line = "x" * (safe_tokens * 4)
        assert estimate_tokens("[LINE_001]" + line + "\n") <= safe_tokens + 50

        plans = plan_batches([line], [0], budget=b, protocol=protocol)
        assert len(plans) == 1
        assert plans[0].source_lines == (line,)

    def test_line_over_the_boundary_raises_instead_of_silently_shrinking(self):
        b = budget(16384)
        protocol = compute_protocol_overhead("")
        # 远超额度的一行：必须报错，不能静默吞原文或截断。
        huge = "x" * ((b.batch_budget_tokens + 500) * 4)
        with pytest.raises(BatchPlanningError):
            plan_batches([huge], [0], budget=b, protocol=protocol)

    def test_no_source_line_is_lost_at_boundaries(self):
        """边界附近的批处理不得吞行或重复行。"""
        for requested in (16_384, 32_768, 131_072):
            b = budget(requested)
            lines = [f"第{i}行" + "内容" * 20 for i in range(400)]
            plans = plan_batches(lines, range(400), budget=b)
            flat = [index for plan in plans for index in plan.source_indices]
            assert flat == list(range(400)), f"budget={requested} 时行索引不连续"
            for plan in plans:
                assert plan.line_count == len(plan.source_indices)

    def test_batches_never_exceed_input_budget(self):
        for requested in (16_384, 32_768, 1_048_576):
            b = budget(requested)
            lines = [f"第{i}行" + "内容" * 30 for i in range(300)]
            for plan in plan_batches(lines, range(300), budget=b):
                assert plan.estimated_input_tokens <= b.batch_budget_tokens

    def test_batches_always_leave_room_for_output(self):
        """超长模式：I + O <= 输入额度（即 I + O + S <= C）。"""
        for requested in (32_768, 131_072, 1_048_576):
            b = budget(requested)
            lines = [f"第{i}行" + "内容文本" * 10 for i in range(300)]
            for plan in plan_batches(lines, range(300), budget=b):
                assert (
                    plan.estimated_input_tokens + plan.output_budget_tokens
                    <= b.batch_budget_tokens + 1  # +1 容忍整数向上取整
                )


class TestOversizedSingleLine:
    def test_reports_line_number_and_guidance(self):
        huge = "字" * 500_000
        with pytest.raises(BatchPlanningError) as excinfo:
            plan_batches([huge], [0], budget=budget(32768))
        message = str(excinfo.value)
        assert "第 1 行" in message
        assert "单次上下文大小" in message
        assert excinfo.value.source_index == 0

    def test_does_not_truncate_the_source_line(self):
        """不得为了塞进预算而截断原文。"""
        huge = "字" * 500_000
        with pytest.raises(BatchPlanningError):
            plan_batches([huge], [0], budget=budget(32768))
        # 原文对象本身未被修改（规划是只读的）。
        assert len(huge) == 500_000

    def test_oversized_line_among_normal_lines_is_reported(self):
        huge = "字" * 500_000
        with pytest.raises(BatchPlanningError) as excinfo:
            plan_batches(["ok", huge, "ok2"], [0, 1, 2], budget=budget(32768))
        assert excinfo.value.source_index == 1

    def test_standard_mode_still_forwards_oversized_line(self):
        """普通模式保持旧行为，不引入新失败路径。"""
        from src.domain.translation_policy import ResolvedContextBudget

        standard = ResolvedContextBudget(
            mode=ContextMode.STANDARD,
            requested_context_tokens=6000,
            effective_context_tokens=6000,
            safety_margin_tokens=0,
            batch_budget_tokens=6000,
            max_batch_lines=20,
        )
        plans = plan_batches(["字" * 500_000], [0], budget=standard)
        assert len(plans) == 1


class TestSmallDeclaredOutputCapacity:
    def test_output_capacity_tightens_input_side(self):
        b = budget(131_072, model_max_output_tokens=4096)
        assert b.model_max_output_tokens == 4096
        # 输入额度被收紧到能容纳 4096 输出：I <= 4096 + S。
        assert b.batch_budget_tokens == 4096 + b.safety_margin_tokens

    def test_small_output_capacity_shrinks_batches(self):
        lines = [f"第{i}行" + "内容" * 40 for i in range(200)]
        generous = plan_batches(lines, range(200), budget=budget(131_072))
        limited = plan_batches(
            lines, range(200), budget=budget(131_072, model_max_output_tokens=4096)
        )
        assert len(limited) > len(generous)

    def test_generous_output_capacity_does_not_widen_input(self):
        """输出容量大于输入额度时不改变输入侧（不能反向放大预算）。"""
        plain = budget(131_072)
        widened = budget(131_072, model_max_output_tokens=1_048_576)
        assert widened.batch_budget_tokens == plain.batch_budget_tokens


class TestTpmSatisfiability:
    """LC-11：区分"额度暂时用尽"与"永远不可能取得额度"。"""

    def test_request_above_tpm_is_unsatisfiable(self):
        assert is_unsatisfiable_for_provider(estimated_input_tokens=300_000, tpm_limit=200_000)

    def test_request_exactly_at_tpm_is_satisfiable(self):
        assert not is_unsatisfiable_for_provider(estimated_input_tokens=200_000, tpm_limit=200_000)

    def test_temporary_exhaustion_is_not_unsatisfiable(self):
        """额度暂时用尽：单请求仍小于限额，应等待而不是报错。"""
        assert not is_unsatisfiable_for_provider(estimated_input_tokens=150_000, tpm_limit=200_000)

    def test_zero_tpm_means_unlimited(self):
        for tokens in (1, 10**9):
            assert not is_unsatisfiable_for_provider(estimated_input_tokens=tokens, tpm_limit=0)

    def test_non_positive_tpm_is_treated_as_unlimited(self):
        assert not is_unsatisfiable_for_provider(estimated_input_tokens=10**9, tpm_limit=-1)


class TestBudgetBelowSafetyMargin:
    def test_tiny_budget_raises_instead_of_forcing_positive(self):
        with pytest.raises(ValueError, match="上下文预算不足"):
            budget(1024, capacity=1024)

    def test_budget_equal_to_margin_raises(self):
        # C = 512 时 S = 1024 > C，没有可用额度。
        with pytest.raises(ValueError):
            budget(512, capacity=512)

    def test_minimum_workable_budget_succeeds(self):
        b = budget(8192)
        assert b.batch_budget_tokens > 0


class TestModelCapacityEdges:
    def test_user_budget_below_capacity_is_used_as_is(self):
        b = budget(65_536, capacity=BIG_CONTEXT)
        assert b.effective_context_tokens == 65_536
        assert b.limit_reasons == ()

    def test_user_budget_above_capacity_is_limited(self):
        b = budget(BIG_CONTEXT, capacity=32_768)
        assert b.effective_context_tokens == 32_768
        assert b.is_limited is True

    def test_corrupt_capacity_falls_back_not_zero(self):
        from src.application.context_budget import resolve_model_context_tokens

        # 损坏值不得变成 0/负数导致预算不可用。
        for bad in (0, -1, "abc", None, True):
            assert resolve_model_context_tokens({"context_window_tokens": bad}) > 0


class TestEngineBoundaries:
    """通过真实引擎验证边界不会死循环或误报成功。"""

    def _engine(self, *, tokens: int, capacity: int = BIG_CONTEXT):
        from src.core.translator import TranslatorEngine

        class Config:
            def __init__(self):
                self._app = {
                    "target_language": "中文",
                    "translation_prompt": "p",
                    "small_model_mode": False,
                    "long_context": {"schema_version": 1, "context_window_tokens": tokens},
                }
                self._api = {"model_name": "m", "context_window_tokens": capacity}

            def get_app_config(self):
                return dict(self._app)

            def get_api_config(self, *, load_secret=True):
                return dict(self._api)

            def get_glossary_prompt(self):
                return ""

        class NoopApi:
            def __init__(self):
                self.calls = 0

            def configure_serial_transport(self, serial):
                pass

            def reset_cancel(self):
                pass

            def cancel_requests(self):
                pass

            def translate_stream(self, prompt, callback, system_prompt=None):
                self.calls += 1
                raise AssertionError("预算不足时不得发出请求")

        engine = TranslatorEngine(Config())
        engine.api = NoopApi()
        return engine

    def test_oversized_line_fails_without_sending(self):
        from src.domain.translation_policy import TranslationContextPolicy

        engine = self._engine(tokens=8192)
        completed = []
        engine.translate_long_context_mode(
            "字" * 200_000,
            lambda *_a: None,
            lambda r: completed.append(r),
            wallet=TranslationContextPolicy(ContextMode.LONG, 8192),
        )
        assert completed
        assert "第 1 行" in (completed[0].error_message or "")
        assert engine.api.calls == 0

    def test_budget_below_margin_fails_without_sending(self):
        from src.domain.translation_policy import TranslationContextPolicy

        engine = self._engine(tokens=512, capacity=512)
        completed = []
        engine.translate_long_context_mode(
            "hello",
            lambda *_a: None,
            lambda r: completed.append(r),
            wallet=TranslationContextPolicy(ContextMode.LONG, 512),
        )
        assert completed
        assert "上下文预算不足" in (completed[0].error_message or "")
        assert engine.api.calls == 0

    def test_exact_boundary_document_translates_successfully(self):
        from src.domain.translation_policy import TranslationContextPolicy

        class EchoApi:
            def __init__(self):
                self.requests: list[str] = []

            def configure_serial_transport(self, serial):
                pass

            def reset_cancel(self):
                pass

            def cancel_requests(self):
                pass

            def last_finish_reason(self):
                return None

            def translate_stream(self, prompt, callback, system_prompt=None):
                self.requests.append(prompt)
                result = "\n".join(
                    f"{line.partition(']')[0]}]译:{line.partition(']')[2]}"
                    for line in prompt.split("\n")
                    if line.startswith("[LINE_")
                )
                if callback:
                    callback(result)
                return result

        engine = self._engine(tokens=32768)
        api = EchoApi()
        engine.api = api
        lines = [f"第{i}行" + "内容" * 10 for i in range(50)]
        completed = []
        engine.translate_long_context_mode(
            "\n".join(lines),
            lambda *_a: None,
            lambda r: completed.append(r),
            wallet=TranslationContextPolicy(ContextMode.LONG, 32768),
        )
        assert completed
        assert completed[0].status.value == "succeeded"
        assert completed[0].lines[0].startswith("译:第0行")
        assert api.requests, "边界内的文档必须真的发出请求"
