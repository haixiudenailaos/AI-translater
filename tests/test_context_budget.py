#!/usr/bin/env python3
"""LC-05 / LC-06：预算解析与公共分批算法的可观察行为。

- ``resolve_*_budget``：用户预算、模型容量、输出预留、限制原因。
- ``plan_batches``：整份文档一批、超预算单行报错、协议开销计入、
  稀疏绝对索引、跨 999/9999 标记长度、行数上限语义。
"""

import pytest

from src.application.batch_planner import (
    BatchPlanningError,
    compute_protocol_overhead,
    estimate_batch_input_tokens,
    line_marker,
    plan_batches,
)
from src.application.context_budget import (
    describe_budget,
    is_unsatisfiable_for_provider,
    resolve_context_budget,
    resolve_long_budget,
    resolve_model_context_tokens,
    resolve_model_max_output_tokens,
    resolve_standard_budget,
)
from src.domain.translation_policy import (
    LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS,
    ContextLimitReason,
    ContextMode,
    TranslationContextPolicy,
    long_context_output_reserve,
    long_context_safety_margin,
)

BIG_CONTEXT = 1_048_576


class TestSafetyMarginAndOutputReserve:
    def test_margin_is_two_percent_with_floor(self):
        assert long_context_safety_margin(1_048_576) == 20_972
        assert long_context_safety_margin(1000) == 1024  # 下限
        assert long_context_safety_margin(4096) == 1024

    def test_output_reserve_uses_half_of_input_as_floor(self):
        assert long_context_output_reserve(0) == 4096
        assert long_context_output_reserve(2000) == 4096
        assert long_context_output_reserve(10_000) == 15_000


class TestResolveLongBudget:
    def test_uses_user_budget_when_model_allows(self):
        budget = resolve_long_budget(requested_context_tokens=131_072, model_context_tokens=131_072)
        assert budget.mode is ContextMode.LONG
        assert budget.effective_context_tokens == 131_072
        assert budget.safety_margin_tokens == 2622  # ceil(131072 * 0.02)
        assert budget.batch_budget_tokens == 131_072 - 2622
        assert budget.max_batch_lines is None  # 不是伪无限值
        assert budget.limit_reasons == ()
        assert budget.is_limited is False

    def test_model_capacity_wins_but_user_value_is_kept(self):
        """LC-05：超模型预算时展示限制原因，不改保存值。"""
        budget = resolve_long_budget(
            requested_context_tokens=1_048_576, model_context_tokens=32_768
        )
        assert budget.requested_context_tokens == 1_048_576
        assert budget.effective_context_tokens == 32_768
        assert ContextLimitReason.MODEL_CAPACITY in budget.limit_reasons
        assert budget.is_limited is True

    def test_increasing_user_budget_changes_batching(self):
        """LC-05：固定模型容量下增减用户预算，实际分批随之变化。"""
        lines = [f"第{i}行 content line" for i in range(400)]
        protocol = compute_protocol_overhead("system prompt with some rules")
        small = plan_batches(
            lines,
            range(len(lines)),
            budget=resolve_long_budget(
                requested_context_tokens=8192, model_context_tokens=BIG_CONTEXT
            ),
            protocol=protocol,
        )
        large = plan_batches(
            lines,
            range(len(lines)),
            budget=resolve_long_budget(
                requested_context_tokens=262_144, model_context_tokens=BIG_CONTEXT
            ),
            protocol=protocol,
        )
        assert len(small) > len(large)
        assert sum(b.line_count for b in small) == len(lines)
        assert sum(b.line_count for b in large) == len(lines)

    def test_provider_pressure_limits_planning_without_rewriting_value(self):
        budget = resolve_long_budget(
            requested_context_tokens=131_072,
            model_context_tokens=131_072,
            provider_recommended_input_budget=6000,
        )
        assert budget.requested_context_tokens == 131_072
        assert budget.max_input_tokens == 6000
        assert ContextLimitReason.PROVIDER_PRESSURE in budget.limit_reasons
        assert budget.effective_context_tokens == 131_072

    def test_declared_model_output_capacity_constrains_input(self):
        budget = resolve_long_budget(
            requested_context_tokens=131_072,
            model_context_tokens=131_072,
            model_max_output_tokens=8192,
        )
        assert ContextLimitReason.MODEL_OUTPUT_CAPACITY in budget.limit_reasons
        assert budget.model_max_output_tokens == 8192
        assert budget.batch_budget_tokens == 8192 + budget.safety_margin_tokens

    def test_rejects_invalid_requested_budget(self):
        with pytest.raises(ValueError):
            resolve_long_budget(requested_context_tokens=0, model_context_tokens=BIG_CONTEXT)
        with pytest.raises(ValueError):
            resolve_long_budget(requested_context_tokens=None, model_context_tokens=BIG_CONTEXT)

    def test_empty_budget_raises_instead_of_being_forced_positive(self):
        """C - S <= 0 时必须报错，不能用 max(512, ...) 变成正预算。"""
        with pytest.raises(ValueError, match="上下文预算不足"):
            resolve_long_budget(requested_context_tokens=1024, model_context_tokens=1024)


