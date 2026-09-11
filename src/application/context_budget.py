#!/usr/bin/env python3
"""把用户预算与模型容量解析成有效上下文预算（实现指南 §5.1）。

纯函数模块：不依赖 Tk、httpx、文件读写或具体引擎。所有算术集中在此处，
调用方（引擎、队列、预检、设置预览）不得复制第二套公式。

公式（``U`` 为用户总预算，``M`` 为当前配置的模型总容量）::

    C = min(U, M)
    S = max(1024, ceil(C * 2%))
    I = 估算后的完整请求输入 token
    O = max(4096, ceil(本批待译文本（含标记）估算 token * 1.5))
    接受该批的条件：I + O + S <= C
    输入额度 = C - S；并要求 I <= max_input_tokens（若 provider 给出建议）

普通模式沿用旧的"输入预算 + 固定输出预留"语义（见
:func:`resolve_standard_budget`），因此不会引入行为回归。两个模式共用同一个
:class:`ResolvedContextBudget`，调用方必须按 ``mode`` 解释各字段。
"""

from dataclasses import dataclass

from ..config.translation_profile import DEFAULT_OUTPUT_TOKEN_RESERVE
from ..domain.translation_policy import (
    LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS,
    LONG_CONTEXT_MIN_SAFETY_MARGIN_TOKENS,
    ContextLimitReason,
    ContextMode,
    ResolvedContextBudget,
    TranslationContextPolicy,
    coerce_context_window_tokens,
    long_context_safety_margin,
)

#: 普通模式的输入预算下限，与旧实现保持一致的 ``max(512, ...)``。
STANDARD_MIN_INPUT_BUDGET_TOKENS = 512

#: 未知模型容量时的保守默认值。与配置默认值同源，避免两处漂移。
FALLBACK_MODEL_CONTEXT_TOKENS = LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS


def resolve_model_context_tokens(api_config: object) -> int:
    """从 API 配置读取用户声明的模型容量 ``M``。

    这是用户声明的值，**不是**向服务端查询验证过的能力。缺失或损坏时回退
    默认值，而不是猜一个更大的数字。
    """
    raw = api_config.get("context_window_tokens") if isinstance(api_config, dict) else None
    tokens = coerce_context_window_tokens(raw)
    if tokens is None:
        return FALLBACK_MODEL_CONTEXT_TOKENS
    return tokens


def resolve_model_max_output_tokens(api_config: object) -> int | None:
    """从 API 配置读取可选的模型输出容量上限；未声明时返回 ``None``。

    只接受用户/预设显式写入的正整数，不根据模型名称推断能力，也不编造
    内置能力表。
    """
    raw = api_config.get("max_output_tokens") if isinstance(api_config, dict) else None
    tokens = coerce_context_window_tokens(raw)
    return tokens


def resolve_standard_budget(
    *,
    configured_input_budget: int,
    model_context_tokens: int,
    output_reserve_tokens: int = DEFAULT_OUTPUT_TOKEN_RESERVE,
    max_batch_lines: int | None = None,
    provider_recommended_input_budget: int | None = None,
) -> ResolvedContextBudget:
    """解析普通模式的输入预算（保持旧行为，含边界下限）。

    旧的 ``max(512, ...)`` 语义保留在此处：普通模式历史上允许把预算收敛到
    512，超长模式**不得**复用该下限（见 :func:`resolve_long_budget`）。

    ``max_batch_lines`` 是普通模式仍然生效的行数上限（主界面 20 / 队列 80/200，
    小模型模式 1）。超长模式必须传 ``None``——那是"应用不设行数上限"的正式
    表达，不是伪无限值。
    """
    context_tokens = max(4096, int(model_context_tokens))
    reserve = max(0, int(output_reserve_tokens))
    context_safe_budget = max(
        STANDARD_MIN_INPUT_BUDGET_TOKENS,
        context_tokens - reserve - LONG_CONTEXT_MIN_SAFETY_MARGIN_TOKENS,
    )
    configured = max(STANDARD_MIN_INPUT_BUDGET_TOKENS, int(configured_input_budget))
    limit_reasons: list[ContextLimitReason] = []
    if configured > context_safe_budget:
        limit_reasons.append(ContextLimitReason.MODEL_CAPACITY)

    effective_input = min(configured, context_safe_budget)
    max_input_tokens: int | None = None
    if provider_recommended_input_budget is not None:
        recommended = int(provider_recommended_input_budget)
        if recommended > 0:
            max_input_tokens = recommended
            if recommended < effective_input:
                limit_reasons.append(ContextLimitReason.PROVIDER_PRESSURE)

    return ResolvedContextBudget(
        mode=ContextMode.STANDARD,
        requested_context_tokens=configured,
        effective_context_tokens=min(configured, context_safe_budget),
        safety_margin_tokens=max(0, context_tokens - context_safe_budget),
        batch_budget_tokens=effective_input,
        max_batch_lines=None if max_batch_lines is None else max(1, int(max_batch_lines)),
        max_input_tokens=max_input_tokens,
        limit_reasons=tuple(limit_reasons),
    )


