#!/usr/bin/env python3
"""P0-3/P0-6：文档会话与导入结果单元测试。

覆盖 ``PYTHON_UIUX_BEST_PRACTICES_AUDIT.md`` P0-6 验收标准：
- 锁定文件、只读目录、损坏编码、磁盘满的测试均不加载空译文
- 不修改原文件
- 保留旧 session（失败时 ``self._session`` 完全不变）

测试不依赖 Tkinter，直接调用 ``_build_txt_import_result`` /
``_build_epub_import_result`` 验证结构化 ``ImportResult``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.document_session import (
    DocumentSession,
    ImportFailure,
    ImportResult,
    SessionKind,
)
from src.application.translation_document import TranslationDocument
from src.ui.file_importer import FileImporter

# ── DocumentSession 基础行为 ─────────────────────────────


def test_document_session_create_builds_aligned_document():
    """DocumentSession.create 同时构建对齐的原文/译文文档。"""
    session = DocumentSession.create(
        SessionKind.TXT,
        source_path=Path("/tmp/a.txt"),
        target_path=Path("/tmp/a_译文.txt"),
        source_lines=["A", "B", "C"],
        target_lines=["译A", "译B"],
    )

    assert session.kind is SessionKind.TXT
    assert session.source_path == Path("/tmp/a.txt")
    assert session.target_path == Path("/tmp/a_译文.txt")
    assert session.mapping_dir is None
    assert session.document.row_count == 3
    assert session.document.source_lines() == ["A", "B", "C"]
    # 不足的译文补空字符串
    assert session.document.target_lines() == ["译A", "译B", ""]
    assert session.dirty is False
    # session_id 非空且唯一
    assert session.session_id


def test_document_session_empty_has_txt_kind_and_no_paths():
    """DocumentSession.empty 初始为 TXT 会话且无路径。"""
    session = DocumentSession.empty()

    assert session.kind is SessionKind.TXT
    assert session.source_path is None
    assert session.target_path is None
    assert session.mapping_dir is None
    assert session.document.row_count == 0


def test_document_session_dirty_transitions():
    """mark_dirty / mark_clean 切换 dirty 状态。"""
    session = DocumentSession.empty()
    assert session.dirty is False

    session.mark_dirty()
    assert session.dirty is True

    session.mark_clean()
    assert session.dirty is False


def test_document_session_each_create_has_unique_session_id():
    """每次 create 生成不同的 session_id。"""
    s1 = DocumentSession.create(SessionKind.TXT, source_lines=["A"])
    s2 = DocumentSession.create(SessionKind.TXT, source_lines=["A"])

    assert s1.session_id != s2.session_id


# ── ImportResult 工厂与 user_message ────────────────────


def test_import_result_success_carries_session():
    """ImportResult.success 携带新 session。"""
    session = DocumentSession.create(SessionKind.TXT, source_lines=["A"])
    result = ImportResult.success(session)

    assert result.succeeded is True
    assert result.session is session
    assert result.failure_kind is None
    assert result.error_message == ""
    assert result.user_message == ""


def test_import_result_failure_does_not_carry_session():
    """ImportResult.failure 不携带 session。"""
    result = ImportResult.failure(
        ImportFailure.SOURCE_READ_FAILED,
        "Permission denied",
        failed_path="/tmp/locked.txt",
    )

    assert result.succeeded is False
    assert result.session is None
    assert result.failure_kind is ImportFailure.SOURCE_READ_FAILED
    assert result.error_message == "Permission denied"
    assert result.failed_path == "/tmp/locked.txt"


def test_import_result_cancelled_is_not_an_error_result():
    result = ImportResult.cancelled()

    assert result.succeeded is False
    assert result.cancelled_by_user is True
    assert result.failure_kind is ImportFailure.CANCELLED
    assert result.user_message == "已取消导入"


def test_import_result_user_message_includes_path_and_reason():
    """user_message 包含路径和原因。"""
    result = ImportResult.failure(
        ImportFailure.TARGET_READ_FAILED,
        "编码错误",
        failed_path="/tmp/x_译文.txt",
    )

    msg = result.user_message
    assert "无法读取已有译文文件" in msg
    assert "/tmp/x_译文.txt" in msg
    assert "编码错误" in msg


def test_import_result_user_message_distinct_per_failure_type():
    """每种失败类型有不同前缀。"""
    cases = [
        (ImportFailure.SOURCE_READ_FAILED, "无法读取源文件"),
        (ImportFailure.TARGET_READ_FAILED, "无法读取已有译文文件"),
        (ImportFailure.TARGET_NOT_WRITABLE, "无法创建译文文件"),
        (ImportFailure.MAPPING_FAILED, "EPUB 映射保存失败"),
        (ImportFailure.EPUB_PARSE_FAILED, "EPUB 解析失败"),
    ]
    for failure_type, expected_prefix in cases:
        result = ImportResult.failure(failure_type, "x")
        assert result.user_message.startswith(expected_prefix), (
            f"{failure_type} 应以前缀 '{expected_prefix}' 开头，实际：{result.user_message}"
        )


def test_import_result_user_message_handles_missing_path():
    """无 failed_path 时 user_message 不包含路径信息。

    格式为 ``<prefix>\n\n原因：<reason>``，路径段（prefix 后紧跟的
    ``：{path}``）被省略。
    """
    result = ImportResult.failure(
        ImportFailure.EPUB_PARSE_FAILED,
        "未知错误",
    )

    msg = result.user_message
    assert msg.startswith("EPUB 解析失败")
    # prefix 后不应紧跟路径分隔符 "："
    assert not msg.startswith("EPUB 解析失败：")
    assert "未知错误" in msg
    assert "原因：未知错误" in msg


# ── FileImporter 构造辅助 ───────────────────────────────


class _FakeFileHandler:
    """可编程的 file_handler 替身，按路径返回内容或抛异常。"""

    def __init__(
        self,
        read_results: dict | None = None,
        read_errors: dict | None = None,
        write_errors: set | None = None,
    ) -> None:
        self.read_results = read_results or {}
        self.read_errors = read_errors or {}
        self.write_errors = write_errors or set()
        self.written: list[tuple[str, str]] = []

    def read_file(self, path: str) -> str:
        if path in self.read_errors:
            raise self.read_errors[path]
        if path in self.read_results:
            return self.read_results[path]
        raise FileNotFoundError(path)

    def write_file(self, path: str, content: str) -> None:
        if path in self.write_errors:
            raise self.write_errors[path]
        self.written.append((path, content))


class _FakeEpubProcessor:
    """可编程的 epub_processor 替身。"""

    def __init__(
        self,
        mapping_info: dict | None = None,
        content_mapping: tuple[list[str], list[str]] | None = None,
        import_error: Exception | None = None,
        load_error: Exception | None = None,
    ) -> None:
        self._mapping_info = mapping_info
        self._content_mapping = content_mapping
        self._import_error = import_error
        self._load_error = load_error

    def import_epub(self, path: str, **_kwargs) -> dict:
        if self._import_error:
            raise self._import_error
        return self._mapping_info or {"mapping_dir": ""}

    def load_content_mapping(self, mapping_dir: str):
        if self._load_error:
            raise self._load_error
        return self._content_mapping or ([], [])


def _make_file_importer(
    file_handler: _FakeFileHandler,
    epub_processor: _FakeEpubProcessor,
    loaded: list | None = None,
) -> FileImporter:
    """构造不依赖 Tk 的 FileImporter。

    ``root`` 用 SimpleNamespace 替身，因为 ``_build_*_import_result``
    路径不接触 Tk。
    """
    loaded = loaded if loaded is not None else []

    def table_loader(source_lines, target_lines):
        loaded.append((list(source_lines), list(target_lines)))

    return FileImporter(
        root=SimpleNamespace(),
        config_manager=None,
        file_handler=file_handler,
        epub_processor=epub_processor,
        table_loader=table_loader,
        status_updater=lambda _msg: None,
        image_translation_starter=lambda: None,
    )


# ── P0-6：文本导入错误路径 ──────────────────────────────


def test_txt_import_source_read_failure_returns_structured_result(tmp_path):
    """P0-6：源文件读取失败返回 SOURCE_READ_FAILED，不提交 session。"""
    src_path = tmp_path / "novel.txt"
    src_path.write_text("dummy", encoding="utf-8")

    file_handler = _FakeFileHandler(read_errors={str(src_path): PermissionError("文件被锁定")})
    importer = _make_file_importer(file_handler, _FakeEpubProcessor())
    old_session = importer.session
    loaded: list = []

    result = importer._build_txt_import_result(src_path)

    assert result.succeeded is False
    assert result.failure_kind is ImportFailure.SOURCE_READ_FAILED
    assert result.failed_path == str(src_path)
    assert "文件被锁定" in result.error_message
    # 失败不应触发 table_loader（不加载空译文）
    assert loaded == []
    # 旧 session 完全不变
    assert importer.session is old_session
    assert importer.current_source_path is old_session.source_path


def test_txt_import_target_read_failure_does_not_load_empty(tmp_path):
    """P0-6：译文文件读取失败不加载空译文，保留旧 session。"""
    src_path = tmp_path / "novel.txt"
    src_path.write_text("原文A\n原文B", encoding="utf-8")
    tgt_path = tmp_path / "novel_译文.txt"
    tgt_path.write_text("旧译文", encoding="utf-8")

    file_handler = _FakeFileHandler(
        read_results={str(src_path): "原文A\n原文B"},
        read_errors={str(tgt_path): UnicodeDecodeError("utf-8", b"\xff", 0, 1, "非法字节")},
    )
    importer = _make_file_importer(file_handler, _FakeEpubProcessor())
    old_session = importer.session

    result = importer._build_txt_import_result(src_path)

    assert result.succeeded is False
    assert result.failure_kind is ImportFailure.TARGET_READ_FAILED
    assert result.failed_path == str(tgt_path)
    # user_message 提示用户检查权限/编码
    assert "请检查文件权限或编码" in result.user_message
    # 旧 session 不变
    assert importer.session is old_session


def test_txt_import_target_not_writable_reports_failure(tmp_path):
    """P0-6：目标文件不可写返回 TARGET_NOT_WRITABLE。"""
    src_path = tmp_path / "novel.txt"
    src_path.write_text("原文", encoding="utf-8")
    tgt_path = tmp_path / "novel_译文.txt"
    # 注意：tgt_path 不存在，会触发 write_file 创建

    file_handler = _FakeFileHandler(
        read_results={str(src_path): "原文"},
        write_errors={str(tgt_path): PermissionError("只读目录")},
    )
    importer = _make_file_importer(file_handler, _FakeEpubProcessor())

    result = importer._build_txt_import_result(src_path)

    assert result.succeeded is False
    assert result.failure_kind is ImportFailure.TARGET_NOT_WRITABLE
    assert result.failed_path == str(tgt_path)
    assert "只读目录" in result.error_message
    assert "请检查目录权限或磁盘空间" in result.user_message


def test_txt_import_success_returns_session_with_aligned_lines(tmp_path):
    """P0-6：成功导入返回带新 session 的 ImportResult，原文/译文对齐。"""
    src_path = tmp_path / "novel.txt"
    src_path.write_text("原文A\n原文B\n原文C", encoding="utf-8")
    tgt_path = tmp_path / "novel_译文.txt"
    tgt_path.write_text("译A\n译B", encoding="utf-8")

    file_handler = _FakeFileHandler(
        read_results={
            str(src_path): "原文A\n原文B\n原文C",
            str(tgt_path): "译A\n译B",
        },
    )
    importer = _make_file_importer(file_handler, _FakeEpubProcessor())

    result = importer._build_txt_import_result(src_path)

    assert result.succeeded is True
    assert result.session is not None
    assert result.session.kind is SessionKind.TXT
    assert result.session.source_path == src_path
    assert result.session.target_path == tgt_path
    assert result.session.mapping_dir is None
    # 对齐：译文不足补空字符串
    assert result.session.document.source_lines() == ["原文A", "原文B", "原文C"]
    assert result.session.document.target_lines() == ["译A", "译B", ""]


def test_txt_import_success_creates_empty_target_file(tmp_path):
    """P0-6：成功导入时若译文文件不存在则创建空文件。"""
    src_path = tmp_path / "novel.txt"
    src_path.write_text("原文", encoding="utf-8")
    tgt_path = tmp_path / "novel_译文.txt"
    assert not tgt_path.exists()

    file_handler = _FakeFileHandler(
        read_results={str(src_path): "原文"},
    )
    importer = _make_file_importer(file_handler, _FakeEpubProcessor())

    result = importer._build_txt_import_result(src_path)

    assert result.succeeded is True
    # write_file 被调用创建空文件
    assert (str(tgt_path), "") in file_handler.written


def test_txt_import_starts_background_worker_and_commits_only_on_ui_callback(tmp_path, monkeypatch):
    src_path = tmp_path / "novel.txt"
    src_path.write_text("原文", encoding="utf-8")
    file_handler = _FakeFileHandler(read_results={str(src_path): "原文"})
    importer = _make_file_importer(file_handler, _FakeEpubProcessor())
    importer._disable_ui_controls = MagicMock()
    importer._enable_ui_controls = MagicMock()
    old_session = importer.session

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr("src.ui.file_importer.threading.Thread", ImmediateThread)

    importer._import_txt_in_background(src_path)

    assert importer.session is old_session
    callbacks = importer._ui_mailbox.drain()
    assert len(callbacks) == 1
    callbacks[0]()

    assert importer.session.kind is SessionKind.TXT
    assert importer.current_source_path == src_path
    assert importer.current_target_path == tmp_path / "novel_译文.txt"
    assert importer._txt_import_busy is False
    importer._disable_ui_controls.assert_called_once()
    importer._enable_ui_controls.assert_called_once()


def test_stale_txt_import_callback_cannot_replace_newer_session(tmp_path):
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    old_session = importer.session
    result = ImportResult.success(
        DocumentSession.create(
            SessionKind.TXT,
            source_path=tmp_path / "old.txt",
            target_path=tmp_path / "old_译文.txt",
            source_lines=["old"],
        )
    )
    importer._txt_import_generation = 2
    importer._txt_import_busy = True

    importer._finish_txt_import(1, tmp_path / "old.txt", result)

    assert importer.session is old_session
    assert importer._txt_import_busy is True


# ── P0-6：EPUB 导入错误路径 ──────────────────────────────


def test_epub_import_parse_failure_returns_structured_result(tmp_path):
    """P0-6：EPUB 解析失败返回 EPUB_PARSE_FAILED，旧 session 不变。"""
    src_path = tmp_path / "book.epub"
    src_path.write_bytes(b"not an epub")

    epub_processor = _FakeEpubProcessor(import_error=RuntimeError("EPUB 格式损坏"))
    importer = _make_file_importer(_FakeFileHandler(), epub_processor)
    old_session = importer.session

    result = importer._build_epub_import_result(src_path)

    assert result.succeeded is False
    assert result.failure_kind is ImportFailure.EPUB_PARSE_FAILED
    assert result.failed_path == str(src_path)
    assert "EPUB 格式损坏" in result.error_message
    assert importer.session is old_session


def test_epub_import_cancellation_returns_distinct_result(tmp_path):
    """取消不是解析失败，且不得替换现有会话。"""
    src_path = tmp_path / "book.epub"
    src_path.write_bytes(b"placeholder")

    class CancelledProcessor(_FakeEpubProcessor):
        def import_epub(self, path: str, **_kwargs) -> dict:
            from src.core.epub_processor import EpubImportCancelled

            raise EpubImportCancelled("cancelled")

    importer = _make_file_importer(_FakeFileHandler(), CancelledProcessor())
    old_session = importer.session

    result = importer._build_epub_import_result(src_path)

    assert result.cancelled_by_user is True
    assert result.failure_kind is ImportFailure.CANCELLED
    assert importer.session is old_session


def test_epub_import_success_returns_epub_session(tmp_path, make_epub):
    """P0-6：EPUB 成功导入返回带 EPUB kind 的 session。

    使用 conftest 的 make_epub fixture 构造真实可解析的 EPUB，
    配合真实的 EPUBProcessor 验证 mapping_dir 路径正常落地。
    """
    epub_path = make_epub(
        chapters=[("第一章", ["これはテストです。"])],
    )
    # 使用真实 EPUBProcessor（集成测试，验证集成路径正常）
    from src.core.epub_processor import EPUBProcessor

    epub_processor = EPUBProcessor()
    importer = _make_file_importer(_FakeFileHandler(), epub_processor)

    result = importer._build_epub_import_result(epub_path)

    assert result.succeeded is True, f"EPUB 导入应成功：{result.user_message}"
    assert result.session.kind is SessionKind.EPUB
    assert result.session.source_path == epub_path
    assert result.session.mapping_dir is not None
    assert result.session.mapping_dir.exists()
    # 译文文件应被创建
    assert result.session.target_path.exists()
    # 原文已加载
    assert result.session.document.row_count >= 1
    assert "これはテストです。" in result.session.document.source_lines()


# ── P0-3：原子 session 替换 ──────────────────────────────


def test_commit_session_replaces_old_session_atomically(tmp_path):
    """P0-3：_commit_session 原子替换 session 并触发 table_loader。"""
    file_handler = _FakeFileHandler()
    loaded: list = []
    importer = _make_file_importer(file_handler, _FakeEpubProcessor(), loaded=loaded)
    old_session = importer.session

    new_session = DocumentSession.create(
        SessionKind.TXT,
        source_path=tmp_path / "a.txt",
        source_lines=["X", "Y"],
        target_lines=["译X"],
    )

    importer._commit_session(new_session)

    assert importer.session is new_session
    assert importer.session is not old_session
    # table_loader 被调用一次，传入新 session 的数据
    assert len(loaded) == 1
    assert loaded[0] == (["X", "Y"], ["译X", ""])


def test_commit_session_reuses_shared_document_model(tmp_path):
    """生产组合根传入的文档模型应成为 session 的唯一实例。"""
    shared = TranslationDocument()
    importer = FileImporter(
        root=SimpleNamespace(),
        config_manager=None,
        file_handler=_FakeFileHandler(),
        epub_processor=_FakeEpubProcessor(),
        table_loader=lambda _source, _target: None,
        status_updater=lambda _message: None,
        image_translation_starter=lambda: None,
        document=shared,
    )
    session = DocumentSession.create(
        SessionKind.CLIPBOARD,
        source_lines=["source"],
        target_lines=["target"],
    )

    importer._commit_session(session)

    assert importer.session.document is shared
    assert shared.source_lines() == ["source"]
    assert shared.target_lines() == ["target"]


def test_commit_session_clears_mapping_keys(tmp_path):
    """P0-3：_commit_session 清空 current_mapping_keys 缓存。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    importer.current_mapping_keys = ["old_key_1", "old_key_2"]

    new_session = DocumentSession.create(SessionKind.TXT, source_lines=["A"])
    importer._commit_session(new_session)

    assert importer.current_mapping_keys == []


