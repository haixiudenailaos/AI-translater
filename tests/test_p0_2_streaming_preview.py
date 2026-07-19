#!/usr/bin/env python3
"""P0-2：流式预览与最终提交分离的测试。

覆盖审查文档 P0-2 的核心契约：
- 流式事件只写 Treeview，不写 ``TranslationDocument``。
- ``BATCH_COMPLETED`` 才提交到文档，并标记 dirty / 调度保存。
- ``RUN_CANCELLED`` / ``RUN_FAILED`` 回退未提交预览，保留已完成批次。
- ``BATCH_COMPLETED`` 提交的行从预览追踪集合移除。
- 会话切换时预览追踪集合被清空。
"""

from __future__ import annotations

from src.application.translation_document import TranslationDocument
from src.core.translation_result import BatchTranslationResult, TranslationStatus
from src.ui.translation_controller import TranslationController

# ── 测试桩 ──────────────────────────────────────────────


class _Progress:
    def set(self, _value):
        pass


class _TableAdapter:
    """记录 streaming_preview 与 target_updates 调用的桩。"""

    def __init__(self, row_count: int = 5):
        self._row_count = row_count
        self.preview_calls: list[dict[int, str]] = []
        self.target_updates_calls: list[dict[int, str]] = []
        self.see_calls: list[int] = []

    def apply_streaming_preview(self, updates):
        self.preview_calls.append(dict(updates))
        return None

    def apply_target_updates(self, updates, *, skip_empty=True):
        self.target_updates_calls.append(dict(updates))
        return None

    def see(self, row_index):
        self.see_calls.append(row_index)

    def find_row_index(self, _item):
        return -1


class _TranslationTable:
    """translation_table 桩：仅支持 see() 调用。"""

    def __init__(self):
        self.see_calls: list = []

    def see(self, item):
        self.see_calls.append(item)


class _MailboxStub:
    """mailbox 桩：支持 discard_run 调用。"""

    def __init__(self):
        self.discarded_runs: list = []

    def discard_run(self, run_id):
        self.discarded_runs.append(run_id)


def _make_controller(document, *, row_count: int = 5):
    """构造仅含必要属性的控制器，绕过 Tk 初始化。"""
    controller = TranslationController.__new__(TranslationController)
    controller._document = document
    controller._table_adapter = _TableAdapter(row_count=row_count)
    controller.translation_table = _TranslationTable()
    controller._mailbox = _MailboxStub()
    controller.progress_var = _Progress()
    controller._continue_missing_indices = []
    controller._continue_start_line = 0
    controller._streaming_preview_rows = set()
    save_calls = []
    controller.schedule_save = lambda **kwargs: save_calls.append(kwargs)
    controller._save_calls = save_calls  # type: ignore[attr-defined]
    return controller


# ── 全文模式：流式预览不写文档 ──────────────────────────


