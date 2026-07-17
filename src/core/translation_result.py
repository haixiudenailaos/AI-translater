#!/usr/bin/env python3
"""
翻译结果语义模块（兼容层）

阶段 2 重构：领域模型已迁移至
- src/domain/translation.py（OperationStatus / TranslationOptions / TranslationProgress / TranslationResult）
- src/domain/errors.py（TranslationRequestError / TranslationCancelled / EpubFingerprintMismatchError）

本模块保留为 re-export 兼容层，避免破坏 translator.py 和现有测试的导入。
迁移完成并通知 AI-B 切换导入路径后可删除本文件（见阶段 7）。

兼容映射：
- TranslationStatus → domain.OperationStatus（别名，枚举值一致）
- TranslationRequestError → domain.errors.TranslationRequestError
- TranslationCancelled → domain.errors.TranslationCancelled
- BatchTranslationResult → 保留为 list 版本（translator.py 仍用可变 list）
                       新代码应使用 domain.TranslationResult（tuple，不可变）
"""

from dataclasses import dataclass, field
from typing import List

from ..domain.errors import TranslationCancelled, TranslationRequestError

# 领域层 re-export
from ..domain.translation import OperationStatus

# 兼容别名：旧代码使用 TranslationStatus，新代码应直接用 OperationStatus
TranslationStatus = OperationStatus


@dataclass(frozen=True)
class BatchTranslationResult:
    """批量翻译结构化结果（list 版本，兼容旧代码）

    新代码应优先使用 domain.TranslationResult（tuple，不可变）。
    translator.py 当前仍使用本类，因其内部需要按索引赋值写入译文。

    Attributes:
        status: 翻译结果状态
        lines: 译文列表（与原文行数对齐；失败行可为空字符串）
        failed_indices: 失败行索引（0-based）
        error_message: 最后一次错误消息（失败时提供）
    """

    status: OperationStatus
    lines: List[str]
    failed_indices: List[int] = field(default_factory=list)
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == OperationStatus.SUCCEEDED

    @property
    def is_cancelled(self) -> bool:
        return self.status == OperationStatus.CANCELLED

    @property
    def is_failed(self) -> bool:
        return self.status == OperationStatus.FAILED


__all__ = [
    "TranslationStatus",  # = OperationStatus 别名
    "BatchTranslationResult",  # list 版本（旧）
    "TranslationRequestError",
    "TranslationCancelled",
    # 以下为领域层类型的再导出，方便单点导入
    "OperationStatus",
]