def resolve_long_budget(
    *,
    requested_context_tokens: int,
    model_context_tokens: int,
    provider_recommended_input_budget: int | None = None,
    model_max_output_tokens: int | None = None,
) -> ResolvedContextBudget:
    """解析超长模式的有效预算。

    - ``C = min(U, M)``：模型容量仍然有效，不宣称无限上下文。
    - ``S = max(1024, ceil(C * 2%))``。
    - 输入额度 ``= C - S``；输出预留按批动态计算（见
      :func:`output_budget_for_batch`），因此这里不扣减固定 4096。
    - 模型已知输出容量小于输出预留下限时，按输出容量收紧输入额度，
      并记录 ``MODEL_OUTPUT_CAPACITY`` 原因。
    - ``provider_recommended_input_budget`` 只作为**本次规划**的输入上限，
      绝不改写用户保存值。
    """
    tokens = coerce_context_window_tokens(requested_context_tokens)
    if tokens is None:
        raise ValueError("超长上下文翻译需要正整数 token 预算")
    model_tokens = resolve_model_context_tokens({"context_window_tokens": model_context_tokens})
    effective = min(tokens, model_tokens)
    safety = long_context_safety_margin(effective)
    limit_reasons: list[ContextLimitReason] = []
    if model_tokens < tokens:
        limit_reasons.append(ContextLimitReason.MODEL_CAPACITY)

    input_budget = effective - safety
    if input_budget <= 0:
        raise ValueError(
            f"上下文预算不足：设置 {tokens} token，模拟容量 {model_tokens} token，"
            f"扣除安全余量 {safety} token 后没有可用额度"
        )

    declared_max_output = coerce_context_window_tokens(model_max_output_tokens)
    if declared_max_output is not None and declared_max_output < input_budget:
        # 输出容量未知或很小时也允许规划，但必须让输入额度服从已知输出容量。
        input_budget = max(1, min(input_budget, declared_max_output + safety))
        limit_reasons.append(ContextLimitReason.MODEL_OUTPUT_CAPACITY)

    max_input_tokens: int | None = None
    if provider_recommended_input_budget is not None:
        recommended = int(provider_recommended_input_budget)
        if recommended > 0:
            max_input_tokens = recommended
            if recommended < input_budget:
                limit_reasons.append(ContextLimitReason.PROVIDER_PRESSURE)

    return ResolvedContextBudget(
        mode=ContextMode.LONG,
        requested_context_tokens=tokens,
        effective_context_tokens=effective,
        safety_margin_tokens=safety,
        batch_budget_tokens=input_budget,
        max_batch_lines=None,
        max_input_tokens=max_input_tokens,
        model_max_output_tokens=declared_max_output,
        limit_reasons=tuple(limit_reasons),
    )


