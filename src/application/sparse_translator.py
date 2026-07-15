#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
稀疏行翻译应用服务（UXF-001 / UXF-002）

统一处理"待翻译行索引列表"的翻译流程，替代旧的"从某行开始到末尾"范围模型。
续翻、查漏、选中行翻译和队列翻译共享同一套稀疏行翻译逻辑。

核心职责：
- 从 TranslationProject 计算待翻译行索引（非空原文 + 空译文 + 非手工编辑）
- 按批次大小对索引分批，调用 TranslationProvider.translate_batch
- 将结果通过稳定索引映射回项目（UXF-002：只在返回非空且索引匹配时写回）
- 跳过手工编辑行（UXF-001：自动任务不覆盖手工修改）
- 失败行保持原值，记录错误原因和尝试次数

设计要点：
- 该服务是无状态协调器，不持有项目状态，项目状态由 TranslationProject 实体管理。
- 翻译过程中通过 on_progress 回调报告进度（已完成/总数/当前批次预览）。
- 取消时抛出 TranslationCancelled，调用方负责捕获并走 CANCELLED 路径。
"""

from collections.abc import Callable, Sequence
from typing import Optional, Tuple

from ..domain.errors import TranslationCancelled
from ..domain.project import TranslationProject
from ..domain.translation import (
    OperationStatus,
    TranslationOptions,
    TranslationProgress,
    TranslationResult,
)
from ..utils.logger import get_logger
from .ports import TranslationProvider

logger = get_logger(__name__)


class SparseLineTranslator:
    """稀疏行翻译服务（UXF-002）

    将"翻译哪些行"的决策与"如何翻译"的实现解耦：
    - 决策：TranslationProject.get_pending_indices() 返回待翻译索引
    - 实现：TranslationProvider.translate_batch() 翻译给定行
    - 映射：本服务负责索引到全局位置的映射和结果写回

    用法：
        service = SparseLineTranslator(provider)
        result = service.translate_pending(project, options, on_progress)
        # result.failed_indices 已映射为全局索引
    """

    def __init__(self, provider: TranslationProvider):
        self._provider = provider

    def translate_pending(
        self,
        project: TranslationProject,
        options: TranslationOptions,
        on_progress: Optional[Callable[[TranslationProgress], None]] = None,
    ) -> TranslationResult:
        """翻译项目中的所有待翻译行（UXF-002）

        - 只提交原文非空且译文为空的行（project.get_pending_indices()）
        - 跳过手工编辑行（UXF-001）
        - 结果通过稳定索引映射写回项目
        - 失败行保持原值并记录

        Args:
            project: 翻译项目（会被原地更新译文和状态）
            options: 翻译选项
            on_progress: 进度回调

        Returns:
            TranslationResult：status / lines（全文档对齐）/ failed_indices（全局索引）
        """
        pending = project.get_pending_indices()
        if not pending:
            # 没有待翻译行，直接返回成功
            project.ensure_translated_capacity()
            return TranslationResult(
                status=OperationStatus.SUCCEEDED,
                lines=tuple(project.translated_lines),
                failed_indices=(),
            )
        return self.translate_lines(project, pending, options, on_progress)

    def translate_lines(
        self,
        project: TranslationProject,
        indices: Sequence[int],
        options: TranslationOptions,
        on_progress: Optional[Callable[[TranslationProgress], None]] = None,
    ) -> TranslationResult:
        """翻译指定的稀疏行索引（UXF-002）

        - 续翻、查漏、选中行翻译和队列翻译共用此方法
        - 通过稳定索引映射，不使用"从某行到末尾"的范围模型
        - 只在返回非空且索引匹配时写回目标行
        - 失败行保持原值，记录错误原因和尝试次数
        - 跳过手工编辑行（UXF-001）

        Args:
            project: 翻译项目（会被原地更新）
            indices: 要翻译的全局行索引列表
            options: 翻译选项
            on_progress: 进度回调

        Returns:
            TranslationResult：failed_indices 为全局索引
        """
        if not indices:
            project.ensure_translated_capacity()
            return TranslationResult(
                status=OperationStatus.SUCCEEDED,
                lines=tuple(project.translated_lines),
                failed_indices=(),
            )

        # 过滤掉越界和手工编辑行（UXF-001：自动任务不覆盖手工修改）
        valid_indices: list[int] = []
        source_lines: list[str] = []
        for idx in indices:
            if 0 <= idx < len(project.original_lines):
                if idx in project.manually_edited_indices:
                    logger.debug("跳过手工编辑行 %d", idx)
                    continue
                orig = project.original_lines[idx]
                if not (orig and orig.strip()):
                    continue  # 跳过空原文
                valid_indices.append(idx)
                source_lines.append(orig)

        if not valid_indices:
            project.ensure_translated_capacity()
            return TranslationResult(
                status=OperationStatus.SUCCEEDED,
                lines=tuple(project.translated_lines),
                failed_indices=(),
            )

        total = len(valid_indices)
        all_failed_global: list[int] = []
        last_error: Optional[str] = None
        any_success = False
        batch_size = max(1, options.batch_size)

        # 分批翻译
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)
            batch_indices = tuple(valid_indices[batch_start:batch_end])
            batch_lines = source_lines[batch_start:batch_end]

            try:
                batch_result = self._provider.translate_batch(
                    batch_lines, options,
                    self._make_progress_wrapper(
                        on_progress, batch_start, total, batch_indices,
                        project,
                    ),
                )
            except TranslationCancelled:
                # 取消：已完成的批次已写回，不覆盖已有译文
                project.update_from_operation(OperationStatus.CANCELLED)
                raise
            except Exception as exc:
                # 批次异常：记录失败行，继续后续批次
                batch_failed_global = list(batch_indices)
                all_failed_global.extend(batch_failed_global)
                last_error = str(exc)
                for gi in batch_indices:
                    project.mark_failed(gi, str(exc))
                logger.error(
                    "批次 %d-%d 翻译失败: %s",
                    batch_start, batch_end - 1, exc,
                )
                continue

            # 映射批次失败索引到全局索引
            batch_failed_global = [
                batch_indices[fi]
                for fi in batch_result.failed_indices
                if fi < len(batch_indices)
            ]

            # 写回成功行（UXF-002：只在非空且索引匹配时写回）
            project.apply_batch_translation(
                indices=batch_indices,
                translated_lines=batch_result.lines,
                failed_indices=batch_result.failed_indices,
            )

            if batch_result.status == OperationStatus.SUCCEEDED:
                any_success = True
            elif batch_result.status == OperationStatus.PARTIAL:
                any_success = True
                all_failed_global.extend(batch_failed_global)
                if batch_result.error_message:
                    last_error = batch_result.error_message
            elif batch_result.status == OperationStatus.FAILED:
                all_failed_global.extend(batch_failed_global)
                if batch_result.error_message:
                    last_error = batch_result.error_message

        # 确定最终状态
        if not all_failed_global:
            status = OperationStatus.SUCCEEDED
        elif any_success:
            status = OperationStatus.PARTIAL
        else:
            status = OperationStatus.FAILED

        project.update_from_operation(status, last_error)

        return TranslationResult(
            status=status,
            lines=tuple(project.translated_lines),
            failed_indices=tuple(all_failed_global),
            error_message=last_error,
        )

    def translate_failed(
        self,
        project: TranslationProject,
        options: TranslationOptions,
        on_progress: Optional[Callable[[TranslationProgress], None]] = None,
    ) -> TranslationResult:
        """重试失败行（UXF-009）

        只翻译标记为失败的行，不会触发成功行的重复翻译。
        """
        failed = tuple(sorted(project.failed_indices))
        if not failed:
            return TranslationResult(
                status=OperationStatus.SUCCEEDED,
                lines=tuple(project.translated_lines),
                failed_indices=(),
            )
        return self.translate_lines(project, failed, options, on_progress)

    def _make_progress_wrapper(
        self,
        on_progress: Optional[Callable[[TranslationProgress], None]],
        batch_start: int,
        total: int,
        batch_indices: Tuple[int, ...],
        project: TranslationProject,
    ) -> Optional[Callable[[TranslationProgress], None]]:
        """包装进度回调，将批次内偏移映射到全局已完成数。"""
        if on_progress is None:
            return None

        def wrapper(event: TranslationProgress) -> None:
            # 将批次内完成数映射到全局待翻译行完成数
            global_completed = batch_start + event.completed
            try:
                on_progress(TranslationProgress(
                    completed=global_completed,
                    total=total,
                    batch_start=batch_indices[0] if batch_indices else 0,
                    preview_lines=event.preview_lines,
                ))
            except Exception as e:
                logger.warning("进度回调异常: %s", e)

        return wrapper
