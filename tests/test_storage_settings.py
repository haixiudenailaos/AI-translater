#!/usr/bin/env python3
"""STORAGE-4/5：StorageSettingsService 切换流程测试。

对应指导文档 §5 第五步流程与 §9“数据迁移 / 运行行为”清单：
- 校验失败保留旧配置
- 迁移成功后配置才指向新目录
- 迁移失败旧配置继续生效
- 运行中任务禁止切换
- 配置写入失败不报成功
"""

import json

import pytest

from src.application.storage_settings import (
    STATUS_ERROR,
    STATUS_MIGRATED,
    STATUS_SAVED,
    STATUS_UNCHANGED,
    StorageSettingsService,
)
from src.config.storage_config import DEFAULT_STORAGE_CONFIG


@pytest.fixture()
def service(tmp_config_manager, tmp_app_paths):
    return StorageSettingsService(tmp_config_manager, tmp_app_paths)


def _candidate(**overrides):
    cfg = dict(DEFAULT_STORAGE_CONFIG)
    cfg.update(overrides)
    return cfg


def _seed_old_data(paths):
    paths.projects_dir.mkdir(parents=True, exist_ok=True)
    (paths.projects_dir / "0123456789abcdef.json").write_text(
        json.dumps({"project_id": "0123456789abcdef"}), encoding="utf-8"
    )
    mapping_dir = paths.mappings_dir / "book-abc"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    (mapping_dir / "content_mapping.json").write_text("{}", encoding="utf-8")


class TestPreview:
    def test_valid_candidate_resolves(self, service, tmp_path):
        resolved, issues, error = service.preview(_candidate(data_root=str(tmp_path / "root")))
        assert error == ""
        assert resolved is not None
        assert [i for i in issues if i.is_error] == []

    def test_relative_path_returns_error(self, service):
        resolved, _issues, error = service.preview(_candidate(data_root="rel/path"))
        assert resolved is None
        assert "绝对路径" in error


class TestSingleRootCandidate:
    def test_one_root_clears_legacy_overrides(self, service, tmp_config_manager, tmp_path):
        tmp_config_manager.update_storage_config(
            {
                "data_root": str(tmp_path / "old-root"),
                "cache_dir": str(tmp_path / "old-cache"),
                "translation_records_dir": str(tmp_path / "old-records"),
                "translation_backups_dir": str(tmp_path / "old-backups"),
            },
            persist=False,
        )

        selected = tmp_path / "统一缓存"
        candidate = service.single_root_candidate(f"  {selected}  ")

        assert candidate["data_root"] == str(selected)
        assert candidate["cache_dir"] == ""
        assert candidate["translation_records_dir"] == ""
        assert candidate["translation_backups_dir"] == ""

        resolved, issues, error = service.preview(candidate)
        assert error == ""
        assert resolved is not None
        assert [issue for issue in issues if issue.is_error] == []
        assert resolved.cache_dir == (selected / "cache").resolve()
        assert resolved.translation_records_dir == (selected / "translation_records").resolve()
        assert resolved.translation_backups_dir == (selected / "translation_backups").resolve()

    def test_empty_root_restores_platform_defaults(self, service):
        candidate = service.single_root_candidate("   ")

        assert candidate["data_root"] == ""
        assert candidate["cache_dir"] == ""
        assert candidate["translation_records_dir"] == ""
        assert candidate["translation_backups_dir"] == ""
        assert service.resolver.resolve(candidate).is_default is True


class TestApplyUnchanged:
    def test_default_candidate_is_unchanged(self, service):
        result = service.apply(_candidate(), migrate_data=False)
        assert result.status == STATUS_UNCHANGED
        assert result.needs_restart is False


class TestApplyWithoutData:
    def test_saved_and_needs_restart(self, service, tmp_config_manager, tmp_path):
        new_root = tmp_path / "新数据"
        result = service.apply(_candidate(data_root=str(new_root)), migrate_data=False)

        assert result.status == STATUS_SAVED
        assert result.needs_restart is True
        # 配置已持久化
        assert tmp_config_manager.get_storage_config()["data_root"] == str(new_root)
        # 新目录结构已创建
        assert (new_root / "translation_records" / "projects").is_dir()
        assert (new_root / "cache").is_dir()
        assert (new_root / "translation_backups").is_dir()