def resolve_context_budget(
    policy: TranslationContextPolicy,
    *,
    model_context_tokens: int,
    configured_input_budget: int = 0,
    max_batch_lines: int | None = None,
    provider_recommended_input_budget: int | None = None,
    model_max_output_tokens: int | None = None,
) -> ResolvedContextBudget:
    """按策略模式解析预算；普通模式与超长模式的唯一分发入口。

    ``max_batch_lines`` 只在普通模式生效（超长模式的计划里它恒为 ``None``）。
    """
    if policy.mode is ContextMode.LONG:
        requested = coerce_context_window_tokens(policy.context_window_tokens)
        if requested is None:
            raise ValueError("超长上下文翻译需要正整数 token 预算")
        return resolve_long_budget(
            requested_context_tokens=requested,
            model_context_tokens=model_context_tokens,
            provider_recommended_input_budget=provider_recommended_input_budget,
            model_max_output_tokens=model_max_output_tokens,
        )
    return resolve_standard_budget(
        configured_input_budget=configured_input_budget,
        model_context_tokens=model_context_tokens,
        max_batch_lines=max_batch_lines,
        provider_recommended_input_budget=provider_recommended_input_budget,
    )


def output_budget_for_batch(budget: ResolvedContextBudget, batch_output_basis_tokens: int) -> int:
    """返回一批允许的输出 token 额度 ``O``。

    ``batch_output_basis_tokens`` 是该批带行号标记的待译文本 token 估算。
    """
    from ..domain.translation_policy import long_context_output_reserve

    return long_context_output_reserve(max(0, int(batch_output_basis_tokens)))


@dataclass(frozen=True, slots=True)
class BudgetNarrative:
    """面向用户的一行预算说明（设置预览、预检、运行状态共用同一文案来源）。"""

    text: str
    is_limited: bool


def describe_budget(budget: ResolvedContextBudget, *, model_context_tokens: int) -> BudgetNarrative:
    """生成"设置预算 X，按当前模型配置有效预算 Y"的可解释说明。

    ``PROVIDER_PRESSURE`` 表示 provider 建议限制了本次规划的输入额度；
    其余原因都属于"模型配置"层面的收紧。
    """
    if budget.mode is not ContextMode.LONG:
        return BudgetNarrative(
            text=(
                f"普通模式：单批输入预算 {budget.batch_budget_tokens:,} token，"
                f"模型容量 {model_context_tokens:,} token"
            ),
            is_limited=bool(budget.limit_reasons),
        )

    requested = budget.requested_context_tokens
    effective = budget.effective_context_tokens
    if not budget.is_limited:
        return BudgetNarrative(
            text=(
                f"设置预算 {requested:,} token，按当前模型配置有效预算 {effective:,} token"
                f"（输入额度 {budget.batch_budget_tokens:,}）"
            ),
            is_limited=False,
        )

    parts: list[str] = []
    if ContextLimitReason.MODEL_CAPACITY in budget.limit_reasons:
        parts.append(f"模型容量 {model_context_tokens:,} token")
    if ContextLimitReason.MODEL_OUTPUT_CAPACITY in budget.limit_reasons:
        parts.append(f"模型输出容量 {budget.model_max_output_tokens:,} token")
    if ContextLimitReason.PROVIDER_PRESSURE in budget.limit_reasons:
        parts.append(f"服务商限流建议输入上限 {budget.max_input_tokens:,} token")
    detail = "、".join(parts) if parts else "当前模型配置"
    return BudgetNarrative(
        text=(
            f"设置预算 {requested:,} token，按当前模型配置有效预算 {effective:,} token"
            f"（受限于{detail}；输入额度 {budget.batch_budget_tokens:,}）"
        ),
        is_limited=True,
    )


def is_unsatisfiable_for_provider(
    *,
    estimated_input_tokens: int,
    tpm_limit: int,
) -> bool:
    """单个请求的输入估算是否超过非零 TPM 上限（永远拿不到许可）。

    ``tpm_limit <= 0`` 表示不限流，返回 ``False``。该判断用于"发送前报错并
    提示调小预算或调整限额"，避免永久等待（见实现指南 §5.3）。
    """
    if tpm_limit <= 0:
        return False
    return max(0, int(estimated_input_tokens)) > int(tpm_limit)


__all__ = [
    "FALLBACK_MODEL_CONTEXT_TOKENS",
    "STANDARD_MIN_INPUT_BUDGET_TOKENS",
    "BudgetNarrative",
    "describe_budget",
    "is_unsatisfiable_for_provider",
    "output_budget_for_batch",
    "resolve_context_budget",
    "resolve_long_budget",
    "resolve_model_context_tokens",
    "resolve_model_max_output_tokens",
    "resolve_standard_budget",
]