class TestResolveStandardBudget:
    def test_matches_legacy_formula(self):
        budget = resolve_standard_budget(
            configured_input_budget=6000,
            model_context_tokens=32_768,
        )
        assert budget.mode is ContextMode.STANDARD
        assert budget.batch_budget_tokens == 6000  # min(6000, 32768-4096-1024)

    def test_legacy_floor_on_tiny_context_is_kept(self):
        budget = resolve_standard_budget(configured_input_budget=6000, model_context_tokens=4096)
        assert budget.batch_budget_tokens == 512

    def test_model_capacity_records_reason(self):
        budget = resolve_standard_budget(configured_input_budget=20_000, model_context_tokens=8192)
        assert budget.batch_budget_tokens == 3072  # 8192 - 4096 - 1024
        assert ContextLimitReason.MODEL_CAPACITY in budget.limit_reasons


class TestResolveContextBudgetDispatch:
    def test_standard_policy_ignores_long_fields(self):
        policy = TranslationContextPolicy()
        budget = resolve_context_budget(
            policy, model_context_tokens=32_768, configured_input_budget=6000
        )
        assert budget.mode is ContextMode.STANDARD

    def test_long_policy_uses_user_budget(self):
        policy = TranslationContextPolicy(ContextMode.LONG, 131_072)
        budget = resolve_context_budget(policy, model_context_tokens=BIG_CONTEXT)
        assert budget.mode is ContextMode.LONG
        assert budget.requested_context_tokens == 131_072


class TestModelConfigReaders:
    def test_reads_declared_capacity(self):
        assert resolve_model_context_tokens({"context_window_tokens": 200_000}) == 200_000

    def test_falls_back_without_guessing_model_name(self):
        assert resolve_model_context_tokens({}) == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS
        assert resolve_model_context_tokens(None) == LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS

    def test_output_capacity_is_optional(self):
        assert resolve_model_max_output_tokens({}) is None
        assert resolve_model_max_output_tokens({"max_output_tokens": "8192"}) == 8192
        assert resolve_model_max_output_tokens({"max_output_tokens": -1}) is None


class TestDescribeBudget:
    def test_long_budget_without_limit(self):
        budget = resolve_long_budget(requested_context_tokens=65_536, model_context_tokens=131_072)
        narrative = describe_budget(budget, model_context_tokens=131_072)
        assert "65,536" in narrative.text
        assert narrative.is_limited is False

    def test_limit_reason_is_visible(self):
        budget = resolve_long_budget(
            requested_context_tokens=1_048_576, model_context_tokens=32_768
        )
        narrative = describe_budget(budget, model_context_tokens=32_768)
        assert "1,048,576" in narrative.text
        assert "32,768" in narrative.text
        assert "有效预算" in narrative.text
        assert narrative.is_limited is True


class TestTpmFeasibility:
    def test_zero_limit_means_unlimited(self):
        assert is_unsatisfiable_for_provider(estimated_input_tokens=10**9, tpm_limit=0) is False

    def test_single_request_above_tpm_is_reported(self):
        assert is_unsatisfiable_for_provider(estimated_input_tokens=200_001, tpm_limit=200_000)

    def test_request_within_tpm_is_allowed(self):
        assert not is_unsatisfiable_for_provider(estimated_input_tokens=199_999, tpm_limit=200_000)


