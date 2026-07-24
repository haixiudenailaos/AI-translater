#!/usr/bin/env python3
"""STORAGE-5：StorageMigrator 数据迁移测试。

对应指导文档 §9“数据迁移”测试清单：
- 空的新目录可以成功迁移
- 已有同名文件时不会静默覆盖
- 复制中断后可重试，旧数据仍可用
- 迁移完成后 TXT 项目、checkpoint、EPUB mapping 均能恢复
- 迁移过程中关闭程序后，下一次启动可以识别未完成迁移
"""

import json

import pytest

from src.infrastructure.storage_migration import (
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_STAGING,
    MigrationConflictError,
    MigrationError,
    StorageMigrator,
    dir_has_data,
    read_interrupted_migration,
)
from src.infrastructure.storage_paths import StoragePathResolver


@pytest.fixture()
def resolver(tmp_app_paths):
    return StoragePathResolver(tmp_app_paths)


@pytest.fixture()
def migrator():
    return StorageMigrator()


def _storage(**overrides):
    from src.config.storage_config import DEFAULT_STORAGE_CONFIG

    cfg = dict(DEFAULT_STORAGE_CONFIG)
    cfg.update(overrides)
    return cfg


def _seed_old_data(paths):
    """在旧目录中制造 TXT 项目、checkpoint 和 EPUB mapping 数据。"""
    paths.projects_dir.mkdir(parents=True, exist_ok=True)
    (paths.projects_dir / "0123456789abcdef.json").write_text(
        json.dumps({"project_id": "0123456789abcdef"}), encoding="utf-8"
    )
    (paths.projects_dir / "0123456789abcdef.ckpt_20260724_120000.json").write_text(
        json.dumps({"checkpoint": True}), encoding="utf-8"
    )
    (paths.projects_dir / "recent_projects.json").write_text(
        json.dumps({"projects": []}), encoding="utf-8"
    )
    mapping_dir = paths.mappings_dir / "mybook-abc123"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    (mapping_dir / "content_mapping.json").write_text(
        json.dumps({"content_mappings": {"c1": {}}}), encoding="utf-8"
    )
    (mapping_dir / "images.json").write_text(json.dumps({"image_mappings": {}}), encoding="utf-8")


class TestMigrateToEmptyTarget:
    """空的新目录可以成功迁移。"""

    def test_migrates_projects_and_mappings(self, resolver, migrator, tmp_path):
        old_paths = resolver.resolve(_storage())
        _seed_old_data(old_paths)
        new_paths = resolver.resolve(_storage(data_root=str(tmp_path / "new_root")))

        result = migrator.migrate(old_paths, new_paths)

        assert result.copied_file_count == 5
        # 新目录文件齐全
        assert (new_paths.projects_dir / "0123456789abcdef.json").is_file()
        assert (new_paths.projects_dir / "recent_projects.json").is_file()
        assert (new_paths.mappings_dir / "mybook-abc123" / "content_mapping.json").is_file()
        # 旧目录数据保留（不自动删除，§8）
        assert (old_paths.projects_dir / "0123456789abcdef.json").is_file()
        # 清单为 committed
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == STATUS_COMMITTED
        assert manifest["source_root"] == str(old_paths.data_root)
        assert len(manifest["copied_files"]) == 2  # projects + mappings
        # staging 已清理
        assert not (result.manifest_path.parent / "staging").exists()

    def test_no_data_returns_empty_result(self, resolver, migrator, tmp_path):
        old_paths = resolver.resolve(_storage())  # 全新默认目录，无数据
        new_paths = resolver.resolve(_storage(data_root=str(tmp_path / "new_root")))
        result = migrator.migrate(old_paths, new_paths)
        assert result.migrated == []
        assert result.copied_file_count == 0

    def test_same_path_skipped(self, resolver, migrator, tmp_path):
        """仅自定义 backups 时，projects/mappings 同路径应跳过。"""
        old_paths = resolver.resolve(_storage())
        _seed_old_data(old_paths)
        new_paths = resolver.resolve(_storage(translation_backups_dir=str(tmp_path / "backups")))
        plan = migrator.plan(old_paths, new_paths)
        assert [item.field for item in plan] == ["translation_backups_dir"]


class TestConflictProtection:
    """目标已有数据时拒绝静默覆盖。"""

    def test_conflict_raises_and_writes_nothing(self, resolver, migrator, tmp_path):
        old_paths = resolver.resolve(_storage())
        _seed_old_data(old_paths)
        new_paths = resolver.resolve(_storage(data_root=str(tmp_path / "new_root")))
        # 目标已有数据
        new_paths.projects_dir.mkdir(parents=True)
        (new_paths.projects_dir / "existing.json").write_text("{}", encoding="utf-8")

        with pytest.raises(MigrationConflictError):
            migrator.migrate(old_paths, new_paths)

        # 目标数据未被覆盖，且没有生成暂存产物
        assert (new_paths.projects_dir / "existing.json").read_text() == "{}"
        assert not (new_paths.projects_dir / "0123456789abcdef.json").exists()


class TestFailureRecovery:
    """复制失败后可重试，旧数据仍可用；中断迁移可被识别。"""

    def test_copy_failure_marks_manifest_failed(self, resolver, migrator, tmp_path, monkeypatch):
        old_paths = resolver.resolve(_storage())
        _seed_old_data(old_paths)
        new_paths = resolver.resolve(_storage(data_root=str(tmp_path / "new_root")))

        import shutil

        def boom(*args, **kwargs):
            raise OSError("磁盘写入失败（模拟）")

        monkeypatch.setattr(shutil, "copytree", boom)
        with pytest.raises(MigrationError):
            migrator.migrate(old_paths, new_paths)

        # 旧数据仍可用
        assert (old_paths.projects_dir / "0123456789abcdef.json").is_file()
        # 清单标记 failed，可被启动检查识别
        interrupted = read_interrupted_migration(new_paths.data_root)
        assert interrupted is not None
        assert interrupted["status"] == STATUS_FAILED
        assert interrupted["failed_files"]

    def test_staging_manifest_detected_as_interrupted(self, tmp_path):
        """status=staging 的清单（迁移中强杀）可被启动检查识别。"""
        root = tmp_path / "root"
        manifest_dir = root / ".migration"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "migration.json").write_text(
            json.dumps({"schema_version": 1, "status": STATUS_STAGING}),
            encoding="utf-8",
        )
        interrupted = read_interrupted_migration(root)
        assert interrupted is not None
        assert interrupted["status"] == STATUS_STAGING

    def test_committed_manifest_not_interrupted(self, tmp_path):
        root = tmp_path / "root"
        manifest_dir = root / ".migration"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "migration.json").write_text(
            json.dumps({"schema_version": 1, "status": STATUS_COMMITTED}),
            encoding="utf-8",
        )
        assert read_interrupted_migration(root) is None

    def test_missing_manifest_not_interrupted(self, tmp_path):
        assert read_interrupted_migration(tmp_path) is None


class TestDirHasData:
    def test_empty_dir(self, tmp_path):
        assert dir_has_data(tmp_path) is False

    def test_dir_with_file(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        assert dir_has_data(tmp_path) is True

    def test_missing_dir(self, tmp_path):
        assert dir_has_data(tmp_path / "nope") is False

    def test_nested_file(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        (sub / "f.txt").write_text("x", encoding="utf-8")
        assert dir_has_data(tmp_path) is True