class TestApplyWithMigration:
    def test_migrates_data_then_switches_config(self, service, tmp_config_manager, tmp_path):
        old_paths = service.current_paths()
        _seed_old_data(old_paths)
        new_root = tmp_path / "new_root"

        result = service.apply(_candidate(data_root=str(new_root)), migrate_data=True)

        assert result.status == STATUS_MIGRATED
        assert result.needs_restart is True
        assert result.migration is not None
        assert result.migration.copied_file_count == 2
        # 新目录有数据
        assert (new_root / "translation_records" / "projects" / "0123456789abcdef.json").is_file()
        assert (
            new_root / "translation_records" / "mappings" / "book-abc" / "content_mapping.json"
        ).is_file()
        # 旧目录数据保留（§8：不自动删除旧目录）
        assert (old_paths.projects_dir / "0123456789abcdef.json").is_file()
        # 配置指向新目录
        assert tmp_config_manager.get_storage_config()["data_root"] == str(new_root)

    def test_keep_old_data_without_migration(self, service, tmp_config_manager, tmp_path):
        old_paths = service.current_paths()
        _seed_old_data(old_paths)
        new_root = tmp_path / "new_root"

        result = service.apply(_candidate(data_root=str(new_root)), migrate_data=False)

        assert result.status == STATUS_SAVED
        # 新目录从空开始
        projects = new_root / "translation_records" / "projects"
        assert projects.is_dir()
        assert list(projects.iterdir()) == []
        # 旧数据未动
        assert (old_paths.projects_dir / "0123456789abcdef.json").is_file()


class TestApplyGuards:
    def test_active_task_blocks_switch(self, tmp_config_manager, tmp_app_paths, tmp_path):
        service = StorageSettingsService(
            tmp_config_manager, tmp_app_paths, is_task_active=lambda: True
        )
        result = service.apply(_candidate(data_root=str(tmp_path / "root")), migrate_data=False)
        assert result.status == STATUS_ERROR
        assert "翻译任务" in result.message
        # 配置未被修改
        assert tmp_config_manager.get_storage_config()["data_root"] == ""

    def test_conflict_keeps_old_config(self, service, tmp_config_manager, tmp_path):
        old_paths = service.current_paths()
        _seed_old_data(old_paths)
        new_root = tmp_path / "new_root"
        # 新目录已有同名数据 → 冲突
        conflict_dir = new_root / "translation_records" / "projects"
        conflict_dir.mkdir(parents=True)
        (conflict_dir / "existing.json").write_text("{}", encoding="utf-8")

        result = service.apply(_candidate(data_root=str(new_root)), migrate_data=True)

        assert result.status == STATUS_ERROR
        assert "已存在数据" in result.message
        assert tmp_config_manager.get_storage_config()["data_root"] == ""

    def test_uncreatable_dir_keeps_old_config(self, service, tmp_config_manager, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        result = service.apply(_candidate(data_root=str(blocker / "sub")), migrate_data=False)
        assert result.status == STATUS_ERROR
        assert tmp_config_manager.get_storage_config()["data_root"] == ""

    def test_validation_error_keeps_old_config(self, service, tmp_config_manager, tmp_path):
        same = tmp_path / "same"
        result = service.apply(
            _candidate(cache_dir=str(same), translation_backups_dir=str(same)),
            migrate_data=False,
        )
        assert result.status == STATUS_ERROR
        assert tmp_config_manager.get_storage_config()["cache_dir"] == ""


class TestPathsChanged:
    def test_unchanged_default(self, service):
        assert service.paths_changed(_candidate()) is False

    def test_changed_with_new_root(self, service, tmp_path):
        assert service.paths_changed(_candidate(data_root=str(tmp_path / "r"))) is True

    def test_same_root_different_format_unchanged(self, service, tmp_path):
        """尾随分隔符等格式差异不应被误判为变化。"""
        root = str(tmp_path / "r")
        service.apply(_candidate(data_root=root), migrate_data=False)
        assert service.paths_changed(_candidate(data_root=root + "/")) is False


class TestSourceHasData:
    def test_no_data(self, service, tmp_path):
        assert service.source_has_data(_candidate(data_root=str(tmp_path / "r"))) is False

    def test_with_data(self, service, tmp_path):
        _seed_old_data(service.current_paths())
        assert service.source_has_data(_candidate(data_root=str(tmp_path / "r"))) is True
