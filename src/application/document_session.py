#!/usr/bin/env python3
"""P0-3/P0-6：文档会话与导入结果。

``DocumentSession`` 是不可分割的会话状态：session_id、kind、source path、
target path、mapping dir、document、dirty/save state 一次创建、一次提交。

后台导入先生成 ``ImportResult``，只有全部成功后才在 Tk 主线程替换当前 session；
失败时旧 session 完全不变。

``ImportResult`` 明确区分源读取失败、目标读取失败、目标不可写、mapping 失败。
任何必要输入失败都不提交新 session。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List

from .translation_document import TranslationDocument


class SessionKind(str, Enum):
    """会话类型。"""

    TXT = "txt"
    EPUB = "epub"
    CLIPBOARD = "clipboard"


@dataclass(slots=True)
class DocumentSession:
    """不可分割的文档会话状态。

    所有字段一次创建、一次提交。后台导入完成后在 Tk 主线程整体替换，
    失败时旧 session 完全不变，避免"新 source + 旧 target/mapping"的混合状态。
    """

    session_id: str
    kind: SessionKind
    source_path: Path | None = None
    target_path: Path | None = None
    mapping_dir: Path | None = None
    document: TranslationDocument = field(default_factory=TranslationDocument)
    dirty: bool = False

    @classmethod
    def create(
        cls,
        kind: SessionKind,
        *,
        source_path: Path | None = None,
        target_path: Path | None = None,
        mapping_dir: Path | None = None,
        source_lines: List[str] | None = None,
        target_lines: List[str] | None = None,
    ) -> DocumentSession:
        """创建新会话，同时构建文档模型。"""
        doc = TranslationDocument()
        if source_lines is not None:
            doc.replace(source_lines, target_lines or [])
        return cls(
            session_id=uuid.uuid4().hex[:12],
            kind=kind,
            source_path=source_path,
            target_path=target_path,
            mapping_dir=mapping_dir,
            document=doc,
            dirty=False,
        )

    @classmethod
    def empty(cls) -> DocumentSession:
        """创建空会话（初始状态）。"""
        return cls(session_id=uuid.uuid4().hex[:12], kind=SessionKind.TXT)

    def mark_dirty(self) -> None:
        """标记会话为脏（有未保存更改）。"""
        self.dirty = True

    def mark_clean(self) -> None:
        """标记会话为干净（已保存）。"""
        self.dirty = False


class ImportFailure(str, Enum):
    """导入失败类型。"""

    SOURCE_READ_FAILED = "source_read_failed"
    TARGET_READ_FAILED = "target_read_failed"
    TARGET_NOT_WRITABLE = "target_not_writable"
    MAPPING_FAILED = "mapping_failed"
    EPUB_PARSE_FAILED = "epub_parse_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ImportResult:
    """P0-6：结构化导入结果。

    明确区分成功和各种失败模式。失败时 ``session`` 为 None，
    调用方不得替换当前会话。

    注意：字段命名为 ``failure_kind`` 而非 ``failure``，避免与
    ``failure`` 类方法冲突。
    """

    succeeded: bool
    session: DocumentSession | None = None
    failure_kind: ImportFailure | None = None
    error_message: str = ""
    # 失败时的路径信息，用于用户提示
    failed_path: str | None = None

    @classmethod
    def success(
        cls,
        session: DocumentSession,
    ) -> ImportResult:
        """创建成功结果。"""
        return cls(succeeded=True, session=session)

    @classmethod
    def failure(
        cls,
        failure: ImportFailure,
        error_message: str,
        *,
        failed_path: str | None = None,
    ) -> ImportResult:
        """创建失败结果。"""
        return cls(
            succeeded=False,
            failure_kind=failure,
            error_message=error_message,
            failed_path=failed_path,
        )

    @classmethod
    def cancelled(cls) -> ImportResult:
        """Create a non-error result for an import cancelled by the user."""
        return cls(succeeded=False, failure_kind=ImportFailure.CANCELLED)

    @property
    def cancelled_by_user(self) -> bool:
        """Whether this result represents an expected user cancellation."""
        return self.failure_kind is ImportFailure.CANCELLED

    @property
    def user_message(self) -> str:
        """面向用户的错误消息。"""
        if self.succeeded:
            return ""
        prefix = {
            ImportFailure.SOURCE_READ_FAILED: "无法读取源文件",
            ImportFailure.TARGET_READ_FAILED: "无法读取已有译文文件",
            ImportFailure.TARGET_NOT_WRITABLE: "无法创建译文文件",
            ImportFailure.MAPPING_FAILED: "EPUB 映射保存失败",
            ImportFailure.EPUB_PARSE_FAILED: "EPUB 解析失败",
            ImportFailure.CANCELLED: "已取消导入",
        }.get(self.failure_kind, "导入失败")
        path_info = f"：{self.failed_path}" if self.failed_path else ""
        detail = f"\n\n原因：{self.error_message}" if self.error_message else ""
        return f"{prefix}{path_info}{detail}"
