#!/usr/bin/env python3
"""STORAGE-6：BackupRepository 译文备份仓库测试。

对应指导文档 §7 / §9“运行行为”：创建、恢复、清理备份都使用新备份目录；
备份数量或总大小超限时按时间清理最旧备份。
"""

import json
import time

import pytest

from src.infrastructure.backup_repository import (
    BackupError,
    BackupRepository,
)

PID = "0123456789abcdef"
PID2 = "fedcba9876543210"


@pytest.fixture()
def repo(tmp_path):
    return BackupRepository(tmp_path / "backups", max_per_project=3)


def _snapshot(tag="v1"):
    return {"project_id": PID, "translated_lines": [f"译文-{tag}"]}


class TestCreateAndRead:
    def test_create_backup_writes_named_file(self, repo, tmp_path):
        path = repo.create_backup(PID, _snapshot(), reason="manual")
        assert path.is_file()
        assert path.parent == repo.backups_dir
        assert path.name.startswith(f"{PID}.backup_")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["project_id"] == PID
        assert payload["reason"] == "manual"
        assert payload["schema_version"] == 1
        assert payload["state"]["translated_lines"] == ["译文-v1"]

    def test_same_second_backups_do_not_overwrite(self, repo):
        first = repo.create_backup(PID, _snapshot("a"))
        second = repo.create_backup(PID, _snapshot("b"))
        assert first != second
        assert first.is_file() and second.is_file()

    def test_invalid_project_id_rejected(self, repo):
        with pytest.raises(BackupError):
            repo.create_backup("../evil", _snapshot())
        with pytest.raises(BackupError):
            repo.create_backup("not-hex", _snapshot())

    def test_read_backup_roundtrip(self, repo):
        path = repo.create_backup(PID, _snapshot("roundtrip"), source_path="D:/a.txt")
        data = repo.read_backup(path.name)
        assert data["state"]["translated_lines"] == ["译文-roundtrip"]
        assert data["source_path"] == "D:/a.txt"

    def test_read_missing_backup_raises(self, repo):
        with pytest.raises(BackupError):
            repo.read_backup(f"{PID}.backup_20990101_000000.json")

    def test_path_traversal_backup_id_rejected(self, repo):
        with pytest.raises(BackupError):
            repo.read_backup("../..%2fetc")
        with pytest.raises(BackupError):
            repo.read_backup("..\\..\\evil.json")


class TestListAndDelete:
    def test_list_backups_filtered_by_project(self, repo):
        repo.create_backup(PID, _snapshot("1"))
        repo.create_backup(PID2, {"project_id": PID2, "translated_lines": []})
        assert len(repo.list_backups()) == 2
        assert len(repo.list_backups(PID)) == 1
        assert repo.list_backups(PID)[0].project_id == PID

    def test_list_backups_sorted_desc(self, repo):
        first = repo.create_backup(PID, _snapshot("old"))
        time.sleep(1.1)  # created_at 秒精度，跨秒保证顺序
        second = repo.create_backup(PID, _snapshot("new"))
        names = [info.backup_id for info in repo.list_backups(PID)]
        assert names[0] == second.name
        assert names[1] == first.name

    def test_delete_backup(self, repo):
        path = repo.create_backup(PID, _snapshot())
        assert repo.delete_backup(path.name) is True
        assert not path.exists()
        assert repo.delete_backup(path.name) is False  # 幂等失败

    def test_delete_rejects_traversal(self, repo):
        assert repo.delete_backup("../x.json") is False

    def test_backup_info_fields(self, repo):
        path = repo.create_backup(
            PID, _snapshot(), reason="before_retranslate", source_path="D:/book.txt"
        )
        info = repo.list_backups(PID)[0]
        assert info.backup_id == path.name
        assert info.reason == "before_retranslate"
        assert info.source_path == "D:/book.txt"
        assert info.size_bytes > 0
        assert info.schema_version == 1


class TestLimits:
    def test_prunes_oldest_beyond_per_project_limit(self, repo):
        paths = [repo.create_backup(PID, _snapshot(str(i))) for i in range(4)]
        backups = repo.list_backups(PID)
        assert len(backups) == 3  # max_per_project=3
        assert not paths[0].exists()  # 最旧的被清理
        for p in paths[1:]:
            assert p.exists()

    def test_total_size_limit_prunes_oldest(self, tmp_path):
        small_repo = BackupRepository(
            tmp_path / "backups", max_per_project=100, max_total_bytes=1
        )
        small_repo.create_backup(PID, _snapshot("x" * 100))
        small_repo.create_backup(PID2, {"project_id": PID2, "translated_lines": ["y" * 100]})
        total = sum(info.size_bytes for info in small_repo.list_backups())
        assert total <= 1 or len(small_repo.list_backups()) <= 1

    def test_unparseable_files_skipped_in_listing(self, repo):
        (repo.backups_dir / f"{PID}.backup_20260724_100000.json").write_text(
            "not-json", encoding="utf-8"
        )
        repo.create_backup(PID, _snapshot())
        assert len(repo.list_backups(PID)) == 1
