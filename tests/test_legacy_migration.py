#!/usr/bin/env python3
"""
R2-BUG-020：迁移旧打包版实际配置目录

验证：
- 从 V1.4 用户目录 `~/.轻小说翻译器V1.4/config` 升级后保留应用配置
- 从任意 CWD 启动不会导入该目录下无关 config/*.json
- 部分迁移失败不会写入"已完成"标记（允许重试）
- marker 记录来源、版本和文件列表
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def isolated_app_paths(tmp_path):
    """构造完全隔离的 AppPaths，并将 Path.home() 指向临时目录。"""
    from src.app_paths import AppPaths

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()

    app_paths = AppPaths.create(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        workspace_dir=tmp_path / "workspace",
        log_dir=tmp_path / "logs",
    )

    with patch("src.app_paths.Path.home", return_value=fake_home):
        yield app_paths, fake_home


class TestLegacyMigration:
    """R2-BUG-020：旧打包版配置目录迁移。"""

    def test_migrate_from_old_user_dir(self, isolated_app_paths):
        """从 ~/.轻小说翻译器V1.4/config 迁移用户配置。"""
        app_paths, fake_home = isolated_app_paths

        # 构造旧用户目录及配置
        old_config = fake_home / ".轻小说翻译器V1.4" / "config"
        old_config.mkdir(parents=True)
        (old_config / "api_config.json").write_text(
            json.dumps({"provider": "siliconflow", "model_name": "old-model"}),
            encoding="utf-8",
        )
        (old_config / "glossary.json").write_text(
            json.dumps({"terms": [{"source": "猫", "target": "ねこ"}]}),
            encoding="utf-8",
        )
        # 示例文件不应被迁移
        (old_config / "api_config_sample.json").write_text(
            json.dumps({"api_key": "sample"}),
            encoding="utf-8",
        )

        app_paths.migrate_legacy_config()

        # 用户配置应被迁移
        assert (app_paths.config_dir / "api_config.json").exists()
        assert (app_paths.config_dir / "glossary.json").exists()
        # 示例文件不应被迁移
        assert not (app_paths.config_dir / "api_config_sample.json").exists()

        # marker 应记录 completed 状态和来源
        marker = json.loads(
            (app_paths.config_dir / "migration_state.json").read_text(encoding="utf-8")
        )
        assert marker["status"] == "completed"
        assert ".轻小说翻译器V1.4" in marker["source"]
        assert "api_config.json" in marker["migrated_files"]
        assert "api_config_sample.json" not in marker["migrated_files"]

    def test_does_not_scan_cwd_config(self, isolated_app_paths, tmp_path, monkeypatch):
        """从任意 CWD 启动不会导入该目录下无关 config/*.json。"""
        app_paths, fake_home = isolated_app_paths

        # 模拟打包环境，排除开发资源目录下的 config 作为迁移源
        monkeypatch.setattr("src.app_paths._is_frozen", lambda: True)

        # 在 CWD 下放一个无关的 config 目录
        unrelated_cwd = tmp_path / "unrelated_run_dir"
        unrelated_cwd.mkdir()
        (unrelated_cwd / "config").mkdir()
        (unrelated_cwd / "config" / "unrelated.json").write_text(
            json.dumps({"should_not": "migrate"}),
            encoding="utf-8",
        )
        monkeypatch.chdir(unrelated_cwd)

        # 不构造旧用户目录，确保唯一可能的源是 CWD/config
        app_paths.migrate_legacy_config()

        # CWD 下的无关配置不应被迁移
        assert not (app_paths.config_dir / "unrelated.json").exists()

        # marker 应为 no_source
        marker = json.loads(
            (app_paths.config_dir / "migration_state.json").read_text(encoding="utf-8")
        )
        assert marker["status"] == "no_source"

    def test_partial_failure_allows_retry(self, isolated_app_paths):
        """部分迁移失败时标记 partial，允许重试。"""
        app_paths, fake_home = isolated_app_paths

        old_config = fake_home / ".轻小说翻译器V1.4" / "config"
        old_config.mkdir(parents=True)
        (old_config / "good.json").write_text(
            json.dumps({"ok": True}),
            encoding="utf-8",
        )
        (old_config / "bad.json").write_text(
            json.dumps({"bad": True}),
            encoding="utf-8",
        )

        # 模拟复制 bad.json 时失败
        import shutil as _shutil

        real_copy2 = _shutil.copy2

        def flaky_copy2(src, dst, *, follow_symlinks=True):
            if Path(src).name == "bad.json":
                raise PermissionError("simulated failure")
            return real_copy2(src, dst, follow_symlinks=follow_symlinks)

        with patch("src.app_paths.shutil.copy2", side_effect=flaky_copy2):
            app_paths.migrate_legacy_config()

        # good.json 应被迁移
        assert (app_paths.config_dir / "good.json").exists()
        # bad.json 应在 failed 列表
        marker = json.loads(
            (app_paths.config_dir / "migration_state.json").read_text(encoding="utf-8")
        )
        assert marker["status"] == "partial"
        assert "bad.json" in marker["failed_files"]
        assert "good.json" in marker["migrated_files"]

    def test_completed_marker_not_re_migrated(self, isolated_app_paths):
        """已完成的 marker 不会重复迁移。"""
        app_paths, fake_home = isolated_app_paths

        old_config = fake_home / ".轻小说翻译器V1.4" / "config"
        old_config.mkdir(parents=True)
        (old_config / "api_config.json").write_text(
            json.dumps({"v": 1}),
            encoding="utf-8",
        )

        # 第一次迁移
        app_paths.migrate_legacy_config()
        marker_path = app_paths.config_dir / "migration_state.json"
        first_marker = marker_path.read_text(encoding="utf-8")

        # 删除旧源，确保第二次不会找到源
        import shutil

        shutil.rmtree(old_config)

        # 第二次调用应直接跳过
        app_paths.migrate_legacy_config()
        second_marker = marker_path.read_text(encoding="utf-8")

        assert first_marker == second_marker

    def test_does_not_overwrite_existing(self, isolated_app_paths):
        """不覆盖新目录中已存在的文件。"""
        app_paths, fake_home = isolated_app_paths

        old_config = fake_home / ".轻小说翻译器V1.4" / "config"
        old_config.mkdir(parents=True)
        (old_config / "api_config.json").write_text(
            json.dumps({"old": True}),
            encoding="utf-8",
        )

        # 新目录已有同名文件
        (app_paths.config_dir / "api_config.json").write_text(
            json.dumps({"new": True}),
            encoding="utf-8",
        )

        app_paths.migrate_legacy_config()

        # 新文件不应被覆盖
        data = json.loads((app_paths.config_dir / "api_config.json").read_text(encoding="utf-8"))
        assert data == {"new": True}
