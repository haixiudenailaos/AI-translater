#!/usr/bin/env python3
"""
队列翻译任务管理器（队列翻译并发优化阶段 2 重写版）。

职责变更（见 QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §5-§10）：
- 旧版：每任务独立线程 + 文件级 Semaphore + worker 直接调用 Tk。
- 新版：委托 ``QueueTranslationCoordinator`` 做全局公平调度 + 共享 ProviderLimiter，
  本类退化为"配置接线 + 旧 API 兼容层 + 文件解析"。

本类保留对外的旧 API（``TranslationTask`` 数据类、``add_task``、``start_task`` 等），
让 ``ConcurrentWindow`` / ``TaskDetailWindow`` 的改动最小化：
- 静态字段（``file_path`` / ``file_name`` / ``file_type`` / ``mapping_dir`` / ``source_lines``）
  在 ``add_task`` 时一次解析并缓存。
- 动态字段（``status`` / ``progress`` / ``target_lines`` / ``error_message`` / ``failed_indices``）
  在 ``get_task`` / ``get_all_tasks`` 时从 Coordinator 的不可变快照重建。

线程模型：
- Tk 主线程：调用本类 API（命令提交、快照查询）。
- Coordinator 线程：状态机、调度、检查点。
- Executor 工作线程：执行 BatchJob。
- 工作线程零 Tk 调用——``set_progress_callback`` 仅作为"状态变更 kick"通知 UI 主线程
  重 poll 快照，不再传递每批次进度。
"""

import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from ..config.translation_profile import build_queue_policy_from_app_config
from ..utils.file_handler import FileHandler
from ..utils.logger import get_logger
from .epub_processor import EPUBProcessor
from .queue_scheduler import (
    QueuePolicy,
    QueueSnapshot,
    QueueTranslationCoordinator,
)

logger = get_logger(__name__)


@dataclass
class TranslationTask:
    """单个翻译任务的数据模型（旧 UI 兼容视图）。

    队列翻译并发优化阶段 2：动态字段由 ``ConcurrentTranslationManager.get_task``
    从 ``QueueTranslationCoordinator`` 快照重建。直接修改本对象的 ``status`` /
    ``progress`` / ``target_lines`` 不会影响 Coordinator 状态。

    P1-UX-3：``error_category`` / ``error_safe_message`` / ``recommended_action``
    / ``error_retryable`` / ``correlation_id`` / ``failed_count`` 由 Coordinator
    落库的 ``ActionableError`` 派生，UI 直接展示安全文案与建议动作。
    """

    task_id: str
    file_path: str
    file_name: str
    file_type: str  # "txt" / "epub"
    status: str = "pending"  # pending / ready / running / pause_requested / paused / completed / partial / cancelled / error
    progress: float = 0.0
    source_lines: List[str] = field(default_factory=list)
    target_lines: List[str] = field(default_factory=list)
    error_message: str | None = None
    mapping_dir: str | None = None  # EPUB专用
    failed_indices: List[int] = field(default_factory=list)
    # P1-UX-3：分类错误字段（None 表示无错误或未分类）
    error_category: str | None = None
    error_safe_message: str | None = None
    recommended_action: str | None = None
    error_retryable: bool = False
    correlation_id: str | None = None
    failed_count: int = 0


