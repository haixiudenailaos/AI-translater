#!/usr/bin/env python3
"""PERF：翻译文档状态模型单元测试。

覆盖 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §7.6 的要求：
- replace 后原文和译文严格对齐
- 译文更新增加 version 并记录 dirty index
- 相同值更新不增加 version
- 空结果不覆盖人工译文
- snapshot 为不可变数据，后续编辑不改变旧快照
- 稀疏索引更新写入原始行
"""

from __future__ import annotations

import pytest

from src.application.translation_document import TranslationDocument, TranslationRow

# ── replace 与对齐 ──────────────────────────────────────


def test_replace_aligns_source_and_target():
    """replace 后原文和译文严格对齐。"""
    doc = TranslationDocument()
    doc.replace(["原文1", "原文2", "原文3"], ["译文1", "译文2"])

    assert doc.row_count == 3
    assert doc.source_lines() == ["原文1", "原文2", "原文3"]
    assert doc.target_lines() == ["译文1", "译文2", ""]
    assert doc.version == 1


def test_replace_with_empty_targets():
    """只有原文时，译文全部为空。"""
    doc = TranslationDocument()
    doc.replace(["A", "B"], [])

    assert doc.target_lines() == ["", ""]
    assert doc.row_count == 2


def test_replace_truncates_extra_targets():
    """译文比原文多时截断。"""
    doc = TranslationDocument()
    doc.replace(["A"], ["译", "多余"])

    assert doc.row_count == 1
    assert doc.target_lines() == ["译"]


def test_replace_increments_version():
    """每次 replace 增加 version。"""
    doc = TranslationDocument()
    assert doc.version == 0

    doc.replace(["A"], [])
    assert doc.version == 1

    doc.replace(["B"], ["译"])
    assert doc.version == 2


# ── update_target 与 version/dirty ─────────────────────


def test_update_target_increments_version_and_marks_dirty():
    """译文更新增加 version 并记录 dirty index。"""
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["", ""])

    changed = doc.update_target(0, "译文A")

    assert changed is True
    assert doc.version == 2
    assert 0 in doc.dirty_indices
    assert 1 not in doc.dirty_indices


def test_same_value_update_does_not_increment_version():
    """相同值更新不增加 version。"""
    doc = TranslationDocument()
    doc.replace(["A"], ["译文A"])
    version_before = doc.version

    changed = doc.update_target(0, "译文A")

    assert changed is False
    assert doc.version == version_before