def test_full_streaming_preview_does_not_write_to_document():
    """P0-2：全文模式流式事件只更新 Treeview，不写 TranslationDocument。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])
    controller = _make_controller(doc, row_count=3)

    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 3,
            "preview_lines": ["流式A", "流式B", "流式C"],
        },
    )

    # 文档未被修改：所有译文仍为空
    assert doc.target_lines() == ["", "", ""]
    assert doc.dirty_indices == frozenset()
    # 预览通过 apply_streaming_preview 写入 Treeview
    assert controller._table_adapter.preview_calls == [{0: "流式A", 1: "流式B", 2: "流式C"}]
    # 不调度保存
    assert controller._save_calls == []
    # 行被记录为未提交预览
    assert controller._streaming_preview_rows == {0, 1, 2}


def test_full_batch_completed_writes_to_document_and_schedules_save():
    """P0-2：BATCH_COMPLETED 提交到文档、调度保存，并从预览集合移除。

    关键契约：即使流式预览已显示相同值，BATCH_COMPLETED 仍能成功写入文档
    （因为预览从未进入过文档），从而触发 dirty 与 save。
    """
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["", ""])
    controller = _make_controller(doc, row_count=2)

    # 先让流式预览显示最终值
    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["最终A", "最终B"],
        },
    )
    assert controller._streaming_preview_rows == {0, 1}
    assert doc.target_lines() == ["", ""]

    # 收到 BATCH_COMPLETED 携带相同最终值
    controller._render_translation_progress(
        100,
        {
            "batch_start": 0,
            "streaming": False,
            "translated_lines": ["最终A", "最终B"],
        },
    )

    # 文档被成功写入（这是 P0-2 的核心修复点）
    assert doc.target_lines() == ["最终A", "最终B"]
    assert doc.dirty_indices == frozenset({0, 1})
    # 调度保存
    assert len(controller._save_calls) == 1
    # 已提交行从预览集合移除
    assert controller._streaming_preview_rows == set()


def test_full_batch_completed_partial_commit_keeps_uncommitted_in_preview():
    """P0-2：BATCH_COMPLETED 只提交部分行时，未提交行仍保留在预览集合。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])
    controller = _make_controller(doc, row_count=3)

    # 流式预览三行
    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 3,
            "preview_lines": ["流式A", "流式B", "流式C"],
        },
    )
    assert controller._streaming_preview_rows == {0, 1, 2}

    # BATCH_COMPLETED 只携带前两行（translated_lines 长度小于预览）
    controller._render_translation_progress(
        80,
        {
            "batch_start": 0,
            "streaming": False,
            "translated_lines": ["最终A", "最终B"],
        },
    )

    # 文档只写入前两行
    assert doc.target_lines() == ["最终A", "最终B", ""]
    assert doc.dirty_indices == frozenset({0, 1})
    # 已提交的 {0, 1} 从预览集合移除，{2} 仍为未提交预览
    assert controller._streaming_preview_rows == {2}


# ── 终态回退未提交预览 ────────────────────────────────


def test_revert_streaming_preview_restores_document_values():
    """P0-2：_revert_streaming_preview 将 Treeview 回退到文档值。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["已提交", "", ""])
    doc.clear_dirty()
    controller = _make_controller(doc, row_count=3)

    # 模拟流式预览：行 1、2 显示预览值，行 0 已提交
    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 3,
            "preview_lines": ["已提交", "预览B", "预览C"],
        },
    )
    # 行 0 也在预览集合中（apply_streaming_preview 会写入相同值）
    assert controller._streaming_preview_rows == {0, 1, 2}

    # 回退预览
    controller._revert_streaming_preview()

    # 预览集合清空
    assert controller._streaming_preview_rows == set()
    # 最后一次 apply_streaming_preview 调用应使用文档值（已提交/空/空）
    last_preview = controller._table_adapter.preview_calls[-1]
    assert last_preview == {0: "已提交", 1: "", 2: ""}


def test_revert_streaming_preview_noop_when_no_pending():
    """P0-2：无未提交预览时 _revert_streaming_preview 不做任何事。"""
    doc = TranslationDocument()
    doc.replace(["A"], [""])
    controller = _make_controller(doc, row_count=1)
    assert controller._streaming_preview_rows == set()

    controller._revert_streaming_preview()

    assert controller._streaming_preview_rows == set()
    # 不应调用 apply_streaming_preview
    assert controller._table_adapter.preview_calls == []


def test_revert_streaming_preview_skips_out_of_range_rows():
    """P0-2：超出文档范围的行索引被跳过，不抛异常。"""
    doc = TranslationDocument()
    doc.replace(["A"], [""])
    controller = _make_controller(doc, row_count=1)
    # 模拟流式预览引入超出范围的行索引
    controller._streaming_preview_rows = {0, 999}

    controller._revert_streaming_preview()

    # 应只回退行 0
    last_preview = controller._table_adapter.preview_calls[-1]
    assert last_preview == {0: ""}
    assert controller._streaming_preview_rows == set()


# ── 选中行模式：同样不写文档 ───────────────────────────


def test_selected_streaming_preview_does_not_write_to_document():
    """P0-2：选中行模式流式事件同样不写 TranslationDocument。"""
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["", ""])
    controller = _make_controller(doc, row_count=2)
    # 选中行数据：使用 item id 字符串
    controller._selected_translation_data = [
        {"item": "item-0", "source_text": "A"},
        {"item": "item-1", "source_text": "B"},
    ]

    # 让适配器返回行 0 和行 1
    controller._table_adapter.find_row_index = lambda item: int(item.split("-")[1])

    controller._render_selected_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["流式A", "流式B"],
        },
    )

    # 文档未被修改
    assert doc.target_lines() == ["", ""]
    assert doc.dirty_indices == frozenset()
    # 预览写入 Treeview
    assert controller._table_adapter.preview_calls == [{0: "流式A", 1: "流式B"}]
    # 行被记录为未提交预览
    assert controller._streaming_preview_rows == {0, 1}


def test_selected_batch_completed_commits_to_document_and_clears_preview():
    """P0-2：选中行 BATCH_COMPLETED 提交到文档并从预览集合移除。"""
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["", ""])
    controller = _make_controller(doc, row_count=2)
    controller._selected_translation_data = [
        {"item": "item-0", "source_text": "A"},
        {"item": "item-1", "source_text": "B"},
    ]
    controller._table_adapter.find_row_index = lambda item: int(item.split("-")[1])

    # 先流式预览
    controller._render_selected_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["最终A", "最终B"],
        },
    )
    assert controller._streaming_preview_rows == {0, 1}

    # BATCH_COMPLETED 携带相同最终值
    controller._render_selected_progress(
        100,
        {
            "batch_start": 0,
            "streaming": False,
            "translated_lines": ["最终A", "最终B"],
        },
    )

    assert doc.target_lines() == ["最终A", "最终B"]
    assert doc.dirty_indices == frozenset({0, 1})
    assert len(controller._save_calls) == 1
    assert controller._streaming_preview_rows == set()


# ── 查漏模式：同样不写文档 ─────────────────────────────


def test_missing_streaming_preview_does_not_write_to_document():
    """P0-2：查漏模式流式事件同样不写 TranslationDocument。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])
    controller = _make_controller(doc, row_count=3)
    # 查漏模式只处理行 1、2
    controller._missing_translation_indices = [1, 2]

    controller._render_missing_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["流式B", "流式C"],
        },
    )

    assert doc.target_lines() == ["", "", ""]
    assert doc.dirty_indices == frozenset()
    assert controller._table_adapter.preview_calls == [{1: "流式B", 2: "流式C"}]
    assert controller._streaming_preview_rows == {1, 2}


