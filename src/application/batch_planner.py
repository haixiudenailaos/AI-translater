#!/usr/bin/env python3
"""公共 token 分批算法（实现指南 §5.2）。

两个界面（主编辑器、批量队列）与引擎共用本模块，避免出现第二套提示词开销
算法。本模块只认识**值对象**：原文行、索引、已解析的协议信息和预算。它不引用
``core.BatchJob`` / ``core.TranslationBatchPlan``，也不导入 ``core.translator``——
需要这些类型的适配层负责包装。

关键契约：

1. 批次规划**按原文顺序贪心**加入待译行，直到下一行无法满足预算；预算足够
   且输出可容纳时可以让整份文档成为一个批次。
2. ``max_batch_lines=None`` 表示"应用不设行数上限"，不是伪无限值。
3. 行 token 只估算一次，协议固定开销每次运行只估算一次——不需要每加一行就
   重新拼接整批，接近线性时间。
4. 稀疏行通过 ``source_indices`` 写回：计划里的索引是**绝对行号**，批内标记
   是相对位置（``index_in_batch``），调用方不得把标记当文档行号。
5. 单行本身无法满足预算时**不静默放行**：抛 :class:`BatchPlanningError`，
   由调用方转成可操作的错误消息（实现指南 §5.2「至少推进一行」的旧逻辑在
   超长模式必须显式报错）。
"""

from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from ..domain.translation_policy import ContextMode, ResolvedContextBudget
from ..utils.token_estimator import estimate_tokens
from .context_budget import output_budget_for_batch

#: 行号标记模板。与引擎的 ``[LINE_XXX]`` 协议一致。
LINE_MARKER_TEMPLATE = "[LINE_{index:03d}]"


def line_marker(index_in_batch: int) -> str:
    """返回批内第 N 行（1-based）的行号标记。

    标记宽度随位数自然增长：第 1,000 行是 ``[LINE_1000]``，因此跨过
    999/9999 边界时按**实际标记长度**计费，不会低估协议开销。
    """
    return LINE_MARKER_TEMPLATE.format(index=index_in_batch)


class BatchPlanningError(ValueError):
    """无法在给定预算内规划出可发送的批次。

    调用方必须把它转成用户可操作的错误（提示调大预算或更换模型），
    不得吞掉后硬发越预算请求、截断原文或无限循环。
    """

    def __init__(self, message: str, *, source_index: int | None = None) -> None:
        super().__init__(message)
        self.source_index = source_index


@dataclass(frozen=True, slots=True)
class ProtocolOverhead:
    """一次运行中固定的协议开销。

    Attributes:
        fixed_tokens: 系统提示 + 术语表等每批都要发送的固定开销。
        protocol: 该固定文本本身，供适配层构造请求时复用（避免两套拼接）。
    """

    fixed_tokens: int
    protocol: str = ""


@dataclass(frozen=True, slots=True)
class PlannedBatch:
    """一个待发送批次。

    Attributes:
        batch_id: 按序递增的批次编号。
        source_indices: 绝对行号（元组，与 ``source_lines`` 一一对应）。
        source_lines: 该批待译原文。
        estimated_input_tokens: 该批完整请求输入估算 ``I``（含协议与标记）。
        estimated_output_basis_tokens: 该批带标记待译文本估算，用于算输出额度。
        output_budget_tokens: 允许的输出 token 额度 ``O``。
    """

    batch_id: int
    source_indices: Tuple[int, ...]
    source_lines: Tuple[str, ...]
    estimated_input_tokens: int
    estimated_output_basis_tokens: int
    output_budget_tokens: int

    @property
    def line_count(self) -> int:
        return len(self.source_lines)


def compute_protocol_overhead(protocol: str) -> ProtocolOverhead:
    """计算固定协议开销；每次运行调用一次。"""
    return ProtocolOverhead(fixed_tokens=estimate_tokens(protocol or ""), protocol=protocol or "")


def _iter_candidate_lines(
    source_lines: Sequence[str],
    indices: Iterable[int],
) -> Iterable[Tuple[int, str]]:
    """按给定顺序产出 ``(绝对索引, 原文)``，越界索引视为空行。

    返回迭代器而不是列表：超长文档可能有数十万行，不为规划额外复制一份
    原文列表。
    """
    total = len(source_lines)
    for absolute_index in indices:
        if 0 <= absolute_index < total:
            yield absolute_index, source_lines[absolute_index]
        else:
            yield absolute_index, ""


