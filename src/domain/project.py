#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译项目领域模型（UXF-001 / UXF-003 / UXF-004 / UXF-005）

定义任务生命周期状态、保存语义、行编辑标记和翻译项目实体。
领域层不依赖任何项目外模块，可独立单元测试。

设计要点：
- TaskStatus 表达整个翻译任务的生命周期（pending → running → completed 等），
  与 OperationStatus（单次翻译操作的结果）是不同维度的概念。
- SaveStatus 表达项目数据的落盘状态，UI 据此展示"已保存/未保存/保存中/保存失败"。
- 行级标记（manually_edited / failed）用集合存储索引，避免逐行 dataclass 的开销。
- TranslationProject 是可变实体（翻译过程中持续更新），但关键属性通过方法保护。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from .translation import OperationStatus


class TaskStatus(str, Enum):
    """翻译任务生命周期状态（UXF-005）

    与 OperationStatus（单次操作结果）的区别：
    - OperationStatus 描述一次 translate_batch 调用的结果。
    - TaskStatus 描述整个翻译任务从创建到终态的流程。

    状态流转规则：
    - pending → running（开始翻译）
    - running → paused（用户暂停）/ partial（有失败行）/ completed / cancelled / error
    - paused → running（恢复）
    - partial → running（重试失败行）/ completed（修复全部失败行）
    - completed / cancelled / error 为终态（cancelled/error 可重新发起）
    """

    PENDING = "pending"        # 等待开始
    RUNNING = "running"        # 正在执行
    PAUSED = "paused"          # 用户暂停，可恢复
    PARTIAL = "partial"        # 有成功结果，也有失败或缺失行
    COMPLETED = "completed"    # 全部必需内容成功且质检通过
    CANCELLED = "cancelled"    # 用户取消
    ERROR = "error"            # 任务无法继续，需要用户处理

    @property
    def is_terminal(self) -> bool:
        """是否为终态（不可自动流转）"""
        return self in (self.COMPLETED, self.CANCELLED, self.ERROR)

    @property
    def is_exportable(self) -> bool:
        """是否允许导出（完成或部分完成可导出，部分完成需提示缺失）"""
        return self in (self.COMPLETED, self.PARTIAL)

    @property
    def is_running(self) -> bool:
        """是否处于运行中（running 或 paused 都算活跃）"""
        return self in (self.RUNNING, self.PAUSED)


class SaveStatus(str, Enum):
    """项目保存状态（UXF-003）

    UI 状态栏据此展示最后保存时间和保存目标。
    保存失败不得吞掉异常，应保留未保存状态并提供"重试保存"。
    """

    SAVED = "saved"              # 已保存，磁盘与内存一致
    UNSAVED = "unsaved"          # 有未保存更改
    SAVING = "saving"            # 正在保存
    SAVE_FAILED = "save_failed"  # 保存失败，需用户处理


class LineEditState(str, Enum):
    """行编辑状态标记（UXF-001）

    - MANUALLY_EDITED：用户手工编辑过的行，默认不被自动任务覆盖。
    - FAILED：翻译失败的行，保留原值并记录错误原因。
    - TRANSLATED：已成功翻译的行（含自动和手工）。
    """

    MANUALLY_EDITED = "manually_edited"
    FAILED = "failed"
    TRANSLATED = "translated"


@dataclass
class ModelSnapshot:
    """翻译时使用的模型与配置快照（UXF-004）

    记录发起翻译时的模型、目标语言、提示词版本和术语库版本，
    用于项目恢复时判断配置是否变化。
    """

    provider: str = ""
    model_name: str = ""
    target_language: str = ""
    prompt_version: str = ""
    glossary_version: str = ""
    batch_size: int = 20

    def to_dict(self) -> Dict[str, object]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "target_language": self.target_language,
            "prompt_version": self.prompt_version,
            "glossary_version": self.glossary_version,
            "batch_size": self.batch_size,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, object]] = None) -> "ModelSnapshot":
        if not data:
            return cls()
        return cls(
            provider=str(data.get("provider", "")),
            model_name=str(data.get("model_name", "")),
            target_language=str(data.get("target_language", "")),
            prompt_version=str(data.get("prompt_version", "")),
            glossary_version=str(data.get("glossary_version", "")),
            batch_size=int(data.get("batch_size", 20)),
        )


