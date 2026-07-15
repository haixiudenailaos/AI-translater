#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译项目领域模型单元测试（UXF-001 / UXF-003 / UXF-004 / UXF-005）

验证 src/domain/project.py 中的：
- TaskStatus 任务生命周期状态
- SaveStatus 保存语义
- LineEditState 行编辑标记
- ModelSnapshot 模型配置快照
- TranslationProject 翻译项目实体（含稀疏行计算、手工编辑保护、状态流转）

领域层不依赖任何项目外模块，可独立运行。
"""

import pytest

from src.domain.project import (
    LineEditState,
    ModelSnapshot,
    SaveStatus,
    TaskStatus,
    TranslationProject,
)
from src.domain.translation import OperationStatus


# ── TaskStatus ──────────────────────────────────

class TestTaskStatus:
    def test_status_values_are_strings(self):
        """状态枚举值为字符串，可直接序列化"""
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.PAUSED == "paused"
        assert TaskStatus.PARTIAL == "partial"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.CANCELLED == "cancelled"
        assert TaskStatus.ERROR == "error"

    def test_terminal_states(self):
        """completed/cancelled/error 为终态"""
        assert TaskStatus.COMPLETED.is_terminal
        assert TaskStatus.CANCELLED.is_terminal
        assert TaskStatus.ERROR.is_terminal
        assert not TaskStatus.PENDING.is_terminal
        assert not TaskStatus.RUNNING.is_terminal
        assert not TaskStatus.PARTIAL.is_terminal

    def test_exportable_states(self):
        """completed 和 partial 允许导出"""
        assert TaskStatus.COMPLETED.is_exportable
        assert TaskStatus.PARTIAL.is_exportable
        assert not TaskStatus.PENDING.is_exportable
        assert not TaskStatus.ERROR.is_exportable

    def test_running_states(self):
        """running 和 paused 都算活跃"""
        assert TaskStatus.RUNNING.is_running
        assert TaskStatus.PAUSED.is_running
        assert not TaskStatus.PENDING.is_running
        assert not TaskStatus.COMPLETED.is_running


# ── SaveStatus ──────────────────────────────────

class TestSaveStatus:
    def test_status_values(self):
        assert SaveStatus.SAVED == "saved"
        assert SaveStatus.UNSAVED == "unsaved"
        assert SaveStatus.SAVING == "saving"
        assert SaveStatus.SAVE_FAILED == "save_failed"


# ── ModelSnapshot ──────────────────────────────

class TestModelSnapshot:
    def test_defaults(self):
        snap = ModelSnapshot()
        assert snap.provider == ""
        assert snap.batch_size == 20

    def test_round_trip(self):
        snap = ModelSnapshot(
            provider="siliconflow",
            model_name="deepseek-v3",
            target_language="中文",
            prompt_version="v1",
            glossary_version="v2",
            batch_size=30,
        )
        data = snap.to_dict()
        restored = ModelSnapshot.from_dict(data)
        assert restored == snap

    def test_from_dict_none(self):
        snap = ModelSnapshot.from_dict(None)
        assert snap.batch_size == 20


# ── TranslationProject 基础 ─────────────────────

def _make_project(lines=None, translations=None):
    """创建测试用项目"""
    orig = lines if lines is not None else ["Hello", "World", "", "Foo"]
    trans = translations if translations is not None else ["", "", "", ""]
    return TranslationProject(
        project_id="test-001",
        source_path="/tmp/test.txt",
        source_fingerprint="abc123",
        file_type="txt",
        mapping_dir="/tmp/test_map",
        original_lines=list(orig),
        translated_lines=list(trans),
    )


class TestTranslationProjectBasics:
    def test_total_lines(self):
        project = _make_project()
        assert project.total_lines == 4

    def test_translated_count_empty(self):
        project = _make_project()
        assert project.translated_count == 0

    def test_translated_count_partial(self):
        project = _make_project(translations=["你好", "", "", ""])
        assert project.translated_count == 1

    def test_completion_ratio_empty(self):
        project = _make_project()
        assert project.completion_ratio == 0.0

    def test_completion_ratio_full(self):
        project = _make_project(
            translations=["你好", "世界", "", "富"]
        )
        # 3 个非空原文行中 3 个已翻译
        assert project.completion_ratio == 1.0

    def test_completion_ratio_zero_lines(self):
        project = TranslationProject(
            project_id="empty", source_path="", source_fingerprint="",
            file_type="txt", mapping_dir="",
        )
        assert project.completion_ratio == 1.0


# ── UXF-002：稀疏行计算 ─────────────────────────

class TestPendingIndices:
    def test_all_empty_translations(self):
        """全部译文为空时，所有非空原文行待翻译"""
        project = _make_project()
        pending = project.get_pending_indices()
        # 第3行是空原文，不待翻译
        assert pending == (0, 1, 3)

    def test_skip_translated_lines(self):
        """已翻译行不在待翻译列表"""
        project = _make_project(translations=["你好", "", "", ""])
        pending = project.get_pending_indices()
        assert 0 not in pending  # 已翻译
        assert 1 in pending
        assert 3 in pending

    def test_skip_manually_edited(self):
        """手工编辑行不待翻译（UXF-001）"""
        project = _make_project()
        project.manually_edited_indices.add(1)
        pending = project.get_pending_indices()
        assert 1 not in pending
        assert 0 in pending
        assert 3 in pending

    def test_skip_empty_original(self):
        """空原文行不待翻译"""
        project = _make_project()
        pending = project.get_pending_indices()
        assert 2 not in pending  # 空原文

    def test_skip_whitespace_only_original(self):
        """仅空白字符的原文行不待翻译"""
        project = _make_project(lines=["Hello", "   ", "World"])
        pending = project.get_pending_indices()
        assert 1 not in pending
        assert pending == (0, 2)

    def test_pending_count(self):
        project = _make_project()
        assert project.pending_count == 3


# ── UXF-001：手工编辑保护 ───────────────────────

class TestManualEditProtection:
    def test_auto_translation_skips_manual(self):
        """自动翻译不覆盖手工编辑行"""
        project = _make_project()
        project.mark_manually_edited(0, "手工译文")
        # 尝试自动写入（manually_edited=False）
        project.apply_translation(0, "自动译文", manually_edited=False)
        assert project.translated_lines[0] == "手工译文"

    def test_manual_translation_overwrites(self):
        """手工翻译可以覆盖手工编辑行"""
        project = _make_project()
        project.mark_manually_edited(0, "旧手工")
        project.apply_translation(0, "新手工", manually_edited=True)
        assert project.translated_lines[0] == "新手工"

    def test_auto_translation_writes_non_manual(self):
        """自动翻译可以写入非手工编辑行"""
        project = _make_project()
        project.apply_translation(1, "自动译文", manually_edited=False)
        assert project.translated_lines[1] == "自动译文"

    def test_batch_translation_skips_manual(self):
        """批量翻译跳过手工编辑行"""
        project = _make_project()
        project.mark_manually_edited(1, "手工")
        project.apply_batch_translation(
            indices=(0, 1, 3),
            translated_lines=("译文0", "译文1", "译文3"),
        )
        assert project.translated_lines[0] == "译文0"
        assert project.translated_lines[1] == "手工"  # 未被覆盖
        assert project.translated_lines[3] == "译文3"

    def test_mark_saved_after_edit(self):
        """编辑后标记为未保存"""
        project = _make_project()
        project.save_status = SaveStatus.SAVED
        project.apply_translation(0, "译文")
        assert project.save_status == SaveStatus.UNSAVED


# ── UXF-005：状态流转 ───────────────────────────

class TestStatusTransition:
    def test_completed_requires_no_failed(self):
        """有失败行时不能标记 completed，降级为 partial"""
        project = _make_project()
        project.failed_indices.add(0)
        project.transition_status(TaskStatus.COMPLETED)
        assert project.status == TaskStatus.PARTIAL

    def test_completed_requires_no_pending(self):
        """有未翻译行时不能标记 completed，降级为 partial"""
        project = _make_project()  # 有3个待翻译行
        project.transition_status(TaskStatus.COMPLETED)
        assert project.status == TaskStatus.PARTIAL

    def test_completed_when_all_done(self):
        """全部完成时可标记 completed"""
        project = _make_project(translations=["你好", "世界", "", "富"])
        project.transition_status(TaskStatus.COMPLETED)
        assert project.status == TaskStatus.COMPLETED

    def test_update_from_succeeded_operation(self):
        """操作成功且无失败/待翻译 → completed"""
        project = _make_project(translations=["你好", "世界", "", "富"])
        project.update_from_operation(OperationStatus.SUCCEEDED)
        assert project.status == TaskStatus.COMPLETED

    def test_update_from_partial_operation(self):
        """部分成功操作 → partial"""
        project = _make_project()
        project.update_from_operation(OperationStatus.PARTIAL)
        assert project.status == TaskStatus.PARTIAL

    def test_update_from_cancelled_with_progress(self):
        """取消但有进度 → partial"""
        project = _make_project(translations=["你好", "", "", ""])
        project.update_from_operation(OperationStatus.CANCELLED)
        assert project.status == TaskStatus.PARTIAL

    def test_update_from_cancelled_no_progress(self):
        """取消且无进度 → cancelled"""
        project = _make_project()
        project.update_from_operation(OperationStatus.CANCELLED)
        assert project.status == TaskStatus.CANCELLED

    def test_update_from_failed_with_progress(self):
        """失败但有进度 → partial"""
        project = _make_project(translations=["你好", "", "", ""])
        project.update_from_operation(OperationStatus.FAILED)
        assert project.status == TaskStatus.PARTIAL

    def test_update_from_failed_no_progress(self):
        """失败且无进度 → error"""
        project = _make_project()
        project.update_from_operation(OperationStatus.FAILED)
        assert project.status == TaskStatus.ERROR


# ── 序列化 ──────────────────────────────────

class TestSerialization:
    def test_round_trip(self):
        project = _make_project(translations=["你好", "", "", ""])
        project.manually_edited_indices.add(0)
        project.failed_indices.add(1)
        project.status = TaskStatus.PARTIAL
        project.last_error = "timeout"
        project.model_snapshot = ModelSnapshot(
            provider="deepseek", model_name="deepseek-v3",
        )
        project.created_at = "2026-07-15T10:00:00"

        data = project.to_dict()
        restored = TranslationProject.from_dict(data)

        assert restored.project_id == project.project_id
        assert restored.original_lines == project.original_lines
        assert restored.translated_lines == project.translated_lines
        assert restored.manually_edited_indices == project.manually_edited_indices
        assert restored.failed_indices == project.failed_indices
        assert restored.status == project.status
        assert restored.last_error == project.last_error
        assert restored.model_snapshot == project.model_snapshot
        assert restored.created_at == project.created_at

    def test_from_dict_missing_fields(self):
        """缺失字段时使用默认值"""
        data = {"project_id": "x", "source_path": "/tmp/x"}
        project = TranslationProject.from_dict(data)
        assert project.project_id == "x"
        assert project.status == TaskStatus.PENDING
        assert project.original_lines == []

    def test_from_dict_invalid_status(self):
        """无效状态值回退到 PENDING"""
        data = {"project_id": "x", "status": "invalid_status"}
        project = TranslationProject.from_dict(data)
        assert project.status == TaskStatus.PENDING


# ── ensure_translated_capacity ─────────────────

class TestEnsureCapacity:
    def test_extend_short(self):
        project = _make_project()
        project.translated_lines = ["", ""]
        project.ensure_translated_capacity()
        assert len(project.translated_lines) == 4

    def test_truncate_long(self):
        project = _make_project()
        project.translated_lines = ["", "", "", "", "", ""]
        project.ensure_translated_capacity()
        assert len(project.translated_lines) == 4


# ── failed 行管理 ──────────────────────────────

class TestFailedLineManagement:
    def test_mark_failed(self):
        project = _make_project()
        project.mark_failed(2, "network error")
        assert 2 in project.failed_indices
        assert project.retry_counts[2] == 1
        assert project.last_error == "network error"

    def test_mark_failed_increments_retry(self):
        project = _make_project()
        project.mark_failed(2, "error1")
        project.mark_failed(2, "error2")
        assert project.retry_counts[2] == 2

    def test_clear_failed(self):
        project = _make_project()
        project.mark_failed(2)
        project.clear_failed(2)
        assert 2 not in project.failed_indices

    def test_apply_translation_clears_failed(self):
        """成功写入译文后清除失败标记"""
        project = _make_project()
        project.mark_failed(0)
        project.apply_translation(0, "你好")
        assert 0 not in project.failed_indices