def test_failed_import_preserves_old_session_paths(tmp_path):
    """P0-6 验收：失败导入保留旧 session 的所有路径状态。"""
    # 先成功导入一个文件建立旧 session
    src1 = tmp_path / "novel1.txt"
    src1.write_text("原文1", encoding="utf-8")
    tgt1 = tmp_path / "novel1_译文.txt"
    tgt1.write_text("译1", encoding="utf-8")

    file_handler = _FakeFileHandler(
        read_results={
            str(src1): "原文1",
            str(tgt1): "译1",
        },
    )
    importer = _make_file_importer(file_handler, _FakeEpubProcessor())
    result1 = importer._build_txt_import_result(src1)
    importer._commit_session(result1.session)

    old_source = importer.current_source_path
    old_target = importer.current_target_path
    old_mapping = importer.current_mapping_dir

    # 尝试导入另一个文件但源文件读取失败
    src2 = tmp_path / "novel2.txt"
    src2.write_text("原文2", encoding="utf-8")
    file_handler.read_errors = {str(src2): PermissionError("锁定")}

    result2 = importer._build_txt_import_result(src2)

    assert result2.succeeded is False
    # 旧 session 路径完全保留
    assert importer.current_source_path == old_source
    assert importer.current_target_path == old_target
    assert importer.current_mapping_dir == old_mapping


