#!/usr/bin/env python3
"""
翻译领域模型

定义翻译操作的状态、选项、进度和结果。领域层不依赖任何项目外模块，
可独立单元测试。

设计要点（阶段 2 重构）：
- 用结构化事件（TranslationProgress）替代旧实现中松散的字典回调，
  UI 不再猜测 'streaming' 字典中是否存在某个键。
- TranslationResult 使用 tuple 而非 list，确保不可变。
- OperationStatus 是状态枚举，所有层共享同一套状态语义。
- 旧名称 TranslationStatus / BatchTranslationResult 通过
  core/translation_result.py re-export 保留兼容，新代码应直接使用本模块。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class OperationStatus(str, Enum):
    """翻译操作结果状态

    所有层（domain / application / infrastructure / presentation）共享
    同一套状态语义，禁止用字符串字面量比较。
    """

    SUCCEEDED = "succeeded"  # 所有必需行成功
    PARTIAL = "partial"  # 部分成功，必须保留成功结果并向用户提示
    CANCELLED = "cancelled"  # 用户停止，不能显示失败或完成
    FAILED = "failed"  # 重试耗尽，不能调用完成回调


@dataclass(frozen=True)
class TranslationOptions:
    """翻译选项（不可变）

    表达一次翻译调用的配置，与具体 provider 实现无关。
    应用层构造此对象传给 TranslationProvider，不泄露 httpx 或模型细节。

    Attributes:
        target_language: 目标语言（如 "中文"）
        model_name: 模型名称（如 "gpt-4"）
        batch_size: 每批原文行数
        max_retries: 单批次最大重试次数
        temperature: 采样温度
        max_tokens: 单次请求最大 token 数
    """

    target_language: str
    model_name: str
    batch_size: int = 20
    max_retries: int = 5
    temperature: float = 0.3
    max_tokens: int = 2048


@dataclass(frozen=True)
class TranslationProgress:
    """翻译进度事件（不可变）

    替代旧实现中松散的字典回调。UI 不再猜测 'streaming' 字典中是否存在某个键。

    Attributes:
        completed: 已完成的行数（全局）
        total: 总行数
        batch_start: 当前批次起始行号（0-based，全局）
        preview_lines: 当前批次已完成的译文预览（tuple，不可变）
    """

    completed: int
    total: int
    batch_start: int
    preview_lines: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TranslationResult:
    """翻译操作结果（不可变，领域纯净版本）

    与 core/translation_result.py 中的 BatchTranslationResult 区别：
    - 使用 tuple 而非 list，确保不可变
    - 状态使用 OperationStatus
    新代码应优先使用本类；BatchTranslationResult 仅供旧 translator.py 兼容使用。

    Attributes:
        status: 翻译结果状态
        lines: 译文元组（与原文行数对齐；失败行可为空字符串）
        failed_indices: 失败行索引元组（0-based）
        error_message: 最后一次错误消息（失败时提供）
    """

    status: OperationStatus
    lines: Tuple[str, ...]
    failed_indices: Tuple[int, ...] = ()
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == OperationStatus.SUCCEEDED

    @property
    def is_cancelled(self) -> bool:
        return self.status == OperationStatus.CANCELLED

    @property
    def is_partial(self) -> bool:
        return self.status == OperationStatus.PARTIAL

    @property
    def is_failed(self) -> bool:
        return self.status == OperationStatus.FAILED

    @property
    def completed_count(self) -> int:
        """已成功完成的行数（总行数减去失败行数）"""
        return len(self.lines) - len(self.failed_indices)
