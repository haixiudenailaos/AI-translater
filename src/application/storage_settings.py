#!/usr/bin/env python3
"""数据目录设置用例服务（STORAGE-4/5）。

对应 docs/USER_CUSTOMIZABLE_DATA_DIRECTORIES_IMPLEMENTATION_GUIDE.md §5
第四、五步：把“解析 → 校验 → 迁移 → 保存配置”编排成单个用例，
设置窗口只负责收集输入和展示结果，不直接操作路径与配置。

切换流程（§5 第五步）::

    检查是否有运行中的翻译任务
        ├─ 有：拒绝切换
        └─ 无：继续
    解析并校验新路径
        ├─ 失败：返回 issues，保留旧配置
        └─ 成功：继续
    （可选）执行迁移到暂存区并校验发布
        ├─ 失败：清单标记 failed，旧配置继续生效
        └─ 成功：继续
    保存配置
        ├─ 失败：返回错误（迁移产物保留、旧配置仍生效的是配置值本身，
        │   因配置未写入，重启后仍使用旧目录）
        └─ 成功：返回 needs_restart，提示下次启动生效

本版本策略（§6）：保存后下次启动生效，不做运行时路径切换。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..infrastructure.storage_migration import (
    MigrationConflictError,
    MigrationError,
    MigrationResult,
    StorageMigrator,
)
from ..infrastructure.storage_paths import (
    ResolvedStoragePaths,
    StoragePathError,
    StoragePathIssue,
    StoragePathResolver,
)

logger = logging.getLogger(__name__)

StorageConfig = dict[str, object]


class StorageConfigManager(Protocol):
    """Configuration dependency needed by the storage settings use case."""

    def get_storage_config(self) -> StorageConfig:
        """Return an independent, normalized storage configuration."""
        ...

    def update_storage_config(self, storage: StorageConfig) -> bool:
        """Persist a normalized storage configuration."""
        ...


class StoragePathDefaults(Protocol):
    """Platform paths consumed by the infrastructure resolver."""

    data_dir: Path
    workspace_dir: Path


class StorageMigrationPort(Protocol):
    """Migration operations orchestrated by the storage settings use case."""

    def has_migratable_data(
        self, old_paths: ResolvedStoragePaths, new_paths: ResolvedStoragePaths
    ) -> bool:
        """Whether changing the configured paths requires migration."""
        ...

    def migrate(
        self,
        old_paths: ResolvedStoragePaths,
        new_paths: ResolvedStoragePaths,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> MigrationResult:
        """Migrate application-owned data or raise a migration error."""
        ...


def _empty_storage_issues() -> list[StoragePathIssue]:
    return []


# ApplyResult.status 取值
STATUS_UNCHANGED = "unchanged"
STATUS_SAVED = "saved"
STATUS_MIGRATED = "migrated"
STATUS_ERROR = "error"


@dataclass
class StorageApplyResult:
    """应用新数据目录配置的结果。"""

    status: str
    message: str = ""
    issues: list[StoragePathIssue] = field(default_factory=_empty_storage_issues)
    migration: MigrationResult | None = None
    #: True 表示需要重启应用才能生效（本版本固定策略）
    needs_restart: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_UNCHANGED, STATUS_SAVED, STATUS_MIGRATED)


class StorageSettingsService:
    """数据与存储设置页的后端用例。

    Args:
        config_manager: 应用配置管理器（读写 storage 段）。
        app_paths: 平台默认路径对象（解析 fallback 与“恢复默认”依据）。
        is_task_active: 可选回调，返回当前是否有未完结的翻译任务；
            未提供时按“无任务”处理（测试与无队列环境）。
    """

    def __init__(
        self,
        config_manager: StorageConfigManager,
        app_paths: StoragePathDefaults,
        *,
        is_task_active: Callable[[], bool] | None = None,
    ) -> None:
        self._config_manager = config_manager
        self._app_paths = app_paths
        self._resolver = StoragePathResolver(app_paths)
        self._migrator: StorageMigrationPort = StorageMigrator()
        self._is_task_active = is_task_active

    # ── 查询 ────────────────────────────────────────

    @property
    def resolver(self) -> StoragePathResolver:
        return self._resolver

    def current_storage(self) -> StorageConfig:
        """当前生效的 storage 配置段（归一化后）。"""
        return self._config_manager.get_storage_config()

    def current_paths(self) -> ResolvedStoragePaths:
        """当前生效配置对应的解析路径。"""
        return self._resolver.resolve(self.current_storage())

    def default_storage(self) -> StorageConfig:
        """平台默认配置（全部为空 = 跟随平台默认目录）。"""
        from ..config.storage_config import DEFAULT_STORAGE_CONFIG

        return dict(DEFAULT_STORAGE_CONFIG)

    def single_root_candidate(self, root: str) -> StorageConfig:
        """生成设置页使用的“单一缓存根目录”配置。

        新界面只让用户选择一个根目录。缓存、翻译中间记录和译文备份
        都由 :class:`StoragePathResolver` 在该目录下自动派生。旧版本允许
        分别覆盖三个子目录；这里主动清空这些覆盖值，使用户新选择的
        根目录成为唯一入口，同时仍保留底层对旧配置的读取兼容。
        """
        candidate = self.current_storage()
        candidate["data_root"] = root.strip()
        for path_field in (
            "cache_dir",
            "translation_records_dir",
            "translation_backups_dir",
        ):
            candidate[path_field] = ""
        return candidate

    # ── 预览与校验 ──────────────────────────────────

    def preview(
        self, candidate: StorageConfig
    ) -> tuple[ResolvedStoragePaths | None, list[StoragePathIssue], str]:
        """解析并校验候选配置，供设置页实时预览。

        Returns:
            (解析结果 or None, issue 列表, 解析级错误消息 or "")。
            解析级错误（如相对路径）时解析结果为 None。
        """
        try:
            resolved = self._resolver.resolve(candidate)
        except StoragePathError as exc:
            return None, [], str(exc)
        issues = self._resolver.validate(resolved)
        return resolved, issues, ""

    def paths_changed(self, candidate: StorageConfig) -> bool:
        """候选配置解析后的写入目录是否与当前生效目录不同。"""
        try:
            new_paths = self._resolver.resolve(candidate)
        except StoragePathError:
            return True  # 无法解析交给 apply/preview 报错
        current = self.current_paths()
        return _paths_signature(new_paths) != _paths_signature(current)

    def source_has_data(self, candidate: StorageConfig) -> bool:
        """切换到候选配置时，旧目录中是否存在可迁移数据。"""
        try:
            new_paths = self._resolver.resolve(candidate)
        except StoragePathError:
            return False
        return self._migrator.has_migratable_data(self.current_paths(), new_paths)

    # ── 应用 ────────────────────────────────────────

    def apply(
        self,
        candidate: StorageConfig,
        *,
        migrate_data: bool,
        progress_callback: Callable[[str], None] | None = None,
    ) -> StorageApplyResult:
        """校验并保存新数据目录配置，可选先迁移旧数据。

        Args:
            candidate: 用户编辑后的 storage 配置段。
            migrate_data: True 时先把旧目录数据复制到新目录（复制后校验
                再发布）；False 表示保留原位置不动。
            progress_callback: 迁移进度回调（参数为中文描述）。
        """
        # 1. 运行中任务守卫（§1：翻译进行中不允许直接切换正在使用的目录）
        if self.paths_changed(candidate) and self._task_active():
            return StorageApplyResult(
                status=STATUS_ERROR,
                message="存在未完结的翻译任务，请先等待任务完成或取消后再修改数据目录",
            )

        # 2. 解析与校验（失败保留旧配置）
        resolved, issues, resolve_error = self.preview(candidate)
        if resolve_error:
            return StorageApplyResult(status=STATUS_ERROR, message=resolve_error)
        errors = [issue for issue in issues if issue.is_error]
        if errors:
            return StorageApplyResult(
                status=STATUS_ERROR,
                message=errors[0].message,
                issues=issues,
            )

        assert resolved is not None
        if not self.paths_changed(candidate):
            # 目录未变化：仅保存配置值（例如仅修正格式）
            if not self._config_manager.update_storage_config(candidate):
                return StorageApplyResult(
                    status=STATUS_ERROR, message="配置保存失败，请检查磁盘后重试"
                )
            return StorageApplyResult(status=STATUS_UNCHANGED, issues=issues)

        # 3. 目标目录预创建（失败保留旧配置）
        try:
            self._resolver.ensure_directories(resolved)
        except StoragePathError as exc:
            return StorageApplyResult(
                status=STATUS_ERROR, message=f"无法创建新目录: {exc}", issues=issues
            )

        # 4. 可选迁移（失败：清单标记 failed，旧配置继续生效）
        migration_result: MigrationResult | None = None
        if migrate_data:
            current = self.current_paths()
            try:
                migration_result = self._migrator.migrate(
                    current, resolved, progress_callback=progress_callback
                )
            except MigrationConflictError as exc:
                return StorageApplyResult(status=STATUS_ERROR, message=str(exc), issues=issues)
            except MigrationError as exc:
                return StorageApplyResult(
                    status=STATUS_ERROR,
                    message=f"数据迁移失败: {exc}\n旧目录与旧配置保持不变，可重试。",
                    issues=issues,
                )

        # 5. 全部关键步骤完成后才写配置（§5：不允许半新半旧）
        if not self._config_manager.update_storage_config(candidate):
            return StorageApplyResult(
                status=STATUS_ERROR,
                message=(
                    "配置保存失败。数据已复制到新目录，但配置仍指向旧目录；"
                    "请检查磁盘后重新保存。"
                ),
                issues=issues,
                migration=migration_result,
            )

        logger.info("数据目录配置已更新，重启后生效")
        return StorageApplyResult(
            status=STATUS_MIGRATED if migration_result else STATUS_SAVED,
            issues=issues,
            migration=migration_result,
            needs_restart=True,
        )

    def _task_active(self) -> bool:
        if self._is_task_active is None:
            return False
        try:
            return bool(self._is_task_active())
        except Exception:  # noqa: BLE001 - 状态查询失败按无任务处理，避免阻断设置
            logger.warning("查询翻译任务状态失败，按无任务处理", exc_info=True)
            return False


def _paths_signature(paths: ResolvedStoragePaths) -> tuple[str, ...]:
    """用于变更比较的目录签名（大小写归一化）。"""
    import os

    return tuple(
        os.path.normcase(str(p))
        for p in (
            paths.cache_dir,
            paths.translation_records_dir,
            paths.translation_backups_dir,
            paths.projects_dir,
            paths.mappings_dir,
            paths.logs_dir or Path(""),
        )
    )