@dataclass
class TranslationProject:
    """翻译项目实体（UXF-004）

    持久化每个翻译项目的完整状态，支持应用重启后恢复长任务。
    项目状态包括原文、译文、手工编辑标记、失败行、进度和配置快照。

    该实体是可变的：翻译过程中持续更新译文和进度，然后通过仓库落盘。
    仓库负责原子写入和检查点保留，实体本身不关心持久化细节。
    """

    project_id: str
    source_path: str
    source_fingerprint: str
    file_type: str  # "txt" / "epub" / "clipboard"
    mapping_dir: str

    original_lines: List[str] = field(default_factory=list)
    translated_lines: List[str] = field(default_factory=list)

    # 行级标记：用索引集合存储，避免逐行 dataclass 开销
    manually_edited_indices: Set[int] = field(default_factory=set)
    failed_indices: Set[int] = field(default_factory=set)

    # 任务状态
    status: TaskStatus = TaskStatus.PENDING
    last_operation_status: OperationStatus = OperationStatus.SUCCEEDED
    last_error: Optional[str] = None
    retry_counts: Dict[int, int] = field(default_factory=dict)

    # 配置快照
    model_snapshot: ModelSnapshot = field(default_factory=ModelSnapshot)

    # 图片翻译状态（EPUB）
    image_translation_done: bool = False
    image_translation_failed: bool = False
    export_records: List[str] = field(default_factory=list)

    # 保存状态
    save_status: SaveStatus = SaveStatus.SAVED

    # 时间戳（ISO 字符串，仓库负责序列化）
    created_at: str = ""
    last_opened_at: str = ""
    last_saved_at: str = ""

    # ── 查询方法 ──────────────────────────────────

    @property
    def total_lines(self) -> int:
        return len(self.original_lines)

    @property
    def translated_count(self) -> int:
        """已成功翻译的行数（译文非空且非失败行）"""
        return sum(
            1
            for i, t in enumerate(self.translated_lines)
            if t and i not in self.failed_indices
        )

    @property
    def pending_count(self) -> int:
        """待翻译行数（原文非空且译文为空且非手工编辑）"""
        return len(self._compute_pending_indices())

    @property
    def failed_count(self) -> int:
        return len(self.failed_indices)

    @property
    def manually_edited_count(self) -> int:
        return len(self.manually_edited_indices)

    @property
    def completion_ratio(self) -> float:
        """完成比例 [0.0, 1.0]，基于非空原文行数计算"""
        translatable = sum(
            1 for line in self.original_lines if line and line.strip()
        )
        if translatable == 0:
            return 1.0
        return self.translated_count / translatable

    def is_line_manually_edited(self, index: int) -> bool:
        return index in self.manually_edited_indices

    def is_line_failed(self, index: int) -> bool:
        return index in self.failed_indices

    def get_pending_indices(self) -> Tuple[int, ...]:
        """返回待翻译行索引元组（UXF-002 稀疏行模型）"""
        return self._compute_pending_indices()

    def _compute_pending_indices(self) -> Tuple[int, ...]:
        """计算待翻译行索引：原文非空 + 译文为空 + 非手工编辑"""
        pending = []
        for i, (orig, trans) in enumerate(
            zip(self.original_lines, self.translated_lines)
        ):
            if orig and orig.strip() and not (trans and trans.strip()):
                if i not in self.manually_edited_indices:
                    pending.append(i)
        return tuple(pending)

    # ── 变更方法 ──────────────────────────────────

    def ensure_translated_capacity(self) -> None:
        """确保译文列表与原文等长，不足部分补空字符串"""
        expected = len(self.original_lines)
        if len(self.translated_lines) < expected:
            self.translated_lines.extend(
                [""] * (expected - len(self.translated_lines))
            )
        elif len(self.translated_lines) > expected:
            del self.translated_lines[expected:]

    def apply_translation(
        self,
        index: int,
        translated: str,
        *,
        manually_edited: bool = False,
    ) -> None:
        """写回单行译文（UXF-001：只在非空且索引有效时写入）

        - 不会覆盖手工编辑行（除非 manually_edited=True 表示这是用户手工写入）。
        - 写入成功后从失败集合移除该索引。
        - 标记项目为未保存。
        """
        if index < 0 or index >= len(self.original_lines):
            return
        # 自动任务不得覆盖手工编辑行
        if not manually_edited and index in self.manually_edited_indices:
            return
        self.ensure_translated_capacity()
        self.translated_lines[index] = translated
        self.failed_indices.discard(index)
        if manually_edited:
            self.manually_edited_indices.add(index)
        self.save_status = SaveStatus.UNSAVED

    def apply_batch_translation(
        self,
        indices: Tuple[int, ...],
        translated_lines: Tuple[str, ...],
        failed_indices: Tuple[int, ...] = (),
    ) -> None:
        """批量写回译文（UXF-002：通过稳定索引映射，跳过手工编辑行）

        Args:
            indices: 本次翻译对应的原文行索引（全局）
            translated_lines: 译文（与 indices 等长，失败行可为空）
            failed_indices: 相对于 indices 的失败偏移（0-based）
        """
        self.ensure_translated_capacity()
        failed_set = set(failed_indices)
        for offset, idx in enumerate(indices):
            if idx in self.manually_edited_indices:
                continue  # 跳过手工编辑行
            translated = (
                translated_lines[offset] if offset < len(translated_lines) else ""
            )
            if offset in failed_set or not (translated and translated.strip()):
                # 失败行保持原值，记录失败
                self.failed_indices.add(idx)
                self.retry_counts[idx] = self.retry_counts.get(idx, 0)
            else:
                self.translated_lines[idx] = translated
                self.failed_indices.discard(idx)
        self.save_status = SaveStatus.UNSAVED

    def mark_manually_edited(self, index: int, translated: str) -> None:
        """标记某行为手工编辑并写入译文"""
        self.apply_translation(index, translated, manually_edited=True)

    def mark_failed(self, index: int, error: Optional[str] = None) -> None:
        """标记某行翻译失败"""
        self.failed_indices.add(index)
        self.retry_counts[index] = self.retry_counts.get(index, 0) + 1
        if error:
            self.last_error = error
        self.save_status = SaveStatus.UNSAVED

    def clear_failed(self, index: int) -> None:
        """清除某行的失败标记（重试成功后调用）"""
        self.failed_indices.discard(index)

    def transition_status(self, new_status: TaskStatus) -> None:
        """切换任务状态（UXF-005）

        存在失败行时不允许标记为 completed。
        """
        if new_status == TaskStatus.COMPLETED and self.failed_count > 0:
            # 有失败行时降级为 partial
            self.status = TaskStatus.PARTIAL
            return
        if new_status == TaskStatus.COMPLETED and self.pending_count > 0:
            # 有未翻译行时降级为 partial
            self.status = TaskStatus.PARTIAL
            return
        self.status = new_status

    def update_from_operation(self, op_status: OperationStatus,
                              error: Optional[str] = None) -> None:
        """根据单次操作结果更新任务状态（UXF-005）"""
        self.last_operation_status = op_status
        if error:
            self.last_error = error
        if op_status == OperationStatus.SUCCEEDED:
            if self.failed_count == 0 and self.pending_count == 0:
                self.transition_status(TaskStatus.COMPLETED)
            elif self.failed_count > 0:
                self.status = TaskStatus.PARTIAL
        elif op_status == OperationStatus.PARTIAL:
            self.status = TaskStatus.PARTIAL
        elif op_status == OperationStatus.CANCELLED:
            if self.translated_count > 0:
                self.status = TaskStatus.PARTIAL
            else:
                self.status = TaskStatus.CANCELLED
        elif op_status == OperationStatus.FAILED:
            if self.translated_count > 0:
                self.status = TaskStatus.PARTIAL
            else:
                self.status = TaskStatus.ERROR

    def mark_saved(self) -> None:
        """标记为已保存"""
        self.save_status = SaveStatus.SAVED

    def mark_unsaved(self) -> None:
        self.save_status = SaveStatus.UNSAVED

    # ── 序列化 ──────────────────────────────────

    def to_dict(self) -> Dict[str, object]:
        """序列化为可 JSON 持久化的字典"""
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "source_path": self.source_path,
            "source_fingerprint": self.source_fingerprint,
            "file_type": self.file_type,
            "mapping_dir": self.mapping_dir,
            "original_lines": list(self.original_lines),
            "translated_lines": list(self.translated_lines),
            "manually_edited_indices": sorted(self.manually_edited_indices),
            "failed_indices": sorted(self.failed_indices),
            "status": self.status.value,
            "last_operation_status": self.last_operation_status.value,
            "last_error": self.last_error,
            "retry_counts": dict(self.retry_counts),
            "model_snapshot": self.model_snapshot.to_dict(),
            "image_translation_done": self.image_translation_done,
            "image_translation_failed": self.image_translation_failed,
            "export_records": list(self.export_records),
            "save_status": self.save_status.value,
            "created_at": self.created_at,
            "last_opened_at": self.last_opened_at,
            "last_saved_at": self.last_saved_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TranslationProject":
        """从字典反序列化（兼容缺失字段）"""
        status_val = str(data.get("status", "pending"))
        try:
            status = TaskStatus(status_val)
        except ValueError:
            status = TaskStatus.PENDING

        op_val = str(data.get("last_operation_status", "succeeded"))
        try:
            op_status = OperationStatus(op_val)
        except ValueError:
            op_status = OperationStatus.SUCCEEDED

        save_val = str(data.get("save_status", "saved"))
        try:
            save_status = SaveStatus(save_val)
        except ValueError:
            save_status = SaveStatus.SAVED

        last_error_raw = data.get("last_error")
        model_snap_raw = data.get("model_snapshot")
        return cls(
            project_id=str(data.get("project_id", "")),
            source_path=str(data.get("source_path", "")),
            source_fingerprint=str(data.get("source_fingerprint", "")),
            file_type=str(data.get("file_type", "txt")),
            mapping_dir=str(data.get("mapping_dir", "")),
            original_lines=list(data.get("original_lines", [])),  # type: ignore[arg-type]
            translated_lines=list(data.get("translated_lines", [])),  # type: ignore[arg-type]
            manually_edited_indices=set(data.get("manually_edited_indices", []) or []),  # type: ignore[arg-type]
            failed_indices=set(data.get("failed_indices", []) or []),  # type: ignore[arg-type]
            status=status,
            last_operation_status=op_status,
            last_error=str(last_error_raw) if last_error_raw else None,
            retry_counts=dict(data.get("retry_counts", {}) or {}),  # type: ignore[arg-type]
            model_snapshot=ModelSnapshot.from_dict(
                model_snap_raw if isinstance(model_snap_raw, dict) else None
            ),
            image_translation_done=bool(data.get("image_translation_done", False)),
            image_translation_failed=bool(data.get("image_translation_failed", False)),
            export_records=list(data.get("export_records", []) or []),  # type: ignore[arg-type]
            save_status=save_status,
            created_at=str(data.get("created_at", "")),
            last_opened_at=str(data.get("last_opened_at", "")),
            last_saved_at=str(data.get("last_saved_at", "")),
        )