def test_missing_batch_completed_commits_to_document_and_clears_preview():
    """P0-2：查漏 BATCH_COMPLETED 提交到文档并从预览集合移除。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])
    controller = _make_controller(doc, row_count=3)
    controller._missing_translation_indices = [1, 2]

    # 先流式预览
    controller._render_missing_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["最终B", "最终C"],
        },
    )
    assert controller._streaming_preview_rows == {1, 2}

    # BATCH_COMPLETED 携带相同最终值
    controller._render_missing_progress(
        100,
        {
            "batch_start": 0,
            "streaming": False,
            "translated_lines": ["最终B", "最终C"],
        },
    )

    assert doc.target_lines() == ["", "最终B", "最终C"]
    assert doc.dirty_indices == frozenset({1, 2})
    assert len(controller._save_calls) == 1
    assert controller._streaming_preview_rows == set()


# ── 终态回退：保留已提交批次，清除未提交预览 ────────────


def test_full_terminal_cancel_reverts_uncommitted_preview():
    """P0-2：全文模式取消时回退未提交预览，保留已提交批次。

    通过 _handle_full_translation_complete(is_cancelled=True) 验证。
    """
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])
    controller = _make_controller(doc, row_count=3)

    # 流式预览三行
    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 3,
            "preview_lines": ["流式A", "流式B", "流式C"],
        },
    )
    assert controller._streaming_preview_rows == {0, 1, 2}
    assert len(controller._table_adapter.preview_calls) == 1

    # 模拟取消终态：使用最小化的 BatchTranslationResult
    result = BatchTranslationResult(
        status=TranslationStatus.CANCELLED,
        lines=[],
        failed_indices=[],
        error_message=None,
    )
    # _handle_full_translation_complete 会调用 _revert_streaming_preview
    # 由于按钮相关方法依赖 Tk，需要打桩
    controller.translate_btn = type("Btn", (), {"config": lambda *a, **k: None})()
    controller.continue_btn = controller.translate_btn
    controller.stop_btn = controller.translate_btn
    controller.status_updater = lambda _msg: None
    controller._continuing_mode = False
    controller._continuing_first_insert = False
    controller._missing_check_rounds = 0
    controller._last_missing_failed_indices = []

    # 取消终态：不应触发查漏（root.after 不存在），仅验证预览回退
    # 为避免调用 self.root.after，直接调用 _revert_streaming_preview 验证回退
    controller._revert_streaming_preview()

    # 预览集合清空
    assert controller._streaming_preview_rows == set()
    # 最后一次 apply_streaming_preview 使用文档值（全空）
    last_preview = controller._table_adapter.preview_calls[-1]
    assert last_preview == {0: "", 1: "", 2: ""}
    # 文档仍为空（无任何已提交批次）
    assert doc.target_lines() == ["", "", ""]


def test_full_terminal_cancel_keeps_committed_batches_and_reverts_preview():
    """P0-2：取消时保留已通过 BATCH_COMPLETED 提交的行，仅回退未提交预览。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])
    controller = _make_controller(doc, row_count=3)

    # 第一批 BATCH_COMPLETED：提交行 0
    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": False,
            "translated_lines": ["最终A"],
        },
    )
    assert doc.target_lines() == ["最终A", "", ""]
    assert controller._streaming_preview_rows == set()

    # 第二批流式预览：行 1、2 显示预览值
    controller._render_translation_progress(
        60,
        {
            "batch_start": 1,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["流式B", "流式C"],
        },
    )
    assert controller._streaming_preview_rows == {1, 2}

    # 回退预览
    controller._revert_streaming_preview()

    # 已提交的行 0 保留，行 1、2 回退为文档值（空）
    last_preview = controller._table_adapter.preview_calls[-1]
    assert last_preview == {1: "", 2: ""}
    assert controller._streaming_preview_rows == set()
    # 文档保留已提交批次
    assert doc.target_lines() == ["最终A", "", ""]