def test_update_multiple_rows_collects_dirty_indices():
    """更新多行后 dirty_indices 收集所有变更行。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C"], ["", "", ""])

    doc.update_target(0, "译A")
    doc.update_target(2, "译C")

    assert doc.dirty_indices == frozenset({0, 2})


def test_clear_dirty_resets_dirty_without_version_change():
    """clear_dirty 清除 dirty 标记但不增加 version。"""
    doc = TranslationDocument()
    doc.replace(["A"], [""])
    doc.update_target(0, "译")
    version_before = doc.version

    doc.clear_dirty()

    assert doc.dirty_indices == frozenset()
    assert doc.version == version_before


# ── 人工编辑保护 ────────────────────────────────────────


def test_manually_edited_flag_is_set():
    """manually_edited=True 标记行。"""
    doc = TranslationDocument()
    doc.replace(["A"], ["旧译"])

    doc.update_target(0, "新译", manually_edited=True)

    assert doc.is_manually_edited(0) is True


def test_empty_result_does_not_overwrite_via_update_target():
    """PERF §7.2 D1：模型层强制保护人工编辑过的行不被空响应覆盖。

    人工标记过（``manually_edited=True``）的行，若后续非人工来源
    （``manually_edited=False``，如翻译批次返回空字符串）尝试写入空值，
    模型应拒绝覆盖，保留原人工译文。原实现仅由适配器层跳过空响应，
    模型层无保护，导致任何直接调用 ``update_target`` 的路径仍可能清空
    用户已确认的译文。修正后模型层强制保护。
    """
    doc = TranslationDocument()
    doc.replace(["A"], ["人工译"])
    doc.update_target(0, "人工译", manually_edited=True)

    # 非人工来源的空响应尝试覆盖：应被拒绝
    overwritten = doc.update_target(0, "")

    assert overwritten is False
    assert doc.target_lines() == ["人工译"]
    assert doc.is_manually_edited(0) is True


def test_non_empty_batch_result_overwrites_manually_edited_row():
    """P0-4：人工编辑过的行不被任何非人工来源的结果覆盖。

    旧 generation 的机器结果（包括非空）不得覆盖人工译文。
    如需重译，必须先通过 clear_manual_flag 清除人工编辑标记。
    """
    doc = TranslationDocument()
    doc.replace(["A"], ["旧人工译"])
    doc.update_target(0, "旧人工译", manually_edited=True)

    # 非空翻译结果：P0-4 不允许覆盖人工编辑
    overwritten = doc.update_target(0, "新机器译")

    assert overwritten is False
    assert doc.target_lines() == ["旧人工译"]
    assert doc.is_manually_edited(0) is True

    # 清除人工编辑标记后，机器结果可以覆盖
    doc.clear_manual_flag(0)
    overwritten = doc.update_target(0, "新机器译")
    assert overwritten is True
    assert doc.target_lines() == ["新机器译"]


def test_manually_edited_persists_across_empty_update():
    """人工编辑标记在后续更新中保留（OR 语义）。"""
    doc = TranslationDocument()
    doc.replace(["A"], [""])
    doc.update_target(0, "译1", manually_edited=True)

    # 后续非人工更新不应清除标记
    doc.update_target(0, "译2", manually_edited=False)

    assert doc.is_manually_edited(0) is True


def test_unedited_row_allows_empty_overwrite():
    """PERF §7.2 D1：未人工编辑过的行允许空响应覆盖（无保护）。"""
    doc = TranslationDocument()
    doc.replace(["A"], ["机器译"])  # 未标记 manually_edited

    # 非人工来源的空响应：允许覆盖（无人工标记可保护）
    overwritten = doc.update_target(0, "")

    assert overwritten is True
    assert doc.target_lines() == [""]


# ── snapshot 不可变性 ──────────────────────────────────


def test_snapshot_is_immutable_to_subsequent_edits():
    """snapshot 后的 tuple 不受后续编辑影响。"""
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["译A", ""])

    version, targets = doc.snapshot_targets()
    assert version == 1
    assert targets == ("译A", "")

    # 后续编辑不改变旧快照
    doc.update_target(1, "译B")

    assert targets == ("译A", "")  # 旧快照未变
    # replace(0→1) + update_target(1→2) = version 2
    assert doc.snapshot_targets() == (2, ("译A", "译B"))


def test_snapshot_returns_tuple_type():
    """snapshot 返回 tuple 类型，确保不可变。"""
    doc = TranslationDocument()
    doc.replace(["A"], ["译"])

    _, targets = doc.snapshot_targets()

    assert isinstance(targets, tuple)
    assert targets == ("译",)


# ── 稀疏索引 ────────────────────────────────────────────


def test_sparse_index_update_writes_correct_row():
    """稀疏索引更新写入正确的原始行。"""
    doc = TranslationDocument()
    doc.replace(["A", "B", "C", "D"], ["", "", "", ""])

    # 只更新行 2
    doc.update_target(2, "译C")

    assert doc.target_lines() == ["", "", "译C", ""]
    assert doc.dirty_indices == frozenset({2})


# ── 查漏/缺失行查找 ────────────────────────────────────


def test_find_missing_indices():
    """查找原文非空但译文为空的行。"""
    doc = TranslationDocument()
    doc.replace(["A", "", "C", "D"], ["译A", "", "", "译D"])

    missing = doc.find_missing_indices()

    assert missing == [2]


def test_find_pending_indices_alias():
    """find_pending_indices 是 find_missing_indices 的别名。"""
    doc = TranslationDocument()
    doc.replace(["A", "B"], ["", "译B"])

    assert doc.find_pending_indices() == doc.find_missing_indices() == [0]


# ── TranslationRow dataclass ───────────────────────────


def test_translation_row_defaults():
    """TranslationRow 默认值。"""
    row = TranslationRow(source="原文")

    assert row.source == "原文"
    assert row.target == ""
    assert row.manually_edited is False


def test_translation_row_slots():
    """TranslationRow 使用 slots，不能添加新属性。"""
    row = TranslationRow(source="A")

    with pytest.raises(AttributeError):
        row.new_field = "value"  # type: ignore[attr-defined]


# ── has_dirty_rows（自动保存热路径）─────────────────────


def test_has_dirty_rows_tracks_the_dirty_set():
    """``has_dirty_rows`` 与 ``dirty_indices`` 的判空语义一致。"""
    doc = TranslationDocument([TranslationRow(source="A"), TranslationRow(source="B")])

    assert doc.has_dirty_rows is False

    doc.update_target(0, "甲")
    assert doc.has_dirty_rows is True
    assert bool(doc.dirty_indices) is True

    doc.clear_dirty()
    assert doc.has_dirty_rows is False
    assert bool(doc.dirty_indices) is False


def test_has_dirty_rows_does_not_copy_the_dirty_set():
    """判空不得拷贝 dirty 集合。

    自动保存调度在每个批次完成时检查一次，而 dirty 集合在保存成功前只增
    不减。若这里走 ``dirty_indices`` 的 frozenset 全量拷贝，长文档翻译会
    退化为 O(n²)，在 Tk 主线程上表现为界面逐渐卡顿。
    """
    doc = TranslationDocument([TranslationRow(source=f"L{i}") for i in range(1000)])
    for index in range(1000):
        doc.update_target(index, f"译{index}")

    class ExplodingSet(set):
        def __iter__(self):
            raise AssertionError("has_dirty_rows 不应遍历/拷贝 dirty 集合")

    doc._dirty_indices = ExplodingSet(range(1000))

    assert doc.has_dirty_rows is True