def plan_batches(
    source_lines: Sequence[str],
    source_indices: Iterable[int],
    *,
    budget: ResolvedContextBudget,
    protocol: ProtocolOverhead | None = None,
    start_batch_id: int = 0,
) -> List[PlannedBatch]:
    """按预算把待译行拆成批次。

    Args:
        source_lines: 整份文档的原文（只读）。
        source_indices: 本次要翻译的**绝对**行号，按原文顺序给出。
        budget: 已解析预算。``LONG`` 模式下 ``batch_budget_tokens`` 是
            ``I + O`` 的共同上限；``STANDARD`` 模式下是 ``I`` 的上限。
        protocol: 固定协议开销；``None`` 表示没有额外协议文本。
        start_batch_id: 首个批次编号（队列重规划时保持编号连续）。

    Raises:
        BatchPlanningError: ``LONG`` 模式下单行无法满足预算，或输出预留无法
            容纳任何一行。``STANDARD`` 模式保持历史行为——预算不足时仍发出
            单行，由 provider 判定，不在这里改变普通模式的既有语义。
    """
    overhead = protocol if protocol is not None else ProtocolOverhead(0, "")
    max_lines = budget.max_batch_lines
    long_mode = budget.mode is ContextMode.LONG

    plans: List[PlannedBatch] = []
    batch_id = start_batch_id
    current_indices: List[int] = []
    current_lines: List[str] = []
    current_input = overhead.fixed_tokens
    current_output_basis = 0

    def flush() -> None:
        nonlocal batch_id, current_indices, current_lines, current_input, current_output_basis
        if not current_indices:
            return
        output_budget = output_budget_for_batch(budget, current_output_basis)
        plans.append(
            PlannedBatch(
                batch_id=batch_id,
                source_indices=tuple(current_indices),
                source_lines=tuple(current_lines),
                estimated_input_tokens=current_input,
                estimated_output_basis_tokens=current_output_basis,
                output_budget_tokens=output_budget,
            )
        )
        batch_id += 1
        current_indices = []
        current_lines = []
        current_input = overhead.fixed_tokens
        current_output_basis = 0

    def trial(source_line: str, index_in_batch: int, base_input: int, base_basis: int):
        """Return ``(fits, input_tokens, output_basis)`` for adding one line."""
        marked_tokens = estimate_tokens(line_marker(index_in_batch) + source_line + "\n")
        next_input = base_input + marked_tokens
        next_basis = base_basis + marked_tokens
        if next_input > budget.batch_budget_tokens:
            return False, next_input, next_basis
        if budget.max_input_tokens is not None and next_input > budget.max_input_tokens:
            return False, next_input, next_basis
        if long_mode:
            # 大上下文不能只预留固定 4096 输出后塞满原文：本批输出额度必须
            # 真的容纳本批待译文本，否则长译文极易被截断。
            room = budget.batch_budget_tokens - next_input
            if output_budget_for_batch(budget, next_basis) > room:
                return False, next_input, next_basis
        return True, next_input, next_basis

    def start_new_batch(absolute_index: int, source_line: str) -> None:
        """把一个必然装不下的行放进空批，或抛出可操作的预算错误。"""
        nonlocal current_input, current_output_basis
        fits, next_input, next_basis = trial(source_line, 1, overhead.fixed_tokens, 0)
        if not fits and long_mode:
            # 超长模式：空批也放不下时，留空批次 + 无限循环或硬发越预算请求
            # 都不可接受，必须给出可定位到具体行的可操作错误。
            raise BatchPlanningError(
                _unsatisfiable_message(
                    source_index=absolute_index,
                    line_tokens=next_basis,
                    budget=budget,
                    protocol_tokens=overhead.fixed_tokens,
                ),
                source_index=absolute_index,
            )
        # 普通模式保留旧行为：至少推进一行，由 provider 判定是否超限。
        current_indices.append(absolute_index)
        current_lines.append(source_line)
        current_input = next_input
        current_output_basis = next_basis

    for absolute_index, source_line in _iter_candidate_lines(source_lines, source_indices):
        if max_lines is not None and len(current_indices) >= max_lines:
            flush()

        fits, next_input, next_basis = trial(
            source_line, len(current_indices) + 1, current_input, current_output_basis
        )
        if fits:
            current_indices.append(absolute_index)
            current_lines.append(source_line)
            current_input = next_input
            current_output_basis = next_basis
            continue

        if not current_indices:
            start_new_batch(absolute_index, source_line)
            continue

        # 本批已满/放不下：先结算，再把这一行放进一个新批。
        flush()
        start_new_batch(absolute_index, source_line)

    flush()
    return plans


def _unsatisfiable_message(
    *,
    source_index: int,
    line_tokens: int,
    budget: ResolvedContextBudget,
    protocol_tokens: int,
) -> str:
    """构造可指导处理的预算不足消息（含定位信息，不含原文）。"""
    if budget.mode is ContextMode.LONG:
        return (
            f"第 {source_index + 1} 行单独一批也无法满足上下文预算："
            f"该行约 {line_tokens:,} token，协议开销约 {protocol_tokens:,} token，"
            f"可用输入额度 {budget.batch_budget_tokens:,} token"
            f"（有效预算 {budget.effective_context_tokens:,} token）。"
            "请调大“单次上下文大小”，或更换支持更大上下文的模型。"
        )
    return (
        f"第 {source_index + 1} 行约 {line_tokens:,} token，超过本批预算 "
        f"{budget.batch_budget_tokens:,} token。请调大批次预算或拆分该行。"
    )


def estimate_batch_input_tokens(
    source_lines: Sequence[str],
    source_indices: Iterable[int],
    *,
    protocol: ProtocolOverhead | None = None,
) -> int:
    """估算把给定行放进**同一个**请求时的输入 token（用于 TPM 可行性检查）。

    复用与 :func:`plan_batches` 相同的标记与估算路径，避免第二套开销算法。
    """
    overhead = protocol if protocol is not None else ProtocolOverhead(0, "")
    total = overhead.fixed_tokens
    for position, (_, source_line) in enumerate(
        _iter_candidate_lines(source_lines, source_indices), start=1
    ):
        total += estimate_tokens(line_marker(position) + source_line + "\n")
    return total


__all__ = [
    "LINE_MARKER_TEMPLATE",
    "BatchPlanningError",
    "PlannedBatch",
    "ProtocolOverhead",
    "compute_protocol_overhead",
    "estimate_batch_input_tokens",
    "line_marker",
    "plan_batches",
]