# ── 向后兼容属性 ────────────────────────────────────────


def test_backward_compat_properties_delegate_to_session(tmp_path):
    """向后兼容属性 current_source_path/current_target_path/current_mapping_dir
    委派到 session 字段。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())

    # 初始空 session
    assert importer.current_source_path is None
    assert importer.current_target_path is None
    assert importer.current_mapping_dir is None

    # 提交新 session 后属性自动更新
    new_session = DocumentSession.create(
        SessionKind.TXT,
        source_path=tmp_path / "a.txt",
        target_path=tmp_path / "a_译文.txt",
        source_lines=["A"],
    )
    importer._commit_session(new_session)

    assert importer.current_source_path == tmp_path / "a.txt"
    assert importer.current_target_path == tmp_path / "a_译文.txt"
    assert importer.current_mapping_dir is None

    # EPUB session 有 mapping_dir
    epub_session = DocumentSession.create(
        SessionKind.EPUB,
        mapping_dir=tmp_path / "mapping",
        source_lines=["B"],
    )
    importer._commit_session(epub_session)
    assert importer.current_mapping_dir == tmp_path / "mapping"


def test_backward_compat_properties_are_read_only():
    """向后兼容属性是只读的，写入会抛 AttributeError。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())

    with pytest.raises(AttributeError):
        importer.current_source_path = Path("/tmp/x")  # type: ignore[misc]

    with pytest.raises(AttributeError):
        importer.current_target_path = Path("/tmp/y")  # type: ignore[misc]

    with pytest.raises(AttributeError):
        importer.current_mapping_dir = Path("/tmp/z")  # type: ignore[misc]


