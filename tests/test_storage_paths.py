#!/usr/bin/env python3
"""STORAGE-2：StoragePathResolver 路径解析、规范化、校验与目录创建测试。

对应 docs/USER_CUSTOMIZABLE_DATA_DIRECTORIES_IMPLEMENTATION_GUIDE.md §9
“路径解析”测试清单：
- 未配置时仍使用平台默认目录
- 自定义 data_root 后，空的子目录配置跟随新根目录
- 显式子目录覆盖 data_root 下的默认子目录
- 相对路径、空路径、路径为文件、无权限路径得到明确错误
- 包含中文和空格的路径可正常工作
- 路径包含 .. 时 resolve 后不越界
"""

import os
import sys

import pytest

from src.config.storage_config import DEFAULT_STORAGE_CONFIG, normalize_storage_config
from src.infrastructure.storage_paths import (
    StoragePathError,
    StoragePathResolver,
)


@pytest.fixture()
def resolver(tmp_app_paths):
    return StoragePathResolver(tmp_app_paths)


def _storage(**overrides):
    cfg = dict(DEFAULT_STORAGE_CONFIG)
    cfg.update(overrides)
    return cfg


class TestDefaultResolution:
    """未配置时使用平台默认目录，且与旧版本布局完全一致。"""

    def test_empty_config_uses_platform_defaults(self, resolver, tmp_app_paths):
        resolved = resolver.resolve({})

        assert resolved.is_default is True
        assert resolved.data_root == tmp_app_paths.data_dir.resolve()
        # 与旧版本布局一致：projects 在 data_dir/projects，
        # mappings 在 workspace_dir/mappings（§8 兼容性）
        assert resolved.projects_dir == (tmp_app_paths.data_dir / "projects").resolve()
        assert resolved.mappings_dir == (tmp_app_paths.workspace_dir / "mappings").resolve()
        assert resolved.logs_dir is None  # 本版本日志不跟随数据根目录

    def test_none_config_uses_defaults(self, resolver, tmp_app_paths):
        resolved = resolver.resolve(None)
        assert resolved.is_default is True
        assert resolved.projects_dir == (tmp_app_paths.data_dir / "projects").resolve()

    def test_default_config_dirs_under_data_dir(self, resolver, tmp_app_paths):
        resolved = resolver.resolve(_storage())
        assert resolved.cache_dir == (tmp_app_paths.data_dir / "cache").resolve()
        assert resolved.translation_backups_dir == (
            tmp_app_paths.data_dir / "translation_backups"
        ).resolve()


