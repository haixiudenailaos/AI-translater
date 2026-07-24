#!/usr/bin/env python3
"""数据目录切换迁移（STORAGE-5）。

对应 docs/USER_CUSTOMIZABLE_DATA_DIRECTORIES_IMPLEMENTATION_GUIDE.md §5 第五步：
采用“复制后校验，再切换”的方式，而不是先移动后尝试恢复。

迁移目录结构::

    新数据根目录/.migration/
    ├── migration.json   # 迁移清单（staging → committed / failed）
    └── staging/         # 复制暂存区，校验通过后才发布到最终位置

关键保证：
- 配置只有在本模块报告全部关键数据迁移完成后才允许写成新路径（由调用方
  ``StorageSettingsService.apply`` 保证顺序）。
- 复制中断、校验失败或发布失败时，旧目录与旧配置保持可用；清单记录
  ``failed`` 状态和失败文件，允许用户排查后重试。
- 程序启动时发现 ``staging`` / ``failed`` 状态的清单，可通过
  :func:`read_interrupted_migration` 识别并提示用户，不把新目录当作
  完整数据目录。
- 目标目录已有数据时拒绝静默覆盖（抛出 :class:`MigrationConflictError`）。

迁移范围：TXT 项目（projects）、EPUB 映射（mappings）、译文备份
（translation_backups）。缓存可重建、日志含历史上下文，均不迁移。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

MIGRATION_DIR_NAME = ".migration"
MIGRATION_MANIFEST_NAME = "migration.json"
MIGRATION_SCHEMA_VERSION = 1

# 清单状态：staging（进行中/中断）→ committed（完成）或 failed（失败可重试）
STATUS_STAGING = "staging"
STATUS_COMMITTED = "committed"
STATUS_FAILED = "failed"


class MigrationError(RuntimeError):
    """迁移执行失败（旧数据与旧配置保持可用）。"""


class MigrationConflictError(MigrationError):
    """目标目录已有数据，拒绝静默覆盖。"""


@dataclass(frozen=True)
class DirectoryMigration:
    """单个目录的迁移计划项。"""

    field: str
    source: Path
    target: Path


@dataclass
class MigrationResult:
    """迁移执行结果。"""

    manifest_path: Path
    migrated: List[Tuple[str, int]] = field(default_factory=list)  # (field, 文件数)
    skipped_same_path: List[str] = field(default_factory=list)
    skipped_empty: List[str] = field(default_factory=list)

    @property
    def copied_file_count(self) -> int:
        return sum(count for _, count in self.migrated)


def dir_has_data(path: Path) -> bool:
    """目录存在且包含任意文件（递归）时返回 True。"""
    try:
        if not path.is_dir():
            return False
        for _root, _dirs, files in os.walk(path):
            if files:
                return True
    except OSError:
        return False
    return False


def read_interrupted_migration(data_root: Path) -> Dict | None:
    """读取未完成的迁移清单（staging / failed 状态）。

    启动时调用：返回 None 表示无中断迁移；返回 dict 表示存在中断迁移，
    调用方应提示用户，不要把新目录当作完整数据目录。
    """
    manifest = Path(data_root) / MIGRATION_DIR_NAME / MIGRATION_MANIFEST_NAME
    try:
        if not manifest.is_file():
            return None
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取迁移清单失败 %s: %s", manifest, exc)
        return None
    if isinstance(data, dict) and data.get("status") in (STATUS_STAGING, STATUS_FAILED):
        return data
    return None


class StorageMigrator:
    """把旧数据目录的内容安全复制到新数据目录。"""

    def plan(
        self,
        old_paths,
        new_paths,
    ) -> List[DirectoryMigration]:
        """生成迁移计划（projects / mappings / backups，跳过同路径项）。

        按叶子目录逐一迁移，而不是整体复制 data_root：默认模式下
        translation_records_dir 等于 data_dir，整体复制会把配置文件
        带到用户选择的普通数据目录（§8：不得复制配置与凭据）。
        """
        pairs = [
            ("projects_dir", Path(old_paths.projects_dir), Path(new_paths.projects_dir)),
            ("mappings_dir", Path(old_paths.mappings_dir), Path(new_paths.mappings_dir)),
            (
                "translation_backups_dir",
                Path(old_paths.translation_backups_dir),
                Path(new_paths.translation_backups_dir),
            ),
        ]
        plan: List[DirectoryMigration] = []
        for field_name, source, target in pairs:
            if os.path.normcase(str(source)) == os.path.normcase(str(target)):
                continue
            plan.append(DirectoryMigration(field=field_name, source=source, target=target))
        return plan

    def has_migratable_data(self, old_paths, new_paths) -> bool:
        """旧目录中是否存在需要迁移的数据。"""
        return any(dir_has_data(item.source) for item in self.plan(old_paths, new_paths))

    def check_conflicts(self, plan: List[DirectoryMigration]) -> List[DirectoryMigration]:
        """返回目标已有数据、与源不一致的计划项（调用方应取消或询问用户）。"""
        conflicts: List[DirectoryMigration] = []
        for item in plan:
            if dir_has_data(item.source) and dir_has_data(item.target):
                conflicts.append(item)
        return conflicts

    def migrate(
        self,
        old_paths,
        new_paths,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> MigrationResult:
        """执行迁移：暂存复制 → 校验 → 发布 → 写清单。

        Raises:
            MigrationConflictError: 目标目录已有数据（未做任何写入）。
            MigrationError: 复制、校验或发布失败（已发布项保持完整，
                未完成项留在 staging，清单标记 failed 可重试）。
        """
        plan = self.plan(old_paths, new_paths)
        migration_root = Path(new_paths.data_root) / MIGRATION_DIR_NAME
        staging_root = migration_root / "staging"
        manifest_path = migration_root / MIGRATION_MANIFEST_NAME

        conflicts = self.check_conflicts(plan)
        if conflicts:
            details = "、".join(f"{c.field}（{c.target}）" for c in conflicts)
            raise MigrationConflictError(
                f"新目录中已存在数据，为避免覆盖已取消迁移: {details}"
            )

        result = MigrationResult(manifest_path=manifest_path)
        active: List[DirectoryMigration] = []
        for item in plan:
            if not dir_has_data(item.source):
                result.skipped_empty.append(item.field)
                continue
            active.append(item)

        if not active:
            logger.info("没有需要迁移的数据")
            return result

        manifest = self._new_manifest(old_paths, new_paths, active)
        try:
            staging_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MigrationError(f"无法创建迁移暂存目录 {staging_root}: {exc}") from exc
        self._write_manifest(manifest_path, manifest)

        try:
            for item in active:
                self._report(progress_callback, f"正在复制 {item.field} ...")
                staged_copy = staging_root / item.field
                if staged_copy.exists():
                    shutil.rmtree(staged_copy)
                file_count = self._copy_and_verify(item.source, staged_copy)

                self._report(progress_callback, f"正在发布 {item.field} ...")
                self._publish(staged_copy, item.target)

                result.migrated.append((item.field, file_count))
                manifest["copied_files"].append(
                    {"field": item.field, "files": file_count, "target": str(item.target)}
                )
                self._write_manifest(manifest_path, manifest)
        except MigrationError as exc:
            manifest["status"] = STATUS_FAILED
            manifest["failed_files"].append({"field": item.field, "error": str(exc)})
            self._write_manifest(manifest_path, manifest)
            raise
        except (OSError, shutil.Error) as exc:
            manifest["status"] = STATUS_FAILED
            manifest["failed_files"].append({"field": item.field, "error": str(exc)})
            self._write_manifest(manifest_path, manifest)
            raise MigrationError(f"迁移 {item.field} 失败: {exc}") from exc

        manifest["status"] = STATUS_COMMITTED
        manifest["committed_at"] = _now_iso()
        self._write_manifest(manifest_path, manifest)
        try:
            shutil.rmtree(staging_root, ignore_errors=True)
        except OSError:
            pass
        logger.info(
            "数据迁移完成: %d 个目录, %d 个文件",
            len(result.migrated),
            result.copied_file_count,
        )
        return result

    # ── 内部步骤 ────────────────────────────────────

    @staticmethod
    def _copy_and_verify(source: Path, staged_copy: Path) -> int:
        """复制 source → staged_copy，并校验文件数量与每个文件大小一致。"""
        try:
            shutil.copytree(source, staged_copy, symlinks=False)
        except (OSError, shutil.Error) as exc:
            raise MigrationError(f"复制失败 {source} → {staged_copy}: {exc}") from exc

        source_files = _collect_file_stats(source)
        staged_files = _collect_file_stats(staged_copy)
        if len(source_files) != len(staged_files):
            raise MigrationError(
                f"复制校验失败 {source}: 文件数不一致 "
                f"（源 {len(source_files)} / 暂存 {len(staged_files)}）"
            )
        for rel_path, size in source_files.items():
            staged_size = staged_files.get(rel_path)
            if staged_size is None or staged_size != size:
                raise MigrationError(
                    f"复制校验失败 {source}: 文件 {rel_path} 大小不一致"
                )
        return len(source_files)

    @staticmethod
    def _publish(staged_copy: Path, target: Path) -> None:
        """把校验通过的暂存副本发布到最终位置（目标必须为空或不存在）。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # 目标为迁移开始前已确认无数据的目录；此处仅允许空目录残留
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            else:
                raise MigrationError(f"发布目标已存在且非空: {target}")
        try:
            staged_copy.replace(target)
        except OSError:
            # 跨磁盘移动退化为 copy + remove
            shutil.move(str(staged_copy), str(target))

    @staticmethod
    def _new_manifest(old_paths, new_paths, active: List[DirectoryMigration]) -> Dict:
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "source_root": str(old_paths.data_root),
            "target_root": str(new_paths.data_root),
            "status": STATUS_STAGING,
            "planned": [
                {"field": item.field, "source": str(item.source), "target": str(item.target)}
                for item in active
            ],
            "copied_files": [],
            "failed_files": [],
            "created_at": _now_iso(),
        }

    @staticmethod
    def _write_manifest(path: Path, manifest: Dict) -> None:
        """清单写入尽力而为：清单损坏不阻断迁移主流程，但必须记录日志。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning("写入迁移清单失败 %s: %s", path, exc)

    @staticmethod
    def _report(callback: Callable[[str], None] | None, message: str) -> None:
        logger.info("%s", message)
        if callback is not None:
            try:
                callback(message)
            except Exception:  # noqa: BLE001 - 进度回调不得中断迁移
                pass


def _collect_file_stats(root: Path) -> Dict[str, int]:
    """收集 root 下所有文件的 {相对路径: 大小}。"""
    stats: Dict[str, int] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = Path(dirpath) / name
            try:
                rel = full.relative_to(root).as_posix()
                stats[rel] = full.stat().st_size
            except (OSError, ValueError):
                continue
    return stats


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")
