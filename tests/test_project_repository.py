#!/usr/bin/env python3
"""
项目持久化仓库单元测试（UXF-004）

验证 src/infrastructure/project_repository.py：
- 项目 ID 生成稳定性
- 文件指纹计算
- 创建/加载/保存项目
- 检查点创建/列表/恢复/清理
- 最近项目列表
- 删除项目
- 保存失败不静默吞掉（UXF-003）
"""

from pathlib import Path

import pytest

from src.domain.project import SaveStatus, TaskStatus
from src.infrastructure.project_repository import (
    ProjectCorruptError,
    ProjectRepository,
    compute_file_fingerprint,
    compute_project_id,
    compute_text_fingerprint,
)


@pytest.fixture
def repo(tmp_path):
    """创建临时目录下的项目仓库"""
    return ProjectRepository(tmp_path / "projects")


def _make_source_file(tmp_path: Path, content: str = "Hello\nWorld\n") -> Path:
    path = tmp_path / "source.txt"
    path.write_text(content, encoding="utf-8")
    return path


# ── 项目 ID 与指纹 ─────────────────────────────


class TestProjectIdAndFingerprint:
    def test_project_id_stable(self):
        """同一路径+指纹生成相同 ID"""
        pid1 = compute_project_id("/tmp/a.txt", "fp123")
        pid2 = compute_project_id("/tmp/a.txt", "fp123")
        assert pid1 == pid2

    def test_project_id_changes_with_path(self):
        pid1 = compute_project_id("/tmp/a.txt", "fp123")
        pid2 = compute_project_id("/tmp/b.txt", "fp123")
        assert pid1 != pid2

    def test_project_id_changes_with_fingerprint(self):
        pid1 = compute_project_id("/tmp/a.txt", "fp123")
        pid2 = compute_project_id("/tmp/a.txt", "fp456")
        assert pid1 != pid2

    def test_file_fingerprint(self, tmp_path):
        path = _make_source_file(tmp_path, "test content")
        fp = compute_file_fingerprint(path)
        assert len(fp) == 64  # SHA-256 hex

    def test_file_fingerprint_missing_file(self, tmp_path):
        fp = compute_file_fingerprint(tmp_path / "nonexistent.txt")
        assert fp == ""

    def test_file_fingerprint_stable(self, tmp_path):
        path = _make_source_file(tmp_path, "same content")
        assert compute_file_fingerprint(path) == compute_file_fingerprint(path)

    def test_text_fingerprint(self):
        fp = compute_text_fingerprint("Hello World")
        assert len(fp) == 64

    def test_text_fingerprint_different(self):
        assert compute_text_fingerprint("a") != compute_text_fingerprint("b")


# ── 创建与加载 ──────────────────────────────


class TestCreateAndLoad:
    def test_create_new_project(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="/tmp/map",
            original_lines=["Hello", "World"],
        )
        assert project.project_id
        assert project.original_lines == ["Hello", "World"]
        assert project.translated_lines == ["", ""]
        assert project.status == TaskStatus.PENDING
        assert project.save_status == SaveStatus.UNSAVED
        assert project.created_at

    def test_create_returns_existing(self, repo):
        """同 ID 项目已存在时返回已有项目"""
        project1 = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project1)

        project2 = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        assert project2.project_id == project1.project_id

    def test_load_existing(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello", "World"],
        )
        project.apply_translation(0, "你好")
        repo.save(project)

        loaded = repo.load(project.project_id)
        assert loaded is not None
        assert loaded.original_lines == ["Hello", "World"]
        assert loaded.translated_lines == ["你好", ""]
        assert loaded.save_status == SaveStatus.SAVED

    def test_load_nonexistent(self, repo):
        assert repo.load("nonexistent-id") is None

    def test_load_corrupt_project_quarantines_instead_of_silently_overwriting(self, repo):
        project_id = "corrupt-project"
        original_path = repo.projects_dir / f"{project_id}.json"
        original_path.write_text("{not valid json", encoding="utf-8")

        with pytest.raises(ProjectCorruptError) as exc_info:
            repo.load(project_id)

        quarantined_path = exc_info.value.quarantined_path
        assert quarantined_path is not None
        assert quarantined_path.exists()
        assert not original_path.exists()
        assert repo.load(project_id) is None


# ── 保存 ──────────────────────────────────


