#!/usr/bin/env python3
"""
统一应用路径解析模块（BUG-001）

集中提供资源目录、数据目录、配置目录、工作区目录和日志目录的稳定路径。
打包程序可从任意当前工作目录启动，均读取同一份用户配置。

路径规则：
- 开发环境资源：项目根目录
- PyInstaller 资源：sys._MEIPASS
- Windows 用户数据：%APPDATA%/LightNovelTranslator
- macOS 用户数据：~/Library/Application Support/LightNovelTranslator
- Linux 用户数据：${XDG_DATA_HOME:-~/.local/share}/LightNovelTranslator
- 测试环境允许显式注入临时目录
"""

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# R2-BUG-021：不再在模块级导入 get_logger / file_handler。
# - get_logger 会在导入阶段触发日志初始化，导致日志目录被固定为 ./logs。
# - file_handler 会拉起 chardet 等重依赖。
# 改为方法内按需获取 logger 和 write_json_atomic，确保导入本模块无副作用。
logger = logging.getLogger(__name__)

APP_NAME = "LightNovelTranslator"


class AppPathsInitError(RuntimeError):
    """AppPaths 初始化失败（必要目录无法创建）。

    P2-7：必要目录失败必须抛出结构化错误，调用方在组合根中决定如何降级，
    不再静默返回指向不存在路径的无效对象，避免后续写入全部失败时
    错误现场被掩盖。
    """


def _is_frozen() -> bool:
    """是否运行在 PyInstaller 打包环境中"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _resource_dir() -> Path:
    """资源目录：开发环境为项目根，打包环境为 _MEIPASS"""
    if _is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    # 开发环境：src/app_paths.py 的上两级为项目根
    return Path(__file__).resolve().parent.parent


def _user_data_dir() -> Path:
    """用户数据目录（可写）：跨平台稳定路径"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / APP_NAME


def _env_path(var_name: str) -> Path | None:
    """P2-7：读取 AI_TRANSLATOR_* 环境变量并返回 Path。

    测试组合根通过环境变量注入临时目录，避免触碰真实用户目录；
    生产入口不设置这些变量，行为与之前一致。
    """
    value = os.environ.get(var_name)
    if not value:
        return None
    return Path(value)


