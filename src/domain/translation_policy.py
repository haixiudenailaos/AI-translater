#!/usr/bin/env python3
"""超长上下文翻译的领域策略与预算值对象。

本模块是"超长上下文翻译"功能的纯领域契约，只依赖标准库：

- :class:`ContextMode`：一次翻译运行使用普通预算还是用户总预算。
- :class:`TranslationContextPolicy`：任务级不可变策略快照，随运行/attempt
  传递，运行中的批次绝不读取可变 UI 或共享配置。
- :class:`ResolvedContextBudget`：把用户预算、模型容量和估算策略解析成
  可解释的有效预算，供公共分批算法使用。

设计要点（对应实现指南 §4 / §5）：

1. ``LONG`` 模式取消应用固定的小批次行数/token 上限，但**不表示无限上下文**：
   模型容量、输出容量和服务限流仍然有效。``max_batch_lines`` 在 ``LONG`` 下
   必须是 ``None``（"没有应用上限"），不得使用 999999 之类的伪无限哨兵值。
2. 预算公式集中在这里定义，任何调用方都不得复制第二套算法。
   ``1.5`` / ``4096`` / ``2%`` 是可测试、集中定义的**估算策略**，
   不是模型容量事实，因此随 :data:`LONG_CONTEXT_POLICY_VERSION` 一起版本化。
3. 数值估算全部使用整数运算（``ceil(x * 150 / 100)`` 而非 ``x * 1.5``），
   避免用户输入 1,048,576 等大整数时引入浮点误差。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Tuple

# ── 估算策略版本 ──────────────────────────────────────────────
# 输出预留公式或安全余量比例变化时必须递增：缓存语义与恢复诊断都依赖它。
LONG_CONTEXT_POLICY_VERSION = 1

# 新配置的默认用户总预算。兼容当前默认模型容量（32,768），
# 是"默认配置值"而不是永久硬上限；用户可自行增大。
LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS = 32768

# 安全余量：C 的 2%，且不低于 1024 token。
LONG_CONTEXT_SAFETY_MARGIN_PERCENT = 2
LONG_CONTEXT_MIN_SAFETY_MARGIN_TOKENS = 1024

# 输出预留下限：大上下文不能只预留固定 4096 输出后塞满原文，
# 否则长译文极易被截断，因此实际预留取 max(下限, 待译文本估算 × 1.5)。
LONG_CONTEXT_MIN_OUTPUT_RESERVE_TOKENS = 4096
LONG_CONTEXT_OUTPUT_ESTIMATE_PERCENT = 150


class ContextMode(str, Enum):
    """一次翻译运行使用的预算语义。

    与 :class:`src.application.translation_modes.TranslationMode` 表达的执行
    档位（稳定/均衡/快速/自定义）正交：本枚举只回答"用多大的上下文预算"。
    """

    STANDARD = "standard"
    LONG = "long"


class ContextLimitReason(str, Enum):
    """有效预算低于用户设置值的可解释原因。

    用于"设置预算 X，按当前模型配置有效预算 Y"的预览/预检/运行状态展示。
    """

    #: 用户预算大于当前模型配置容量，按模型容量生效。
    MODEL_CAPACITY = "model_capacity"
    #: provider 反馈限流压力，本次规划缩小了输入预算（不覆盖用户保存值）。
    PROVIDER_PRESSURE = "provider_pressure"
    #: 已知模型输出容量小于估算输出预留，按输出容量收紧。
    MODEL_OUTPUT_CAPACITY = "model_output_capacity"


def _ceil_div(numerator: int, denominator: int) -> int:
    """整数向上取整除法，避免浮点运算。"""
    return -(-numerator // denominator)


def long_context_safety_margin(context_tokens: int) -> int:
    """返回总预算对应的安全余量 ``S``（整数运算）。"""
    scaled = _ceil_div(context_tokens * LONG_CONTEXT_SAFETY_MARGIN_PERCENT, 100)
    return max(LONG_CONTEXT_MIN_SAFETY_MARGIN_TOKENS, scaled)


def long_context_output_reserve(output_basis_tokens: int) -> int:
    """返回一批带标记待译文本所需的输出预留 ``O``（整数运算）。

    ``output_basis_tokens`` 是该批待译原文（含行号标记）的 token 估算。
    """
    scaled = _ceil_div(output_basis_tokens * LONG_CONTEXT_OUTPUT_ESTIMATE_PERCENT, 100)
    return max(LONG_CONTEXT_MIN_OUTPUT_RESERVE_TOKENS, scaled)


def coerce_context_window_tokens(value: object) -> int | None:
    """把不可信配置值规范化成正整数 token 预算，无法解析时返回 ``None``。

    严格拒绝空值、0、负数、小数、布尔值和字符串形式的非整数：
    不允许 ``int(1.5)`` 之类的静默截断，也不接受 ``True`` 被当作 1。

    字符串按 ASCII 数字严格解析：``int()`` 会接受全角数字
    （``int("０１２") == 12``），这属于静默误读用户输入，必须拒绝。
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or not all(char in "0123456789" for char in text):
            return None
        value = int(text)
    if value <= 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class TranslationContextPolicy:
    """任务级不可变上下文策略。

    随运行（主界面 run）或队列 attempt 传递，被所有批次复用。运行期间不读取
    可变 UI 或共享配置，因此设置改动只影响新开始的运行。

    Attributes:
        mode: ``STANDARD`` 时 ``context_window_tokens`` 必须为 ``None``，
            表示按原普通/队列策略解析；``LONG`` 时必须为正整数用户总预算
            （输入与输出合计）。
        context_window_tokens: 用户填写的总预算，单位 token。``None`` 只表示
            "STANDARD 由原策略解析"。
    """

    mode: ContextMode = ContextMode.STANDARD
    context_window_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.mode is ContextMode.LONG:
            tokens = coerce_context_window_tokens(self.context_window_tokens)
            if tokens is None:
                raise ValueError("超长上下文翻译需要正整数 token 预算")
            if tokens != self.context_window_tokens:
                # 字符串数字规范化后写回，保证运行中读取的是整数。
                object.__setattr__(self, "context_window_tokens", tokens)
            return
        if self.mode is ContextMode.STANDARD and self.context_window_tokens is not None:
            raise ValueError("普通模式不使用用户上下文预算，请勿携带该值")

    @property
    def is_long(self) -> bool:
        return self.mode is ContextMode.LONG