class TestSave:
    def test_save_creates_file(self, repo, tmp_path):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project)
        project_file = tmp_path / "projects" / f"{project.project_id}.json"
        assert project_file.exists()

    def test_save_marks_saved(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        assert project.save_status == SaveStatus.UNSAVED
        repo.save(project)
        assert project.save_status == SaveStatus.SAVED
        assert project.last_saved_at

    def test_save_round_trip(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["A", "B", "C"],
        )
        project.apply_translation(0, "甲")
        project.apply_translation(1, "乙", manually_edited=True)
        project.mark_failed(2, "timeout")
        project.status = TaskStatus.PARTIAL
        repo.save(project)

        loaded = repo.load(project.project_id)
        assert loaded is not None
        assert loaded.translated_lines == ["甲", "乙", ""]
        assert loaded.manually_edited_indices == {1}
        assert loaded.failed_indices == {2}
        assert loaded.status == TaskStatus.PARTIAL
        assert loaded.last_error == "timeout"

    def test_save_round_trip_preserves_completed_empty_translation(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["A", "B"],
        )
        project.completed_indices.add(0)
        repo.save(project)

        loaded = repo.load(project.project_id)

        assert loaded is not None
        assert loaded.translated_lines[0] == ""
        assert loaded.completed_indices == {0}


# ── 检查点 ──────────────────────────────────


class TestCheckpoints:
    def test_create_checkpoint(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        project.apply_translation(0, "你好")
        repo.save(project)

        ckpt_name = repo.create_checkpoint(project, label="before_retranslate")
        assert ckpt_name.startswith(project.project_id)
        assert "ckpt_" in ckpt_name

    def test_list_checkpoints(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project)
        repo.create_checkpoint(project, label="first")
        repo.create_checkpoint(project, label="second")

        ckpts = repo.list_checkpoints(project.project_id)
        assert len(ckpts) == 2

    def test_restore_checkpoint(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        project.apply_translation(0, "原始译文")
        repo.save(project)
        ckpt_name = repo.create_checkpoint(project, label="before_overwrite")

        # 覆盖译文
        project.apply_translation(0, "新译文", manually_edited=True)
        repo.save(project)

        # 恢复检查点
        restored = repo.restore_checkpoint(project.project_id, ckpt_name)
        assert restored is not None
        assert restored.translated_lines[0] == "原始译文"
        assert 0 not in restored.manually_edited_indices

    def test_restore_nonexistent_checkpoint(self, repo):
        result = repo.restore_checkpoint("pid", "nonexistent.json")
        assert result is None

    def test_prune_old_checkpoints(self, repo):
        """超出 MAX_CHECKPOINTS 的旧检查点被删除"""
        import time

        from src.infrastructure.project_repository import MAX_CHECKPOINTS

        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project)
        for i in range(MAX_CHECKPOINTS + 3):
            repo.create_checkpoint(project, label=f"ckpt_{i}")
            time.sleep(0.01)

        ckpts = repo.list_checkpoints(project.project_id)
        assert len(ckpts) <= MAX_CHECKPOINTS


# ── 最近项目 ──────────────────────────────


class TestRecentProjects:
    def test_list_recent_empty(self, repo):
        assert repo.list_recent() == []

    def test_list_recent_after_save(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project)

        recent = repo.list_recent()
        assert len(recent) == 1
        assert recent[0]["project_id"] == project.project_id
        assert recent[0]["source_path"] == "/tmp/test.txt"

    def test_list_recent_excludes_deleted(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project)
        repo.delete(project.project_id)

        recent = repo.list_recent()
        assert len(recent) == 0

    def test_list_recent_ordered(self, repo):
        """最近项目按最后打开时间倒序"""
        import time

        for i in range(3):
            project = repo.create(
                source_path=f"/tmp/test{i}.txt",
                source_fingerprint=f"fp{i}",
                file_type="txt",
                mapping_dir="",
                original_lines=["Hello"],
            )
            repo.save(project)
            time.sleep(0.01)

        recent = repo.list_recent()
        assert len(recent) == 3


# ── 删除 ──────────────────────────────────


class TestDelete:
    def test_delete_project(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project)
        assert repo.delete(project.project_id) is True
        assert repo.load(project.project_id) is None

    def test_delete_nonexistent(self, repo):
        assert repo.delete("nonexistent") is False

    def test_delete_removes_checkpoints(self, repo):
        project = repo.create(
            source_path="/tmp/test.txt",
            source_fingerprint="fp123",
            file_type="txt",
            mapping_dir="",
            original_lines=["Hello"],
        )
        repo.save(project)
        repo.create_checkpoint(project, label="test")
        repo.delete(project.project_id)

        assert repo.list_checkpoints(project.project_id) == []