@dataclass(frozen=True)
class AppPaths:
    """应用路径集合（不可变）

    Attributes:
        resource_dir: 只读资源目录（图标、默认配置等）
        data_dir: 用户数据根目录（可写）
        config_dir: 配置文件目录
        workspace_dir: EPUB 工作区目录
        log_dir: 日志目录
    """

    resource_dir: Path
    data_dir: Path
    config_dir: Path
    workspace_dir: Path
    log_dir: Path

    @classmethod
    def create(
        cls,
        *,
        data_dir: Path | None = None,
        config_dir: Path | None = None,
        workspace_dir: Path | None = None,
        log_dir: Path | None = None,
    ) -> "AppPaths":
        """创建 AppPaths，允许测试环境注入临时目录。

        解析优先级（P2-7）：
        1. 显式参数（最高，组合根测试使用）
        2. ``AI_TRANSLATOR_*`` 环境变量（隔离用户目录的测试 fixture 使用）
        3. 平台默认用户数据目录（生产路径）

        必要目录（data/config/workspace/logs）创建失败时抛出
        :class:`AppPathsInitError`，不再返回指向无效路径的对象。

        Args:
            data_dir: 显式指定用户数据目录（测试用）
            config_dir: 显式指定配置目录（测试用）
            workspace_dir: 显式指定工作区目录（测试用）
            log_dir: 显式指定日志目录（测试用）

        Raises:
            AppPathsInitError: 必要目录无法创建时抛出。
        """
        resource = _resource_dir()
        base_data = Path(data_dir) if data_dir else (_env_path("AI_TRANSLATOR_DATA_DIR") or _user_data_dir())

        cfg = Path(config_dir) if config_dir else (_env_path("AI_TRANSLATOR_CONFIG_DIR") or (base_data / "config"))
        ws = (
            Path(workspace_dir)
            if workspace_dir
            else (_env_path("AI_TRANSLATOR_WORKSPACE_DIR") or (base_data / "workspace"))
        )
        lg = Path(log_dir) if log_dir else (_env_path("AI_TRANSLATOR_LOG_DIR") or (base_data / "logs"))

        # P2-7：必要目录创建失败抛出结构化错误，避免后续在无效路径上反复失败
        failed: list[tuple[Path, str]] = []
        for d in (base_data, cfg, ws, lg):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("创建目录失败 %s: %s", d, exc)
                failed.append((d, str(exc)))

        if failed:
            details = "; ".join(f"{p}: {msg}" for p, msg in failed)
            raise AppPathsInitError(f"AppPaths 必要目录创建失败: {details}")

        return cls(
            resource_dir=resource,
            data_dir=base_data,
            config_dir=cfg,
            workspace_dir=ws,
            log_dir=lg,
        )

    def migrate_legacy_config(self) -> None:
        """兼容迁移：首次运行新版本时，从历史用户配置目录复制用户配置。

        R2-BUG-020 修复要点：
        - 将所有历史正式版本使用过的用户配置目录列为显式迁移源，
          不再扫描任意当前工作目录中的同名 config（避免迁移无关应用数据）。
        - 迁移来源、版本和文件列表写入 marker；失败时允许重试。
        - 只有全部待迁移文件都成功后才标记为 completed，否则标记 partial 允许重试。

        历史迁移源（按优先级）：
        1. 旧打包版用户目录 `~/.轻小说翻译器V1.4/config`
        2. 开发环境资源目录下的 config（仅非打包环境）
        """
        migration_marker = self.config_dir / "migration_state.json"

        # 已完成迁移则跳过（completed / no_source 都视为完成）
        if migration_marker.exists():
            try:
                import json as _json

                state = _json.loads(migration_marker.read_text(encoding="utf-8"))
                if state.get("status") in ("completed", "no_source"):
                    return
                # partial 状态：允许重试，继续执行迁移
            except (OSError, ValueError) as exc:
                # marker 损坏，继续尝试迁移
                logger.warning("迁移标记读取失败，将重试迁移: %s", exc)

        # R2-BUG-020：显式历史迁移源，不扫描任意 CWD
        legacy_sources = self._legacy_config_sources()

        # 选择第一个存在的迁移源
        legacy_config: Path | None = None
        for src in legacy_sources:
            try:
                if src.exists() and src.is_dir():
                    legacy_config = src
                    break
            except OSError:
                continue

        if legacy_config is None:
            self._write_migration_marker(
                migration_marker,
                status="no_source",
                source=None,
                migrated=[],
                failed=[],
            )
            return

        # 仅迁移用户配置文件，排除示例文件
        skip_files = {"api_config_sample.json", "glossary_sample.json"}
        migrated: list[str] = []
        failed: list[str] = []

        for src_file in legacy_config.glob("*.json"):
            if src_file.name in skip_files:
                continue
            dst_file = self.config_dir / src_file.name
            if dst_file.exists():
                continue  # 不覆盖已存在文件
            try:
                shutil.copy2(src_file, dst_file)
                migrated.append(src_file.name)
            except Exception as exc:
                logger.warning("迁移配置文件失败 %s: %s", src_file.name, exc)
                failed.append(src_file.name)

        if migrated:
            logger.info("已迁移旧配置文件: %s", ", ".join(migrated))

        # R2-BUG-020：只有无失败才标记 completed，否则标记 partial 允许重试
        status = "completed" if not failed else "partial"
        self._write_migration_marker(
            migration_marker,
            status=status,
            source=str(legacy_config),
            migrated=migrated,
            failed=failed,
        )

    def _legacy_config_sources(self) -> list[Path]:
        """返回历史正式版本使用过的用户配置目录列表。

        R2-BUG-020：不再扫描任意 CWD/config，避免迁移无关应用数据。
        """
        home = Path.home()
        sources: list[Path] = [
            # 旧打包版用户目录（中文应用名）
            home / ".轻小说翻译器V1.4" / "config",
            home / ".轻小说翻译器V1.3" / "config",
        ]
        # 开发环境：资源目录下的 config（仅非打包环境）
        if not _is_frozen():
            dev_config = self.resource_dir / "config"
            if dev_config not in sources:
                sources.append(dev_config)
        return sources

    @staticmethod
    def _write_migration_marker(
        marker: Path,
        *,
        status: str = "completed",
        source: str | None = None,
        migrated: list[str] | None = None,
        failed: list[str] | None = None,
    ) -> None:
        try:
            import datetime

            payload = {
                "schema_version": 1,
                "status": status,
                "source": source,
                "migrated_files": migrated or [],
                "failed_files": failed or [],
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            # R2-BUG-021：延迟导入，避免模块导入阶段拉起 file_handler / chardet
            from .utils.file_handler import write_json_atomic

            # BUG-006：使用原子写入
            write_json_atomic(marker, payload)
        except Exception as exc:
            logger.warning("写入迁移标记失败: %s", exc)