class ConcurrentTranslationManager:
    """队列翻译管理器（命令门面 + 旧 API 兼容层）。

    所有并发与状态机由 ``QueueTranslationCoordinator`` 负责。本类只负责：
    1. 解析文件内容（TXT/EPUB）并注册到 Coordinator。
    2. 暴露旧 API（``start_task`` / ``pause_task`` / ``cancel_task`` 等）转译为 Coordinator 命令。
    3. 从 Coordinator 快照重建 ``TranslationTask`` 供 UI 读取。
    4. 维护 ``_epub_processor`` 供 ``ConcurrentWindow._export_all`` 等流程使用。
    """

    def __init__(
        self,
        config_manager,
        max_concurrent: int = 1,
        app_paths=None,
        *,
        limiter_registry=None,
    ):
        # ``max_concurrent`` 旧参数保留但不使用（新调度由 QueuePolicy.hard_request_cap 主导）。
        # 仅为向后兼容旧构造签名。
        self.config_manager = config_manager
        self.app_paths = app_paths
        self._file_handler = FileHandler()
        self._epub_processor = EPUBProcessor(app_paths=app_paths)

        # 静态任务元数据（task_id -> dict），add_task 时填入
        self._task_meta: Dict[str, dict] = {}
        self._task_order: List[str] = []
        self._meta_lock = threading.Lock()
        self._progress_callback: Callable | None = None
        self._closed = False

        # P1-UX-2：TXT 跨重启续传的项目仓库。从 app_paths.data_dir/projects
        # 加载/保存项目状态。app_paths 为 None（旧测试路径）时不接入，
        # Coordinator 降级为只写 _译文.txt 的旧行为。
        self._project_repository = None
        if app_paths is not None:
            try:
                from pathlib import Path

                from ..infrastructure.project_repository import ProjectRepository

                projects_dir = Path(app_paths.data_dir) / "projects"
                self._project_repository = ProjectRepository(projects_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("初始化 ProjectRepository 失败，TXT 续传降级: %s", exc)
                self._project_repository = None
        # P1-UX-2：指纹变化回调。默认 None（按新建处理），
        # 由 UI 层通过 set_fingerprint_mismatch_callback 注入。
        self._fingerprint_mismatch_callback: Callable | None = None

        # 构造 QueuePolicy 并启动 Coordinator
        app_config = config_manager.get_app_config()
        self._policy: QueuePolicy = build_queue_policy_from_app_config(app_config)
        self._coordinator = QueueTranslationCoordinator(
            config_manager,
            self._policy,
            file_handler=self._file_handler,
            epub_processor=self._epub_processor,
            app_paths=app_paths,
            limiter_registry=limiter_registry,
            project_repository=self._project_repository,
            fingerprint_mismatch_callback=self._invoke_fingerprint_mismatch_callback,
        )
        self._coordinator.start()

    # ── P1-UX-2：指纹变化回调 ──────────────────────────

    def set_fingerprint_mismatch_callback(self, callback: Callable | None) -> None:
        """P1-UX-2：注册指纹变化回调（UI 层注入）。

        回调签名：``callback(info: dict) -> str``，返回 ``"new" / "map" / "discard"``。
        回调在 Tk 主线程同步调用（``add_task`` 路径），可安全弹出 Tk 对话框。
        """
        self._fingerprint_mismatch_callback = callback

    def _invoke_fingerprint_mismatch_callback(self, info: dict) -> str:
        """包装回调以便异常时降级为 ``"new"``。"""
        cb = self._fingerprint_mismatch_callback
        if cb is None:
            return "new"
        try:
            result = cb(info)
            return result if result in ("new", "map", "discard") else "new"
        except Exception as exc:  # noqa: BLE001
            logger.warning("指纹变化回调异常，按新建处理: %s", exc)
            return "new"

    # ── 旧 API 兼容层 ────────────────────────────────────

    @property
    def max_concurrent(self) -> int:
        return self._policy.max_in_flight_requests

    @max_concurrent.setter
    def max_concurrent(self, value: int) -> None:
        # 旧 API 兼容：不再支持运行时修改并发数，需通过设置页修改 queue_max_in_flight_requests。
        # 写入只更新 policy 副本，不影响已运行的 Coordinator。
        pass

    @property
    def tasks(self) -> Dict[str, TranslationTask]:
        """旧 API：返回 task_id -> TranslationTask 视图（每次调用重建）。"""
        return {tid: self.get_task(tid) for tid in self._task_order if self.get_task(tid)}

    def set_progress_callback(self, callback: Callable | None) -> None:
        """设置 UI 进度回调（状态变更 kick）。

        队列并发优化阶段 2 后，UI 通过轮询不可变 Snapshot 获取精确进度，
        此回调仅作为"任务状态有变化"的轻量通知，触发 UI 提前 poll。
        """
        self._progress_callback = callback

    def close(self) -> None:
        """关闭管理器：取消所有任务、释放 API 资源、关闭 Coordinator。幂等。"""
        with self._meta_lock:
            if self._closed:
                return
            self._closed = True
            self._progress_callback = None
        try:
            self._coordinator.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭 Coordinator 失败: %s", exc)

    # ── 任务添加与解析 ───────────────────────────────────

    def add_task(
        self,
        file_path: str,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> TranslationTask:
        """添加翻译任务：解析文件内容并注册到 Coordinator。"""
        from pathlib import Path

        path = Path(file_path)
        file_type = "epub" if path.suffix.lower() == ".epub" else "txt"
        task_id = str(uuid.uuid4())[:8]

        # 解析文件内容
        if file_type == "epub":
            mapping_info = self._epub_processor.import_epub(
                file_path,
                cancel_requested=cancel_requested,
            )
            mapping_dir = mapping_info["mapping_dir"]
            originals, translations = self._epub_processor.load_content_mapping(mapping_dir)
            source_lines = list(originals)
            target_lines = list(translations)
        else:
            mapping_dir = None
            content = self._file_handler.read_file(file_path)
            source_lines = content.splitlines()
            target_lines = [""] * len(source_lines)

        # 注册到 Coordinator（独占写锁失败时拒绝）
        ok = self._coordinator.add_task(
            task_id=task_id,
            file_path=str(path),
            file_name=path.name,
            file_type=file_type,
            mapping_dir=mapping_dir,
            source_lines=source_lines,
            target_lines=target_lines,
        )
        if not ok:
            raise RuntimeError(f"文件已在队列中或路径冲突: {path.name}")

        with self._meta_lock:
            self._task_meta[task_id] = {
                "task_id": task_id,
                "file_path": str(path),
                "file_name": path.name,
                "file_type": file_type,
                "mapping_dir": mapping_dir,
                "source_lines": source_lines,
            }
            self._task_order.append(task_id)

        # 初始 TranslationTask 视图（动态字段从 Coordinator 取）
        return self._build_task_view(task_id, target_lines, status="pending")

    # ── 命令接口（转译为 Coordinator 命令） ──────────────

    def start_task(self, task_id: str) -> None:
        self._coordinator.submit_command("start", task_id)
        self._notify_progress(task_id)

    def pause_task(self, task_id: str) -> None:
        self._coordinator.submit_command("pause", task_id)
        self._notify_progress(task_id)

    def resume_task(self, task_id: str) -> None:
        self._coordinator.submit_command("resume", task_id)
        self._notify_progress(task_id)

    def cancel_task(self, task_id: str) -> None:
        self._coordinator.submit_command("cancel", task_id)
        self._notify_progress(task_id)

    def start_all(self) -> None:
        self._coordinator.submit_command("start_all")
        self._notify_progress(None)

    def pause_all(self) -> None:
        self._coordinator.submit_command("pause_all")
        self._notify_progress(None)

    def cancel_all(self) -> None:
        self._coordinator.submit_command("cancel_all")
        self._notify_progress(None)

    def remove_task(self, task_id: str) -> None:
        """移除任务（仅限非 running/pause_requested 状态）。"""
        task = self.get_task(task_id)
        if not task:
            return
        if task.status in ("running", "pause_requested"):
            return
        ok = self._coordinator.remove_task(task_id)
        if not ok:
            return
        with self._meta_lock:
            self._task_meta.pop(task_id, None)
            if task_id in self._task_order:
                self._task_order.remove(task_id)
        self._notify_progress(task_id)

    # ── 查询接口（从 Coordinator 快照重建） ──────────────

    def get_task(self, task_id: str) -> TranslationTask | None:
        """返回任务的当前视图。任务不存在返回 None。"""
        with self._meta_lock:
            meta = self._task_meta.get(task_id)
        if meta is None:
            return None
        data = self._coordinator.get_task_data(task_id)
        if data is None:
            # 已被 Coordinator 清理但仍在本类元数据中（罕见）：用元数据兜底
            return self._build_task_view(
                task_id, meta.get("target_lines_backup", []), status="cancelled"
            )
        return TranslationTask(
            task_id=data["task_id"],
            file_path=data["file_path"],
            file_name=data["file_name"],
            file_type=data["file_type"],
            status=data["status"],
            progress=data["progress"],
            source_lines=list(data["source_lines"]),
            target_lines=list(data["target_lines"]),
            error_message=data.get("error_message"),
            mapping_dir=data.get("mapping_dir"),
            failed_indices=list(data.get("failed_indices", [])),
            # P1-UX-3：分类错误字段透传
            error_category=data.get("error_category"),
            error_safe_message=data.get("error_safe_message"),
            recommended_action=data.get("recommended_action"),
            error_retryable=bool(data.get("error_retryable", False)),
            correlation_id=data.get("correlation_id"),
            failed_count=int(data.get("failed_count", 0)),
        )

    def get_all_tasks(self) -> List[TranslationTask]:
        with self._meta_lock:
            order = list(self._task_order)
        tasks: List[TranslationTask] = []
        for tid in order:
            t = self.get_task(tid)
            if t is not None:
                tasks.append(t)
        return tasks

    def save_task(self, task_id: str) -> bool:
        """同步保存任务（详情页编辑后落盘）。"""
        return self._coordinator.save_task_now(task_id)

    def update_task_line(self, task_id: str, row_idx: int, new_value: str) -> bool:
        """P0-2：更新任务内部译文行并标记检查点为脏。"""
        return self._coordinator.update_task_line(task_id, row_idx, new_value)

    # ── 快照接口（供 UI 直接轮询，阶段 4 推荐） ───────────

    def get_snapshot(self) -> QueueSnapshot | None:
        """返回最新不可变队列快照（阶段 4 推荐 UI 轮询入口）。"""
        return self._coordinator.get_snapshot()

    # ── 内部辅助 ─────────────────────────────────────────

    def _build_task_view(
        self,
        task_id: str,
        target_lines: List[str],
        *,
        status: str = "pending",
    ) -> TranslationTask:
        """构造初始 TranslationTask 视图（注册时使用）。"""
        with self._meta_lock:
            meta = self._task_meta.get(task_id, {})
        return TranslationTask(
            task_id=task_id,
            file_path=meta.get("file_path", ""),
            file_name=meta.get("file_name", ""),
            file_type=meta.get("file_type", "txt"),
            status=status,
            progress=0.0,
            source_lines=list(meta.get("source_lines", [])),
            target_lines=list(target_lines),
            error_message=None,
            mapping_dir=meta.get("mapping_dir"),
            failed_indices=[],
        )

    def _notify_progress(self, task_id: str | None) -> None:
        """通知 UI 有状态变更（kick 主线程轮询快照）。"""
        cb = self._progress_callback
        if cb is None or self._closed:
            return
        try:
            cb(task_id)
        except Exception:  # noqa: BLE001
            logger.debug("progress_callback 异常被吞掉", exc_info=True)