# ── P0-3：update_session_target_path / _guard_replace_session ──


def test_update_session_target_path_replaces_only_target(tmp_path):
    """P0-3：update_session_target_path 保留 session 其他字段，仅替换 target_path。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    src = tmp_path / "src.txt"
    src.write_text("A", encoding="utf-8")
    importer._session = DocumentSession.create(
        kind=SessionKind.TXT,
        source_path=src,
        target_path=tmp_path / "old.txt",
        mapping_dir=None,
        source_lines=["A"],
        target_lines=["旧译"],
    )
    original_session = importer.session
    new_target = tmp_path / "new.txt"

    importer.update_session_target_path(new_target)

    new_session = importer.session
    assert new_session is not original_session
    assert new_session.target_path == new_target
    # 其他字段保持不变
    assert new_session.session_id == original_session.session_id
    assert new_session.kind is SessionKind.TXT
    assert new_session.source_path == original_session.source_path
    assert new_session.mapping_dir == original_session.mapping_dir


def test_update_session_target_path_noop_when_unchanged(tmp_path):
    """P0-3：target_path 未变化时不替换 session。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    src = tmp_path / "src.txt"
    src.write_text("A", encoding="utf-8")
    target = tmp_path / "target.txt"
    importer._session = DocumentSession.create(
        kind=SessionKind.TXT,
        source_path=src,
        target_path=target,
        mapping_dir=None,
        source_lines=["A"],
        target_lines=["译"],
    )
    original_session = importer.session

    importer.update_session_target_path(target)

    assert importer.session is original_session


