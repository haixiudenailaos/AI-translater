#!/usr/bin/env python3
"""译文备份仓库（STORAGE-6）。

对应 docs/USER_CUSTOMIZABLE_DATA_DIRECTORIES_IMPLEMENTATION_GUIDE.md §7：
备份逻辑集中在独立仓库，不散落在翻译控制器、文件处理器和导出器中。

备份文件命名（§4.3，不使用用户输入的完整文件名拼接路径）::

    <project_id>.backup_YYYYMMDD_HHMMSS.json

写入要求（§7）：
- 使用现有的原子 JSON 写入能力（write_json_atomic：临时文件 + 替换）。
- 备份数量与总大小有上限，超限时按时间清理最旧备份。
- 清理缓存不影响备份；备份目录由 ResolvedStoragePaths.translation_backups_dir
  统一注入。
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from ..utils.file_handler import write_json_atomic

logger = logging.getLogger(__name__)

BACKUP_SCHEMA_VERSION = 1
#: 每个项目保留的最大备份数（§12：本版本决策值，后续可做成设置项）
DEFAULT_MAX_PER_PROJECT = 10
#: 备份总大小上限，超出时按时间清理最旧备份
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024

_BACKUP_NAME_PATTERN = re.compile(r"^[0-9a-f]{16}\.backup_\d{8}_\d{6}(_\d+)?\.json$")
_PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


class BackupError(RuntimeError):
    """备份写入/读取失败（调用方保留主记录 dirty 状态）。"""


@dataclass(frozen=True)
class BackupInfo:
    """单个备份的元信息。"""

    backup_id: str  # 备份文件名（即备份 ID）
    project_id: str
    path: Path
    created_at: str
    reason: str
    size_bytes: int
    source_path: str
    schema_version: int


class BackupRepository:
    """译文备份仓库。

    Args:
        backups_dir: 备份根目录（ResolvedStoragePaths.translation_backups_dir）。
        max_per_project: 每个项目保留的最大备份数。
        max_total_bytes: 全部备份的总大小上限。
    """

    def __init__(
        self,
        backups_dir: Path,
        *,
        max_per_project: int = DEFAULT_MAX_PER_PROJECT,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ):
        self.backups_dir = Path(backups_dir).resolve()
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.max_per_project = max(1, int(max_per_project))
        self.max_total_bytes = max(0, int(max_total_bytes))

    # ── 创建 ────────────────────────────────────────

    def create_backup(
        self,
        project_id: str,
        snapshot: Dict,
        *,
        reason: str = "",
        source_path: str = "",
        source_fingerprint: str = "",
    ) -> Path:
        """创建一次完整译文状态快照备份，返回备份文件路径。

        Args:
            project_id: 项目 ID（16 位十六进制，与 ProjectRepository 一致）。
            snapshot: 完整译文状态（通常为 ``TranslationProject.to_dict()``）。
            reason: 备份原因（如 ``manual`` / ``before_retranslate``）。
            source_path: 来源文件路径（元信息，便于恢复时定位）。
            source_fingerprint: 源文件指纹（元信息）。

        Raises:
            BackupError: 项目 ID 非法或写入失败。主记录的 dirty 状态由
                调用方保留（本方法不修改传入 snapshot）。
        """
        if not _PROJECT_ID_PATTERN.fullmatch(project_id or ""):
            raise BackupError(f"非法项目 ID: {project_id!r}")

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{project_id}.backup_{timestamp}.json"
        path = self._safe_direct_child(name)
        # 同一秒内的重复备份：追加序号避免覆盖
        counter = 1
        while path.exists():
            name = f"{project_id}.backup_{timestamp}_{counter}.json"
            path = self._safe_direct_child(name)
            counter += 1

        payload = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "project_id": project_id,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "source_path": source_path,
            "source_fingerprint": source_fingerprint,
            "state": snapshot,
        }
        try:
            write_json_atomic(path, payload)
        except OSError as exc:
            raise BackupError(f"备份写入失败 {path}: {exc}") from exc

        logger.info("已创建译文备份: %s（reason=%s）", name, reason)
        self._enforce_limits(project_id)
        return path

    # ── 查询 ────────────────────────────────────────

    def list_backups(self, project_id: str | None = None) -> List[BackupInfo]:
        """列出备份（按创建时间倒序）。project_id 为 None 时列出全部。"""
        results: List[BackupInfo] = []
        for path in self.backups_dir.glob("*.backup_*.json"):
            if not self._is_safe_direct_child(path):
                continue
            info = self._read_info(path)
            if info is None:
                continue
            if project_id is not None and info.project_id != project_id:
                continue
            results.append(info)
        # created_at 为秒精度，同秒内的多个备份以文件名（含时间戳+序号，
        # 字典序即创建顺序）作为稳定次序键。
        results.sort(key=lambda item: (item.created_at, item.backup_id), reverse=True)
        return results

    def read_backup(self, backup_id: str) -> Dict:
        """读取备份完整内容（含 state 快照），供恢复使用。"""
        path = self._resolve_backup_id(backup_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError(f"读取备份失败 {backup_id}: {exc}") from exc

    def delete_backup(self, backup_id: str) -> bool:
        """删除指定备份。返回是否删除成功。"""
        try:
            path = self._resolve_backup_id(backup_id)
        except BackupError:
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning("删除备份失败 %s: %s", backup_id, exc)
            return False

    # ── 限额清理 ────────────────────────────────────

    def _enforce_limits(self, project_id: str) -> None:
        """按数量与总大小上限清理最旧备份。"""
        project_backups = self.list_backups(project_id)
        for old in project_backups[self.max_per_project :]:
            self._delete_quiet(old)

        all_backups = self.list_backups()
        total = sum(item.size_bytes for item in all_backups)
        # all_backups 按时间倒序，从末尾（最旧）开始删
        for old in reversed(all_backups):
            if total <= self.max_total_bytes:
                break
            if self._delete_quiet(old):
                total -= old.size_bytes

    @staticmethod
    def _delete_quiet(info: BackupInfo) -> bool:
        try:
            info.path.unlink()
            logger.info("已清理超限备份: %s", info.backup_id)
            return True
        except OSError as exc:
            logger.warning("清理备份失败 %s: %s", info.backup_id, exc)
            return False

    # ── 内部工具 ────────────────────────────────────

    def _read_info(self, path: Path) -> BackupInfo | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            size = path.stat().st_size
        except (OSError, ValueError) as exc:
            logger.warning("跳过无法解析的备份文件 %s: %s", path.name, exc)
            return None
        if not isinstance(data, dict):
            return None
        return BackupInfo(
            backup_id=path.name,
            project_id=str(data.get("project_id", "")),
            path=path,
            created_at=str(data.get("created_at", "")),
            reason=str(data.get("reason", "")),
            size_bytes=size,
            source_path=str(data.get("source_path", "")),
            schema_version=int(data.get("schema_version", 0) or 0),
        )

    def _resolve_backup_id(self, backup_id: str) -> Path:
        """把备份 ID 解析为安全的直接子路径（防路径穿越）。"""
        if not isinstance(backup_id, str) or not _BACKUP_NAME_PATTERN.fullmatch(backup_id):
            raise BackupError(f"非法备份 ID: {backup_id!r}")
        path = self._safe_direct_child(backup_id)
        if not path.exists():
            raise BackupError(f"备份不存在: {backup_id}")
        return path

    def _safe_direct_child(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise BackupError(f"非法备份文件名: {name!r}")
        path = (self.backups_dir / name).resolve()
        if path.parent != self.backups_dir:
            raise BackupError(f"备份文件越出根目录: {name!r}")
        return path

    def _is_safe_direct_child(self, path: Path) -> bool:
        try:
            return Path(path).resolve().parent == self.backups_dir
        except OSError:
            return False