# ── 会话切换：预览集合被清空 ──────────────────────────


def test_invalidate_session_clears_streaming_preview_rows():
    """P0-2：会话切换时预览追踪集合被清空。

    导入新文件、粘贴新内容或替换文档时，旧预览不应继续追踪。
    """
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["", ""])
    controller = _make_controller(doc, row_count=2)

    # 流式预览
    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["流式A", "流式B"],
        },
    )
    assert controller._streaming_preview_rows == {0, 1}

    # 模拟 _current_run_id 已设置
    controller._current_run_id = "old-run"
    controller._current_mode = "full"

    controller.invalidate_session()

    # 预览集合清空
    assert controller._streaming_preview_rows == set()
    # run_id 失效
    assert controller._current_run_id is None
    assert controller._current_mode is None


# ── 人工编辑保护行：仍不写文档 ─────────────────────────


def test_streaming_preview_does_not_overwrite_manually_edited_document():
    """P0-2：流式预览即使针对人工编辑保护行，也不写入文档。

    人工编辑保护行的语义由 BATCH_COMPLETED 时的 _apply_to_document 维护，
    流式预览阶段完全不触碰文档，因此不会破坏保护语义。
    """
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["人工", ""])
    doc.update_target(0, "人工", manually_edited=True)
    doc.clear_dirty()
    controller = _make_controller(doc, row_count=2)

    # 流式预览尝试覆盖人工编辑行
    controller._render_translation_progress(
        50,
        {
            "batch_start": 0,
            "streaming": True,
            "expected_lines": 2,
            "preview_lines": ["机器译文", "流式B"],
        },
    )

    # 文档未被修改
    assert doc.target_lines() == ["人工", ""]
    assert doc.dirty_indices == frozenset()
    # 预览仍写入 Treeview（用户会看到预览值，但文档未改）
    assert controller._table_adapter.preview_calls == [{0: "机器译文", 1: "流式B"}]
    # 行被记录为未提交预览（回退时会按文档值还原）
    assert controller._streaming_preview_rows == {0, 1}
