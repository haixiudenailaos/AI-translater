#!/usr/bin/env python3
"""
翻译项目持久化仓库（UXF-004）

将 TranslationProject 状态持久化到磁盘，支持应用重启后恢复长任务。

职责：
- 保存/加载项目状态（原子写入）
- 保留最近若干检查点，允许撤销到翻译前版本
- 列出最近项目，供用户恢复
- 基于源文件指纹生成稳定项目 ID

设计要点：
- 项目文件存放在 data_dir/projects/<project_id>.json
- 检查点存放在 data_dir/projects/<project_id>.checkpoint_<n>.json
- 保存失败直接抛出异常（UXF-003：不得静默吞掉），由上层展示并允许重试
- 不在此处导入 tkinter 或 httpx，保持基础设施层纯净
"""

import datetime
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Tuple

from ..domain.project import SaveStatus, TaskStatus, TranslationProject
from ..utils.file_handler import write_json_atomic
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 项目状态文件格式版本
PROJECT_SCHEMA_VERSION = 2
# 保留的最大检查点数量
MAX_CHECKPOINTS = 5
# 最近项目列表文件名
RECENT_PROJECTS_FILE = "recent_projects.json"


class ProjectCorruptError(RuntimeError):
    """A persisted project could not be parsed and was isolated for recovery."""

    def __init__(self, project_id: str, original_path: Path, quarantined_path: Path | None):
        self.project_id = project_id
        self.original_path = original_path
        self.quarantined_path = quarantined_path
        location = str(quarantined_path or original_path)
        super().__init__(f"项目恢复文件已损坏，已隔离到: {location}")


def compute_project_id(source_path: str, fingerprint: str) -> str:
    """基于源文件路径和内容指纹生成稳定项目 ID。

    同一源文件（路径+内容）始终映射到同一项目 ID，
    支持应用重启后恢复。路径变化或内容变化会产生新 ID。
    """
    raw = f"{source_path}|{fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_file_fingerprint(file_path: Path, chunk_size: int = 65536) -> str:
    """计算文件内容 SHA-256 指纹，用于检测源文件是否变化。"""
    sha = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                sha.update(chunk)
    except FileNotFoundError:
        return ""
    return sha.hexdigest()