def test_guard_replace_session_passes_when_not_dirty():
    """P0-3：无未保存更改时直接放行。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    importer.is_dirty_callback = lambda: False
    guard_calls: list = []
    importer._confirm_replace_session = lambda: guard_calls.append("called") or "proceed"

    assert importer._guard_replace_session() is True
    # dirty=False 时不调用守卫
    assert guard_calls == []


def test_guard_replace_session_discard_allows_replace():
    """P0-3：dirty + 用户选择 discard → 放行替换。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    importer.is_dirty_callback = lambda: True
    importer._confirm_replace_session = lambda: "discard"

    assert importer._guard_replace_session() is True


def test_guard_replace_session_cancel_blocks_replace():
    """P0-3：dirty + 用户选择 cancel → 阻止替换。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    importer.is_dirty_callback = lambda: True
    importer._confirm_replace_session = lambda: "cancel"

    assert importer._guard_replace_session() is False


def test_guard_replace_session_save_blocks_when_still_dirty():
    """P0-3：dirty + 用户选择 save 但保存失败（仍 dirty）→ 阻止替换。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    states = [True]  # 初始 dirty
    importer.is_dirty_callback = lambda: states[0]
    importer._confirm_replace_session = lambda: "save"
    status_messages: list = []
    importer.status_updater = status_messages.append

    assert importer._guard_replace_session() is False
    assert any("保存未完成" in m for m in status_messages)


def test_guard_replace_session_save_allows_replace_when_clean_after_save():
    """P0-3：dirty + 用户选择 save 且保存成功（dirty 清空）→ 放行替换。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    states = [True]  # 初始 dirty，保存后变 False
    importer.is_dirty_callback = lambda: states[0]
    importer._confirm_replace_session = lambda: (states.__setitem__(0, False), "save")[1]

    assert importer._guard_replace_session() is True


def test_guard_replace_session_without_callback_passes():
    """P0-3：未注入守卫回调时保留旧行为（直接放行）。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    importer.is_dirty_callback = lambda: True
    # 不设置 _confirm_replace_session（构造器 None）

    assert importer._guard_replace_session() is True


def test_is_dirty_delegates_to_callback_and_defaults_false():
    """P0-3：is_dirty 委派给 is_dirty_callback，未绑定时返回 False。"""
    importer = _make_file_importer(_FakeFileHandler(), _FakeEpubProcessor())
    # 未绑定 is_dirty_callback
    assert importer.is_dirty is False

    flag = [True]
    importer.is_dirty_callback = lambda: flag[0]
    assert importer.is_dirty is True
    flag[0] = False
    assert importer.is_dirty is False