class TestPlanBatches:
    def test_whole_document_can_be_one_batch(self):
        """LC-03 基础：预算足够时整份文档一个批次（取消旧行数上限）。"""
        lines = [f"line {i}" for i in range(5000)]
        budget = resolve_long_budget(
            requested_context_tokens=BIG_CONTEXT, model_context_tokens=BIG_CONTEXT
        )
        plans = plan_batches(lines, range(len(lines)), budget=budget)
        assert len(plans) == 1
        assert plans[0].line_count == 5000
        assert plans[0].source_indices == tuple(range(5000))

    def test_standard_mode_still_honors_line_cap(self):
        from src.domain.translation_policy import ResolvedContextBudget

        budget = ResolvedContextBudget(
            mode=ContextMode.STANDARD,
            requested_context_tokens=6000,
            effective_context_tokens=6000,
            safety_margin_tokens=0,
            batch_budget_tokens=6000,
            max_batch_lines=20,
        )
        lines = [f"line {i}" for i in range(45)]
        plans = plan_batches(lines, range(len(lines)), budget=budget)
        assert [p.line_count for p in plans] == [20, 20, 5]

    def test_sparse_indices_are_absolute(self):
        lines = [f"line {i}" for i in range(10)]
        budget = resolve_long_budget(
            requested_context_tokens=BIG_CONTEXT, model_context_tokens=BIG_CONTEXT
        )
        plans = plan_batches(lines, [1, 5, 9], budget=budget)
        assert len(plans) == 1
        assert plans[0].source_indices == (1, 5, 9)
        assert plans[0].source_lines == ("line 1", "line 5", "line 9")

    def test_out_of_range_index_is_treated_as_empty(self):
        budget = resolve_long_budget(
            requested_context_tokens=BIG_CONTEXT, model_context_tokens=BIG_CONTEXT
        )
        plans = plan_batches(["a"], [0, 7], budget=budget)
        assert plans[0].source_indices == (0, 7)
        assert plans[0].source_lines == ("a", "")

    def test_protocol_overhead_is_charged_once_per_batch(self):
        """LC-06：预算必须包含全部协议开销，而不是只数原文。"""
        protocol = compute_protocol_overhead("x" * 40_000)  # 10,000 tokens
        assert protocol.fixed_tokens == 10_000
        budget = resolve_long_budget(
            requested_context_tokens=16_384, model_context_tokens=BIG_CONTEXT
        )
        # 1,000 行短文本（约 6,000 token）能装进一个无协议开销的批次；
        # 加上 10,000 token 的协议开销后必须拆批。
        lines = ["short line"] * 1000
        without = plan_batches(lines, range(1000), budget=budget)
        with_overhead = plan_batches(lines, range(1000), budget=budget, protocol=protocol)
        assert len(without) == 1
        assert len(with_overhead) > 1
        assert sum(plan.line_count for plan in with_overhead) == 1000
        for plan in with_overhead:
            assert plan.estimated_input_tokens >= protocol.fixed_tokens
            assert plan.estimated_input_tokens <= budget.batch_budget_tokens

    def test_line_marker_width_grows_past_999(self):
        assert line_marker(1) == "[LINE_001]"
        assert line_marker(999) == "[LINE_999]"
        assert line_marker(1000) == "[LINE_1000]"
        assert line_marker(10001) == "[LINE_10001]"
        # 标记是真实的四位/五位文本，而不是被截断成固定三位模板。
        assert line_marker(1000) != line_marker(999)
        # 批内第 1,000 行按四位标记计费：2,000 个短行的批次估算必须反映这一点。
        protocol = compute_protocol_overhead("")
        budget = resolve_long_budget(
            requested_context_tokens=BIG_CONTEXT, model_context_tokens=BIG_CONTEXT
        )
        lines = ["x"] * 2000
        plans = plan_batches(lines, range(2000), budget=budget, protocol=protocol)
        assert plans[0].estimated_input_tokens == estimate_batch_input_tokens(
            lines, range(2000), protocol=protocol
        )
        four_digit_rows = sum(1 for position in range(1, 2001) if len(line_marker(position)) == 11)
        assert four_digit_rows == 1001

    def test_single_oversized_line_raises_actionable_error(self):
        """LC-06：单行过大不能硬发、不能死循环。"""
        budget = resolve_long_budget(
            requested_context_tokens=8192, model_context_tokens=BIG_CONTEXT
        )
        huge = "字" * 100_000
        with pytest.raises(BatchPlanningError) as excinfo:
            plan_batches([huge], [0], budget=budget)
        assert "第 1 行" in str(excinfo.value)
        assert excinfo.value.source_index == 0

    def test_protocol_alone_exceeding_budget_raises(self):
        protocol = compute_protocol_overhead("x" * 400_000)
        budget = resolve_long_budget(
            requested_context_tokens=8192, model_context_tokens=BIG_CONTEXT
        )
        with pytest.raises(BatchPlanningError):
            plan_batches(["short"], [0], budget=budget, protocol=protocol)

    def test_output_reserve_prevents_filling_budget_with_source(self):
        """输出预留必须真的容纳本批待译文本，否则长译文会被截断。"""
        budget = resolve_long_budget(
            requested_context_tokens=20_000, model_context_tokens=BIG_CONTEXT
        )
        lines = ["字" * 2000] * 20
        plans = plan_batches(lines, range(len(lines)), budget=budget)
        assert len(plans) > 1
        for plan in plans:
            assert (
                plan.estimated_input_tokens + plan.output_budget_tokens
                <= budget.batch_budget_tokens
            )

    def test_standard_mode_still_admits_oversized_single_line(self):
        """普通模式保持旧行为：至少推进一行，由 provider 判定。"""
        from src.domain.translation_policy import ResolvedContextBudget

        budget = ResolvedContextBudget(
            mode=ContextMode.STANDARD,
            requested_context_tokens=6000,
            effective_context_tokens=6000,
            safety_margin_tokens=0,
            batch_budget_tokens=6000,
            max_batch_lines=20,
        )
        plans = plan_batches(["字" * 100_000], [0], budget=budget)
        assert len(plans) == 1
        assert plans[0].source_indices == (0,)

    def test_provider_input_cap_is_respected(self):
        budget = resolve_long_budget(
            requested_context_tokens=262_144,
            model_context_tokens=BIG_CONTEXT,
            provider_recommended_input_budget=6000,
        )
        lines = [f"line {i} content" for i in range(1000)]
        plans = plan_batches(lines, range(len(lines)), budget=budget)
        assert len(plans) > 1
        for plan in plans:
            assert plan.estimated_input_tokens <= 6000

    def test_planning_is_linear_in_document_size(self):
        """LC-15 基础：数据翻倍不产生明显的平方增长。"""
        import time

        budget = resolve_long_budget(
            requested_context_tokens=65_536, model_context_tokens=BIG_CONTEXT
        )
        protocol = compute_protocol_overhead("prompt")

        def measure(count: int) -> float:
            lines = [f"line {i} 内容文本" for i in range(count)]
            start = time.perf_counter()
            plans = plan_batches(lines, range(count), budget=budget, protocol=protocol)
            elapsed = time.perf_counter() - start
            assert sum(p.line_count for p in plans) == count
            return elapsed

        measure(2000)  # 预热
        small = measure(4000)
        large = measure(8000)
        # 线性算法约 2x；留足余量以容忍计时噪声与解释器抖动。
        assert large < max(small, 1e-4) * 6

    def test_empty_input_produces_no_batches(self):
        budget = resolve_long_budget(
            requested_context_tokens=4096, model_context_tokens=BIG_CONTEXT
        )
        assert plan_batches([], [], budget=budget) == []

    def test_estimate_batch_input_tokens_matches_plan(self):
        budget = resolve_long_budget(
            requested_context_tokens=BIG_CONTEXT, model_context_tokens=BIG_CONTEXT
        )
        lines = [f"line {i}" for i in range(30)]
        protocol = compute_protocol_overhead("prompt text")
        plans = plan_batches(lines, range(30), budget=budget, protocol=protocol)
        assert len(plans) == 1
        assert estimate_batch_input_tokens(lines, range(30), protocol=protocol) == (
            plans[0].estimated_input_tokens
        )