class TestDataRootResolution:
    """自定义 data_root 后，空子目录配置跟随新根目录。"""

    def test_subdirs_follow_data_root(self, resolver, tmp_path):
        root = tmp_path / "自定义 数据"
        resolved = resolver.resolve(_storage(data_root=str(root)))

        assert resolved.is_default is False
        assert resolved.data_root == root.resolve()
        assert resolved.cache_dir == (root / "cache").resolve()
        assert resolved.translation_records_dir == (root / "translation_records").resolve()
        assert resolved.translation_backups_dir == (root / "translation_backups").resolve()
        assert resolved.projects_dir == (root / "translation_records" / "projects").resolve()
        assert resolved.mappings_dir == (root / "translation_records" / "mappings").resolve()

    def test_chinese_and_space_path_works(self, resolver, tmp_path):
        root = tmp_path / "米哈游 翻译 数据"
        resolved = resolver.resolve(_storage(data_root=str(root)))
        assert resolved.data_root == root.resolve()
        issues = resolver.validate(resolved)
        assert [i for i in issues if i.is_error] == []

    def test_env_var_and_tilde_expansion(self, resolver, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_DATA", str(tmp_path))
        resolved = resolver.resolve(_storage(data_root="%MY_DATA%/root" if os.name == "nt" else "$MY_DATA/root"))
        assert resolved.data_root == (tmp_path / "root").resolve()

    def test_dotdot_resolved_to_absolute(self, resolver, tmp_path):
        base = tmp_path / "a" / "b"
        base.mkdir(parents=True)
        resolved = resolver.resolve(_storage(data_root=str(base / "..")))
        # resolve() 消除 ..，最终落在 a 目录
        assert resolved.data_root == (tmp_path / "a").resolve()


class TestOverrideResolution:
    """显式子目录覆盖 data_root 默认子目录。"""

    def test_explicit_subdir_overrides_root(self, resolver, tmp_path):
        root = tmp_path / "root"
        fast_disk = tmp_path / "fast_cache"
        resolved = resolver.resolve(
            _storage(data_root=str(root), cache_dir=str(fast_disk))
        )
        assert resolved.cache_dir == fast_disk.resolve()
        # 未覆盖的目录仍跟随 data_root
        assert resolved.translation_records_dir == (root / "translation_records").resolve()

    def test_explicit_records_dir_moves_projects_and_mappings(self, resolver, tmp_path):
        records = tmp_path / "records"
        resolved = resolver.resolve(_storage(translation_records_dir=str(records)))
        assert resolved.translation_records_dir == records.resolve()
        assert resolved.projects_dir == (records / "projects").resolve()
        assert resolved.mappings_dir == (records / "mappings").resolve()

    def test_explicit_logs_dir(self, resolver, tmp_path):
        logs = tmp_path / "my_logs"
        resolved = resolver.resolve(_storage(logs_dir=str(logs)))
        assert resolved.logs_dir == logs.resolve()


class TestNormalizationErrors:
    """相对路径、空路径等必须给出明确错误。"""

    def test_relative_path_rejected(self, resolver):
        with pytest.raises(StoragePathError) as exc_info:
            resolver.resolve(_storage(data_root="relative/path"))
        assert exc_info.value.field == "data_root"
        assert "绝对路径" in str(exc_info.value)

    def test_relative_subdir_rejected(self, resolver):
        with pytest.raises(StoragePathError) as exc_info:
            resolver.resolve(_storage(cache_dir="cache"))
        assert exc_info.value.field == "cache_dir"

    def test_blank_path_treated_as_unset(self, resolver, tmp_app_paths):
        resolved = resolver.resolve(_storage(data_root="   ", cache_dir="  "))
        assert resolved.is_default is True
        assert resolved.data_root == tmp_app_paths.data_dir.resolve()


class TestValidation:
    """路径为文件、目录冲突、嵌套等语义校验。"""

    def test_path_pointing_to_file_is_error(self, resolver, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        resolved = resolver.resolve(_storage(data_root=str(tmp_path), cache_dir=str(blocker)))
        issues = resolver.validate(resolved)
        errors = [i for i in issues if i.is_error]
        assert any(i.field == "cache_dir" and "普通文件" in i.message for i in errors)

    def test_same_dir_for_cache_and_backups_is_error(self, resolver, tmp_path):
        same = tmp_path / "same"
        resolved = resolver.resolve(
            _storage(cache_dir=str(same), translation_backups_dir=str(same))
        )
        issues = resolver.validate(resolved)
        assert any(
            i.is_error and "同一目录" in i.message and i.field == "translation_backups_dir"
            for i in issues
        )

    def test_nested_cache_inside_records_warns(self, resolver, tmp_path):
        root = tmp_path / "root"
        resolved = resolver.resolve(
            _storage(
                data_root=str(root),
                cache_dir=str(root / "translation_records" / "cache"),
            )
        )
        issues = resolver.validate(resolved)
        warnings = [i for i in issues if not i.is_error]
        assert any("嵌套" in i.message for i in warnings)
        # warning 不应阻断：error 列表为空
        assert [i for i in issues if i.is_error] == []

    def test_drive_root_data_root_warns(self, resolver):
        if sys.platform == "win32":
            anchor = os.path.splitdrive(os.getcwd())[0] + "\\"
        else:
            anchor = "/"
        resolved = resolver.resolve(_storage(data_root=anchor))
        issues = resolver.validate(resolved)
        assert any(
            not i.is_error and i.field == "data_root" and "根目录" in i.message
            for i in issues
        )

    def test_valid_custom_dirs_pass(self, resolver, tmp_path):
        resolved = resolver.resolve(_storage(data_root=str(tmp_path / "ok")))
        issues = resolver.validate(resolved)
        assert [i for i in issues if i.is_error] == []


class TestEnsureDirectories:
    """目录创建与失败时的结构化错误。"""

    def test_creates_all_managed_directories(self, resolver, tmp_path):
        root = tmp_path / "new_root"
        resolved = resolver.resolve(_storage(data_root=str(root)))
        resolver.ensure_directories(resolved)

        assert root.is_dir()
        for _, path in resolved.managed_directories():
            assert path.is_dir(), f"{path} 应被创建"

    def test_failure_raises_structured_error(self, resolver, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        resolved = resolver.resolve(
            _storage(data_root=str(tmp_path / "ok"), cache_dir=str(blocker / "sub"))
        )
        with pytest.raises(StoragePathError) as exc_info:
            resolver.ensure_directories(resolved)
        assert exc_info.value.field
        assert exc_info.value.reason

    def test_existing_file_as_dir_fails(self, resolver, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        resolved = resolver.resolve(_storage(cache_dir=str(blocker)))
        with pytest.raises(StoragePathError):
            resolver.ensure_directories(resolved)


class TestStorageConfigNormalization:
    """storage 配置段归一化（STORAGE-1）。"""

    def test_none_returns_defaults(self):
        cfg = normalize_storage_config(None)
        assert cfg == DEFAULT_STORAGE_CONFIG

    def test_non_dict_returns_defaults(self):
        assert normalize_storage_config("bad") == DEFAULT_STORAGE_CONFIG
        assert normalize_storage_config(42) == DEFAULT_STORAGE_CONFIG

    def test_unknown_keys_dropped(self):
        cfg = normalize_storage_config({"data_root": "", "evil_key": "x"})
        assert "evil_key" not in cfg

    def test_non_string_path_fields_reset(self):
        cfg = normalize_storage_config({"data_root": 123, "cache_dir": ["x"]})
        assert cfg["data_root"] == ""
        assert cfg["cache_dir"] == ""

    def test_whitespace_stripped(self, tmp_path):
        cfg = normalize_storage_config({"data_root": f"  {tmp_path}  "})
        assert cfg["data_root"] == str(tmp_path)

    def test_schema_version_type_corrected(self):
        cfg = normalize_storage_config({"schema_version": "1"})
        assert cfg["schema_version"] == 1


class TestConfigManagerStorageSection:
    """ConfigManager 的 storage 段加载与更新（STORAGE-1）。"""

    def test_default_config_contains_storage_section(self, tmp_config_manager):
        storage = tmp_config_manager.get_storage_config()
        assert storage == DEFAULT_STORAGE_CONFIG

    def test_app_config_contains_normalized_storage(self, tmp_config_manager):
        app_config = tmp_config_manager.get_app_config()
        assert app_config["storage"] == DEFAULT_STORAGE_CONFIG

    def test_update_storage_config_persists(self, tmp_config_manager, tmp_path):
        ok = tmp_config_manager.update_storage_config(
            {"data_root": str(tmp_path / "新目录")}
        )
        assert ok is True
        storage = tmp_config_manager.get_storage_config()
        assert storage["data_root"] == str(tmp_path / "新目录")
        # 重新加载后仍然生效（落盘成功）
        reloaded = tmp_config_manager.load_app_config()
        assert reloaded["storage"]["data_root"] == str(tmp_path / "新目录")

    def test_update_storage_config_non_persist(self, tmp_config_manager):
        ok = tmp_config_manager.update_storage_config({"data_root": "D:/x"}, persist=False)
        assert ok is True
        assert tmp_config_manager.get_storage_config()["data_root"] == "D:/x"
        # 未持久化：重新加载（读盘）后应回到默认
        reloaded = tmp_config_manager.load_app_config()
        assert reloaded["storage"]["data_root"] == ""

    def test_update_normalizes_unknown_keys(self, tmp_config_manager):
        tmp_config_manager.update_storage_config({"data_root": "", "junk": 1})
        assert "junk" not in tmp_config_manager.get_storage_config()

    def test_legacy_config_without_storage_loads_defaults(
        self, tmp_config_manager, tmp_path
    ):
        """旧版本 app_config.json 没有 storage 段时自动补默认值。"""
        import json

        config_file = tmp_config_manager.app_config_file
        config_file.write_text(json.dumps({"target_language": "英文"}), encoding="utf-8")
        reloaded = tmp_config_manager.load_app_config()
        assert reloaded["target_language"] == "英文"
        assert reloaded["storage"] == DEFAULT_STORAGE_CONFIG