@dataclass(frozen=True, slots=True)
class ResolvedContextBudget:
    """解析后的有效预算与限制原因。

    两种模式的算术语义不同，由 :attr:`mode` 区分，调用方不得混用：

    - ``STANDARD``：沿用旧的输入预算语义。``batch_budget_tokens`` 是
      ``I``（提示词 + 术语 + 原文 + 行标记）的允许上限，
      ``safety_margin_tokens`` 记录模型容量之下被保留的部分（含输出预留）。
    - ``LONG``：``batch_budget_tokens = effective_context_tokens - safety_margin_tokens``
      是 ``I + O`` 的共同上限，即 "``I + O + S <= C``"。

    Attributes:
        mode: 该预算所属模式。
        requested_context_tokens: 用户设置的总预算（未被任何机制改写）。
        effective_context_tokens: 实际生效的总预算 ``C``。
        safety_margin_tokens: 安全余量 ``S``。
        batch_budget_tokens: 单批规划可用的 token 额度。
        max_batch_lines: 应用强制的批次行数上限；``LONG`` 下为 ``None``，
            表示应用不设行数上限（不是伪无限值）。
        min_output_reserve_tokens: ``LONG`` 输出预留下限。
        output_estimate_percent: ``LONG`` 输出估算百分比（150 = 1.5 倍）。
        max_input_tokens: 额外的输入上限（``I <= max_input_tokens``）。用于
            provider 动态建议（限流压力）或模型已知输出容量收紧**输入**侧，
            与 ``batch_budget_tokens`` 是两个并列约束；``None`` 表示无额外限制。
        model_max_output_tokens: 已知的模型单次输出容量上限，未知为 ``None``。
        limit_reasons: 有效预算被收紧的原因，按发生顺序去重。
        policy_version: 估算策略版本，用于缓存语义升级与诊断记录。
    """

    mode: ContextMode
    requested_context_tokens: int
    effective_context_tokens: int
    safety_margin_tokens: int
    batch_budget_tokens: int
    max_batch_lines: int | None
    min_output_reserve_tokens: int = LONG_CONTEXT_MIN_OUTPUT_RESERVE_TOKENS
    output_estimate_percent: int = LONG_CONTEXT_OUTPUT_ESTIMATE_PERCENT
    max_input_tokens: int | None = None
    model_max_output_tokens: int | None = None
    limit_reasons: Tuple[ContextLimitReason, ...] = field(default=())
    policy_version: int = LONG_CONTEXT_POLICY_VERSION

    @property
    def is_long(self) -> bool:
        return self.mode is ContextMode.LONG

    @property
    def is_limited(self) -> bool:
        """有效预算是否低于用户设置值。"""
        return self.effective_context_tokens < self.requested_context_tokens


__all__ = [
    "LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS",
    "LONG_CONTEXT_MIN_OUTPUT_RESERVE_TOKENS",
    "LONG_CONTEXT_MIN_SAFETY_MARGIN_TOKENS",
    "LONG_CONTEXT_OUTPUT_ESTIMATE_PERCENT",
    "LONG_CONTEXT_POLICY_VERSION",
    "LONG_CONTEXT_SAFETY_MARGIN_PERCENT",
    "ContextLimitReason",
    "ContextMode",
    "ResolvedContextBudget",
    "TranslationContextPolicy",
    "coerce_context_window_tokens",
    "long_context_output_reserve",
    "long_context_safety_margin",
]