def compute_text_fingerprint(text: str) -> str:
    """计算文本内容 SHA-256 指纹（剪贴板导入场景）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProjectRepository:
    """翻译项目仓库（UXF-004）

    持久化 TranslationProject 到磁盘，支持：
    - create：创建新项目（原文、指纹、映射目录）
    - load：按项目 ID 加载
    - save：原子保存（失败抛异常）
    - checkpoint：创建检查点（覆盖性操作前调用，允许撤销）
    - list_recent：列出最近项目
    - delete：删除项目及其检查点
    """

    def __init__(self, projects_dir: Path):
        """初始化项目仓库。

        Args:
            projects_dir: 项目文件存放目录（通常为 data_dir/projects）
        """
        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    # ── 项目生命周期 ──────────────────────────────

    def create(
        self,
        *,
        source_path: str,
        source_fingerprint: str,
        file_type: str,
        mapping_dir: str,
        original_lines: List[str],
        model_snapshot: Dict[str, object] | None = None,
    ) -> TranslationProject:
        """创建新翻译项目。

        如果同 ID 项目已存在（同一源文件），返回已有项目而非覆盖。
        源文件发生变化时（指纹不同）会生成新项目 ID。
        """
        project_id = compute_project_id(source_path, source_fingerprint)
        existing = self.load(project_id)
        if existing is not None:
            # 已有项目：更新 last_opened_at
            existing.last_opened_at = _now_iso()
            return existing

        from ..domain.project import ModelSnapshot

        now = _now_iso()
        project = TranslationProject(
            project_id=project_id,
            source_path=source_path,
            source_fingerprint=source_fingerprint,
            file_type=file_type,
            mapping_dir=mapping_dir,
            original_lines=list(original_lines),
            translated_lines=[""] * len(original_lines),
            status=TaskStatus.PENDING,
            save_status=SaveStatus.UNSAVED,
            created_at=now,
            last_opened_at=now,
            last_saved_at="",
            model_snapshot=ModelSnapshot.from_dict(model_snapshot),
        )
        return project

    def load(self, project_id: str) -> TranslationProject | None:
        """Load a project, distinguishing absent and corrupted files."""
        path = self._project_file(project_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            project = TranslationProject.from_dict(data)
            project.last_opened_at = _now_iso()
            return project
        except Exception as exc:
            quarantined_path = self._quarantine_corrupt_project(path)
            logger.error(
                "加载项目 %s 失败，已隔离损坏文件 %s: %s",
                project_id,
                quarantined_path or path,
                exc,
            )
            raise ProjectCorruptError(project_id, path, quarantined_path) from exc

    def _quarantine_corrupt_project(self, path: Path) -> Path | None:
        """Move an unreadable project aside before any caller can recreate it."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        quarantined_path = path.with_name(f"{path.stem}.corrupt-{timestamp}{path.suffix}")
        try:
            path.replace(quarantined_path)
            return quarantined_path
        except OSError as exc:
            logger.error("隔离损坏项目文件 %s 失败: %s", path, exc)
            return None

    def find_by_source_path(self, source_path: str) -> TranslationProject | None:
        """P1-UX-2：按源文件路径查找最近的项目（用于指纹变化检测）。

        遍历最近项目列表，找到 source_path 匹配的项目并加载。
        找不到返回 None。同一路径下可能存在多个历史项目（指纹不同），
        返回最近打开的那个，由调用方比较指纹决定是否复用。
        """
        try:
            recent = self.list_recent(limit=50)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查找同路径项目时读取最近列表失败: %s", exc)
            return None
        for entry in recent:
            if str(entry.get("source_path", "")) == source_path:
                pid = str(entry.get("project_id", ""))
                if not pid:
                    continue
                project = self.load(pid)
                if project is not None:
                    return project
        return None

    def save(self, project: TranslationProject) -> None:
        """原子保存项目状态（UXF-003：失败抛异常，不静默吞掉）。

        保存成功后更新 last_saved_at 并标记为 SAVED。
        保存失败时保持 UNSAVED 状态，由上层捕获异常并提示"重试保存"。
        """
        project.last_saved_at = _now_iso()
        project.mark_saved()
        path = self._project_file(project.project_id)
        payload = project.to_dict()
        # write_json_atomic 失败会抛异常，不吞掉
        try:
            write_json_atomic(path, payload)
        except Exception:
            # 写入失败：回退为未保存状态
            project.mark_unsaved()
            raise
        # 更新最近项目列表
        self._touch_recent(project)

    # ── 检查点（UXF-001：覆盖前创建检查点，允许撤销） ──

    def create_checkpoint(self, project: TranslationProject, label: str = "") -> str:
        """创建检查点，返回检查点文件名。

        在"重新翻译全部"等覆盖性操作前调用，允许撤销到翻译前版本。
        保留最近 MAX_CHECKPOINTS 个检查点，超出时删除最旧的。
        """
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(c for c in label if c.isalnum() or c in "_-")[:20]
        name = f"{project.project_id}.ckpt_{ts}"
        if safe_label:
            name += f"_{safe_label}"
        name += ".json"

        path = self.projects_dir / name
        payload = project.to_dict()
        payload["checkpoint_label"] = label
        payload["checkpoint_created_at"] = _now_iso()
        write_json_atomic(path, payload)

        # 清理超出数量的旧检查点
        self._prune_checkpoints(project.project_id)
        logger.info("已创建检查点: %s", name)
        return name

    def list_checkpoints(self, project_id: str) -> List[Tuple[str, str]]:
        """列出项目的所有检查点。

        Returns:
            [(文件名, 创建时间), ...] 按时间倒序
        """
        prefix = f"{project_id}.ckpt_"
        results: List[Tuple[str, str]] = []
        for p in self.projects_dir.glob(f"{prefix}*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                created = str(data.get("checkpoint_created_at", ""))
                results.append((p.name, created))
            except Exception:
                results.append((p.name, ""))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def restore_checkpoint(
        self, project_id: str, checkpoint_name: str
    ) -> TranslationProject | None:
        """从检查点恢复项目状态。"""
        path = self.projects_dir / checkpoint_name
        if not path.exists() or not checkpoint_name.startswith(f"{project_id}.ckpt_"):
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return TranslationProject.from_dict(data)
        except Exception as e:
            logger.error("恢复检查点 %s 失败: %s", checkpoint_name, e)
            return None

    def _prune_checkpoints(self, project_id: str) -> None:
        """删除超出 MAX_CHECKPOINTS 的旧检查点。"""
        prefix = f"{project_id}.ckpt_"
        checkpoints = sorted(
            self.projects_dir.glob(f"{prefix}*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in checkpoints[MAX_CHECKPOINTS:]:
            try:
                old.unlink()
            except Exception:
                pass

    # ── 最近项目 ──────────────────────────────

    def list_recent(self, limit: int = 20) -> List[Dict[str, object]]:
        """列出最近打开的项目摘要，按 last_opened_at 倒序。"""
        recent_file = self.projects_dir / RECENT_PROJECTS_FILE
        if not recent_file.exists():
            return []
        try:
            data = json.loads(recent_file.read_text(encoding="utf-8"))
            entries = data.get("projects", []) if isinstance(data, dict) else []
            # 过滤掉已不存在的项目文件
            valid = []
            for entry in entries:
                pid = str(entry.get("project_id", ""))
                if pid and self._project_file(pid).exists():
                    valid.append(entry)
            valid.sort(key=lambda x: str(x.get("last_opened_at", "")), reverse=True)
            return valid[:limit]
        except Exception as e:
            logger.warning("读取最近项目列表失败: %s", e)
            return []

    def _touch_recent(self, project: TranslationProject) -> None:
        """更新最近项目列表。"""
        recent_file = self.projects_dir / RECENT_PROJECTS_FILE
        entries: List[Dict[str, object]] = []
        if recent_file.exists():
            try:
                data = json.loads(recent_file.read_text(encoding="utf-8"))
                entries = list(data.get("projects", [])) if isinstance(data, dict) else []
            except Exception:
                entries = []

        # 移除同 ID 旧条目
        entries = [e for e in entries if e.get("project_id") != project.project_id]

        entries.append(
            {
                "project_id": project.project_id,
                "source_path": project.source_path,
                "file_type": project.file_type,
                "status": project.status.value,
                "total_lines": project.total_lines,
                "translated_count": project.translated_count,
                "failed_count": project.failed_count,
                "last_opened_at": project.last_opened_at,
                "last_saved_at": project.last_saved_at,
            }
        )

        # 只保留最近 50 条
        entries.sort(key=lambda x: str(x.get("last_opened_at", "")), reverse=True)
        entries = entries[:50]

        try:
            write_json_atomic(recent_file, {"projects": entries})
        except Exception as e:
            logger.warning("更新最近项目列表失败: %s", e)

    # ── 删除 ──────────────────────────────

    def delete(self, project_id: str) -> bool:
        """删除项目及其所有检查点。"""
        deleted = False
        path = self._project_file(project_id)
        if path.exists():
            try:
                path.unlink()
                deleted = True
            except Exception as e:
                logger.warning("删除项目文件失败 %s: %s", path, e)

        # 删除检查点
        for ckpt in self.projects_dir.glob(f"{project_id}.ckpt_*.json"):
            try:
                ckpt.unlink()
            except Exception:
                pass

        # 从最近列表移除
        recent_file = self.projects_dir / RECENT_PROJECTS_FILE
        if recent_file.exists():
            try:
                data = json.loads(recent_file.read_text(encoding="utf-8"))
                entries = [e for e in data.get("projects", []) if e.get("project_id") != project_id]
                write_json_atomic(recent_file, {"projects": entries})
            except Exception:
                pass

        return deleted

    # ── 内部工具 ──────────────────────────────

    def _project_file(self, project_id: str) -> Path:
        return self.projects_dir / f"{project_id}.json"


def _now_iso() -> str:
    """当前时间 ISO 字符串（秒精度）。"""
    return datetime.datetime.now().isoformat(timespec="seconds")
