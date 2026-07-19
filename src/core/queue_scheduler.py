#!/usr/bin/env python3
"""队列翻译全局公平调度器（队列翻译并发优化阶段 2-4）。

职责（见 QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §5-§10）：
- ``QueueTranslationCoordinator`` 是任务状态、批次状态和写回顺序的唯一修改者。
- Worker 只接收不可变 ``BatchJob``，返回不可变 ``BatchOutcome``，不修改任务、不调用 Tk。
- 全局只有一个 ``ThreadPoolExecutor(max_workers=hard_cap)``，不存在两层并发乘法。
- 调度策略：round-robin 第一轮（每任务最多 1 槽位）+ 空闲槽位借用（单大任务独享剩余）。
- ``attempt_id`` 隔离：取消/重启后旧 attempt 的 Outcome 一律丢弃。
- 429/``Retry-After``/RPM/TPM/AIMD 由共享 ``ProviderLimiter`` 统一决策（阶段 3）。
- 批次成功后 generation/debounce 单飞原子保存（阶段 4）。
- UI 只消费不可变 ``QueueSnapshot``，工作线程零 Tk 调用（阶段 4）。

线程模型（§5.1）：
- Tk 主线程：发送命令、渲染快照。
- Coordinator 线程：状态机、调度、结果应用、检查点调度。
- Executor 工作线程：执行 ``BatchJob``，返回 ``BatchOutcome``。
- Checkpoint 线程：串行写盘。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Tuple

# P1-UX-3：所有异常先转换为 ActionableError，UI 展示安全文案与建议动作
from ..application.error_handling import (
    ActionableError,
    ErrorCategory,
    classify_error,
    log_classified_error,
)
from ..domain.errors import TranslationCancelled, TranslationRequestError
from ..domain.project import TranslationProject

# P1-UX-2：TXT 队列跨重启续传所需的指纹 / 项目仓库支持
from ..infrastructure.project_repository import (
    ProjectCorruptError,
    compute_file_fingerprint,
    compute_project_id,
)
from ..utils.logger import get_logger
from ..utils.token_estimator import estimate_tokens
from .queue_checkpoint import (
    CheckpointCoordinator,
    CheckpointSnapshot,
    ExclusiveWriteLockRegistry,
)
from .queue_provider import (
    ProviderLimiter,
    ProviderLimiterRegistry,
    provider_runtime_key_for,
)
from .translation_result import BatchTranslationResult, TranslationStatus
from .translator import TranslationRunContext, TranslatorEngine

logger = get_logger(__name__)


# ── 数据结构（不可变） ─────────────────────────────────


class QueueTaskState(str, Enum):
    """队列任务状态机（§9.1）。

    状态转换：
        PENDING -> READY -> RUNNING -> FINALIZING -> COMPLETED
                       |    |
                       |    +-> PARTIAL / ERROR
                       |
                       +-> PAUSE_REQUESTED -> PAUSED -> READY
                       |
                       +-> CANCELLED
    """

    PENDING = "pending"
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    ERROR = "error"


# UI 友好的状态文案映射（保留与旧状态字符串兼容，便于 ConcurrentWindow 复用）
STATE_DISPLAY_MAP = {
    QueueTaskState.PENDING: "等待中",
    QueueTaskState.PREPARING: "准备中",
    QueueTaskState.READY: "准备中",
    QueueTaskState.RUNNING: "翻译中",
    QueueTaskState.PAUSE_REQUESTED: "暂停中",
    QueueTaskState.PAUSED: "已暂停",
    QueueTaskState.FINALIZING: "正在保存",
    QueueTaskState.COMPLETED: "已完成",
    QueueTaskState.PARTIAL: "部分完成",
    QueueTaskState.CANCELLED: "已取消",
    QueueTaskState.ERROR: "出错",
}

# 旧字符串状态到新枚举的兼容映射（ConcurrentWindow 仍用字符串）
_LEGACY_STATE_STRINGS = {
    "pending": QueueTaskState.PENDING,
    "running": QueueTaskState.RUNNING,
    "paused": QueueTaskState.PAUSED,
    "completed": QueueTaskState.COMPLETED,
    "partial": QueueTaskState.PARTIAL,
    "cancelled": QueueTaskState.CANCELLED,
    "error": QueueTaskState.ERROR,
}


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """队列调度策略（§5.2）。

    所有并发只有一个清晰 owner：全局 ``ProviderLimiter.current_limit``。
    不存在"文件并发 x 文件内并发"两层乘法。
    """

    max_in_flight_requests: int = 2
    hard_request_cap: int = 4
    max_active_tasks: int = 4
    per_task_soft_limit: int = 1
    target_batch_input_tokens: int = 6000
    max_batch_input_tokens: int = 16000
    max_batch_lines: int = 80
    min_batch_input_tokens: int = 1000
    adaptive_concurrency: bool = True
    rpm_limit: int = 0
    tpm_limit: int = 0


@dataclass(frozen=True, slots=True)
class BatchJob:
    """不可变批次任务（§5.2）。

    Worker 只读取本对象，不修改任务状态、不调用 Tk。
    ``source_indices`` 是最终写回依据，不依赖 Future 完成顺序。
    API Key 不进入 Job/日志/指标/repr。
    """

    task_id: str
    attempt_id: str
    batch_id: int
    source_indices: Tuple[int, ...]
    source_lines: Tuple[str, ...]
    estimated_input_tokens: int
    run_context: TranslationRunContext


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """不可变批次结果（§5.2）。

    Worker 返回，Coordinator 校验 ``attempt_id`` 后应用。
    乱序完成不改变最终行顺序——Coordinator 按 ``source_indices`` 写回固定位置。

    P1-UX-3：``error_category`` / ``recommended_action`` / ``correlation_id``
    由 Worker 在 ``execute_batch_job`` 中通过 ``classify_error`` 转换得到，
    Coordinator 将其落到 ``_TaskSlot.actionable_error`` 供 UI 消费。
    """

    task_id: str
    attempt_id: str
    batch_id: int
    source_indices: Tuple[int, ...]
    translated_lines: Tuple[str, ...]
    failed_relative_indices: Tuple[int, ...]
    retry_count: int
    rate_limited: bool
    retry_after_seconds: float | None
    request_seconds: float
    error_message: str | None = None
    cancelled: bool = False
    # P1-UX-3：分类后的错误信息，供 UI 展示安全文案与建议动作。
    # error_message 是原始 str(exc)，error_safe_message 是脱敏后的用户可读文案。
    error_category: str | None = None
    error_safe_message: str | None = None
    recommended_action: str | None = None
    error_retryable: bool = False
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class QueueTaskSnapshot:
    """任务级不可变快照（供 UI 轮询，§11.2）。

    P1-UX-3：``error_category`` / ``recommended_action`` / ``correlation_id``
    / ``failed_count`` 让 UI 无需解析 ``error_message`` 即可显示分类、
    建议动作和失败行数。
    """

    task_id: str
    file_name: str
    file_type: str
    state: QueueTaskState
    state_display: str
    progress: float
    completed_lines: int
    total_lines: int
    in_flight_batches: int
    pending_batches: int
    error_message: str | None
    failed_indices: Tuple[int, ...]
    # 兼容旧 UI：旧 status 字符串
    status: str
    # 检查点状态
    checkpoint_dirty: bool
    checkpoint_error: str | None
    # P1-UX-3：分类后的错误信息（UI 直接显示文案与动作，不解析 error_message）
    error_category: str | None = None
    error_safe_message: str | None = None
    recommended_action: str | None = None
    correlation_id: str | None = None
    failed_count: int = 0


@dataclass(frozen=True, slots=True)
class QueueMetricsSnapshot:
    """队列级聚合指标快照（§11.2）。"""

    active_requests: int
    current_limit: int
    hard_cap: int
    configured_max: int
    is_blocked: bool
    cooldown_remaining: float
    total_429: int
    total_timeouts: int
    total_success: int
    consecutive_successes: int
    # 吞吐（最近窗口）
    throughput_lines_per_minute: float
    # ETA（基于最近完成批次，样本不足为 None）
    eta_seconds: float | None


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """完整队列快照：任务列表 + 聚合指标。"""

    tasks: Tuple[QueueTaskSnapshot, ...]
    metrics: QueueMetricsSnapshot


# ── BatchPlanner（纯函数，从 _build_batch_ranges 抽出） ──


def plan_batches(
    source_lines: List[str],
    missing_indices: List[int],
    policy: QueuePolicy,
    run_context: TranslationRunContext,
    input_token_budget: int,
    *,
    start_batch_id: int = 0,
) -> List[BatchJob]:
    """构造批次计划（§7）。

    纯函数：不读取配置、不修改任务状态、不调用 Tk。
    只为 ``missing_indices`` 中的行生成 BatchJob，``source_indices`` 指向绝对位置。

    - ``target_batch_input_tokens`` 是调度目标，``max_batch_input_tokens`` 是硬上限。
    - ``max_batch_lines`` 防止极短行无限聚合。
    - 估算含 system prompt、术语表、行号标记协议开销。
    """
    if not missing_indices:
        return []

    # 协议固定开销（system prompt + 行号标记模板）
    protocol = run_context.system_prompt or ""
    fixed_tokens = estimate_tokens(protocol)

    jobs: List[BatchJob] = []
    batch_id = start_batch_id
    idx_pos = 0
    n = len(missing_indices)

    while idx_pos < n:
        end_pos = idx_pos
        used_tokens = fixed_tokens
        batch_indices: List[int] = []
        batch_lines: List[str] = []

        while end_pos < n and (end_pos - idx_pos) < policy.max_batch_lines:
            abs_idx = missing_indices[end_pos]
            src_line = source_lines[abs_idx] if abs_idx < len(source_lines) else ""
            marker = f"[LINE_{end_pos - idx_pos + 1:03d}]"
            line_tokens = estimate_tokens(marker + src_line + "\n")
            if end_pos > idx_pos and used_tokens + line_tokens > input_token_budget:
                break
            used_tokens += line_tokens
            batch_indices.append(abs_idx)
            batch_lines.append(src_line)
            end_pos += 1

        if not batch_indices:
            # 兜底：至少推进一行
            abs_idx = missing_indices[idx_pos]
            src_line = source_lines[abs_idx] if abs_idx < len(source_lines) else ""
            batch_indices.append(abs_idx)
            batch_lines.append(src_line)
            end_pos = idx_pos + 1

        jobs.append(
            BatchJob(
                task_id="",  # 由 Coordinator 填充
                attempt_id="",  # 由 Coordinator 填充
                batch_id=batch_id,
                source_indices=tuple(batch_indices),
                source_lines=tuple(batch_lines),
                estimated_input_tokens=used_tokens,
                run_context=run_context,
            )
        )
        batch_id += 1
        idx_pos = end_pos

    return jobs


# ── BatchWorker（执行 BatchJob，返回 BatchOutcome） ─────


def execute_batch_job(
    job: BatchJob,
    engine: TranslatorEngine,
    cancel_event: threading.Event,
    limiter: ProviderLimiter,
) -> BatchOutcome:
    """执行一个 BatchJob，返回不可变 BatchOutcome（§5.1）。

    Worker 不修改任务状态、不调用 Tk、不持有任务锁。
    结果上报（成功/429/超时）交给 ``limiter``；槽位 acquire/release 由 Coordinator 负责。
    """
    started_at = time.monotonic()

    # 取消检查（等待槽位期间可零请求取消，§4.1）
    if cancel_event.is_set() or engine.is_stopped:
        return BatchOutcome(
            task_id=job.task_id,
            attempt_id=job.attempt_id,
            batch_id=job.batch_id,
            source_indices=job.source_indices,
            translated_lines=tuple(),
            failed_relative_indices=tuple(range(len(job.source_lines))),
            retry_count=0,
            rate_limited=False,
            retry_after_seconds=None,
            request_seconds=0.0,
            error_message="cancelled",
            cancelled=True,
        )

    batch_lines = list(job.source_lines)
    try:
        # 复用 engine._translate_batch 的协议构造、行号标记解析和 Hunyuan 路径。
        # progress_callback 置空：Coordinator 从 Snapshot 驱动 UI，不接收逐 token 回调。
        result: BatchTranslationResult = engine._translate_batch(
            batch_lines,
            lambda *_: None,
            0,
            len(batch_lines),
            emit_stream_progress=False,
            run_context=job.run_context,
        )
        request_seconds = time.monotonic() - started_at

        # 结果上报：成功（含 PARTIAL 也算请求成功，只是部分行缺失）
        limiter.record_success()

        # 转换为 BatchOutcome
        # result.failed_indices 是相对批次起始的 0-based 索引
        return BatchOutcome(
            task_id=job.task_id,
            attempt_id=job.attempt_id,
            batch_id=job.batch_id,
            source_indices=job.source_indices,
            translated_lines=tuple(result.lines),
            failed_relative_indices=tuple(result.failed_indices),
            retry_count=0,
            rate_limited=False,
            retry_after_seconds=None,
            request_seconds=request_seconds,
            error_message=result.error_message,
            cancelled=(result.status == TranslationStatus.CANCELLED),
        )

    except TranslationCancelled:
        request_seconds = time.monotonic() - started_at
        return BatchOutcome(
            task_id=job.task_id,
            attempt_id=job.attempt_id,
            batch_id=job.batch_id,
            source_indices=job.source_indices,
            translated_lines=tuple(),
            failed_relative_indices=tuple(range(len(batch_lines))),
            retry_count=0,
            rate_limited=False,
            retry_after_seconds=None,
            request_seconds=request_seconds,
            error_message="cancelled",
            cancelled=True,
        )
    except TranslationRequestError as exc:
        request_seconds = time.monotonic() - started_at
        rate_limited = exc.status_code == 429
        if rate_limited:
            # 立即上报 429，让共享 Limiter 在 Provider 范围统一 cooldown（§8.2）
            limiter.record_rate_limited(
                retry_after_seconds=exc.retry_after_seconds,
            )
        elif exc.status_code in (408, 504) or "timeout" in str(exc).lower():
            limiter.record_timeout()
        else:
            # 其他失败不计入限流指标，但重置成功计数阻止立即恢复
            limiter.record_timeout()
        # P1-UX-3：转换为 ActionableError，记录原始异常到脱敏日志
        actionable = classify_error(exc)
        log_classified_error(
            exc,
            actionable,
            context={
                "task_id": job.task_id,
                "batch_id": job.batch_id,
                "status_code": exc.status_code,
            },
        )
        return BatchOutcome(
            task_id=job.task_id,
            attempt_id=job.attempt_id,
            batch_id=job.batch_id,
            source_indices=job.source_indices,
            translated_lines=tuple(),
            failed_relative_indices=tuple(range(len(batch_lines))),
            retry_count=0,
            rate_limited=rate_limited,
            retry_after_seconds=exc.retry_after_seconds,
            request_seconds=request_seconds,
            error_message=str(exc),
            error_category=actionable.category.value,
            error_safe_message=actionable.safe_message,
            recommended_action=actionable.recommended_action,
            error_retryable=actionable.retryable,
            correlation_id=actionable.correlation_id,
        )
    except Exception as exc:  # noqa: BLE001 - Worker 边界必须兜底
        request_seconds = time.monotonic() - started_at
        logger.exception("BatchJob 执行未预期异常: task=%s batch=%s", job.task_id, job.batch_id)
        limiter.record_timeout()
        # P1-UX-3：兜底分类，确保所有 Worker 异常都有 safe_message 与动作
        actionable = classify_error(exc)
        log_classified_error(
            exc,
            actionable,
            context={
                "task_id": job.task_id,
                "batch_id": job.batch_id,
            },
        )
        return BatchOutcome(
            task_id=job.task_id,
            attempt_id=job.attempt_id,
            batch_id=job.batch_id,
            source_indices=job.source_indices,
            translated_lines=tuple(),
            failed_relative_indices=tuple(range(len(batch_lines))),
            retry_count=0,
            rate_limited=False,
            retry_after_seconds=None,
            request_seconds=request_seconds,
            error_message=str(exc),
            error_category=actionable.category.value,
            error_safe_message=actionable.safe_message,
            recommended_action=actionable.recommended_action,
            error_retryable=actionable.retryable,
            correlation_id=actionable.correlation_id,
        )


# ── 内部任务槽（Coordinator 私有，受 _lock 保护） ──────


@dataclass
class _TaskSlot:
    """单个任务在 Coordinator 内部的可变状态。"""

    task_id: str
    file_path: str
    file_name: str
    file_type: str  # "txt" / "epub"
    mapping_dir: str | None
    source_lines: List[str]
    target_lines: List[str]
    state: QueueTaskState = QueueTaskState.PENDING
    attempt_id: str = ""
    engine: TranslatorEngine | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # 批次队列（未提交）
    pending_batches: deque = field(default_factory=deque)
    # 已提交批次：batch_id -> BatchJob
    in_flight_batches: Dict[int, BatchJob] = field(default_factory=dict)
    # 已完成的批次 ID 集合
    completed_batch_ids: set = field(default_factory=set)
    # 失败的原索引（绝对）
    failed_indices: List[int] = field(default_factory=list)
    error_message: str | None = None
    # 进度（0-100）
    progress: float = 0.0
    # 检查点协调器
    checkpoint: CheckpointCoordinator | None = None
    # 检查点 generation（单调递增）
    checkpoint_generation: int = 0
    # 统计
    completed_lines: int = 0
    total_to_translate: int = 0
    # 轮询公平性：上次派发时间戳
    last_dispatch_at: float = 0.0
    # P1-UX-2：TXT 跨重启续传状态
    source_fingerprint: str = ""
    project: TranslationProject | None = None
    # P1-UX-2：手工编辑行索引（不被自动任务覆盖，重启后保留）
    manually_edited_indices: set = field(default_factory=set)
    # P1-UX-2：显式完成行索引（译文非空 OR 手工标记完成 OR 翻译成功）
    # 用集合而不是"译文非空"推断，因为空字符串也可能是有效的译文结果。
    completed_indices: set = field(default_factory=set)
    # P1-UX-3：分类后的用户可读错误（含 correlation_id）。
    # 每次 BatchOutcome 带回分类信息后刷新；任务恢复时清空。
    actionable_error: ActionableError | None = None
    # 用户已请求开始，但因活动窗口已满而尚未 prepare 的任务。
    start_requested: bool = False
    # FINALIZING 阶段由锁外 executor 处理，避免检查点和引擎 I/O 阻塞调度锁。
    finalization_scheduled: bool = False
    finalization_complete: bool = False
    final_state_hint: QueueTaskState | None = None


# ── 命令（Tk 主线程 -> Coordinator） ────────────────────


@dataclass(frozen=True, slots=True)
class _Command:
    """Coordinator 命令（不可变）。"""

    kind: str  # "start" / "pause" / "resume" / "cancel" / "start_all" / "pause_all" / "cancel_all" / "remove" / "close"
    task_id: str | None = None


# ── QueueTranslationCoordinator ────────────────────────


class QueueTranslationCoordinator:
    """队列翻译全局公平调度器（§5-§10）。

    生命周期由 ``ConcurrentTranslationManager`` 管理：一个 Coordinator 实例对应应用内队列。
    关闭后不再接受命令，等待在途请求结束后退出调度线程。
    """

    def __init__(
        self,
        config_manager,
        policy: QueuePolicy,
        *,
        file_handler,
        epub_processor,
        app_paths=None,
        limiter_registry: ProviderLimiterRegistry | None = None,
        project_repository=None,
        fingerprint_mismatch_callback=None,
    ) -> None:
        self._config_manager = config_manager
        self._policy = policy
        self._file_handler = file_handler
        self._epub_processor = epub_processor
        self._app_paths = app_paths

        # P1-UX-2：TXT 跨重启续传仓库。None 时降级为旧行为（仅写 _译文.txt）。
        self._project_repository = project_repository
        # P1-UX-2：指纹变化回调。回调返回 "new" / "map" / "discard"。
        # None 时默认 "new"（不尝试复用旧进度），避免阻塞 Coordinator 线程。
        self._fingerprint_mismatch_callback = fingerprint_mismatch_callback

        self._lock = threading.Lock()
        self._tasks: Dict[str, _TaskSlot] = {}
        self._task_order: List[str] = []  # 添加顺序（round-robin 基准）
        self._closed = False

        # 命令队列（Tk 主线程 -> Coordinator）
        self._commands: deque[_Command] = deque()
        # Commands and completed futures share one wake-up event.  The
        # scheduler must not wait for the next polling tick after a fast
        # batch completes.
        self._wake_event = threading.Event()

        # 待应用的 Outcome（从 future 收割后暂存，下一步统一应用）
        self._pending_outcomes: deque[BatchOutcome] = deque()

        # 全局执行池：max_workers = hard_cap，硬上限不可突破（§6.1）
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, policy.hard_request_cap),
            thread_name_prefix="queue-batch",
        )
        self._in_flight: Dict[Future, Tuple[str, int]] = {}  # future -> (task_id, batch_id)
        self._finalizer_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="queue-finalizer",
        )
        self._finalizing: Dict[Future, str] = {}

        # 共享 ProviderLimiter 注册表（阶段 3）。应用注入时与主编辑器共同计数；
        # 独立构造 Coordinator（测试/工具）时仍由自身持有。
        self._owns_limiter_registry = limiter_registry is None
        self._limiter_registry = limiter_registry or ProviderLimiterRegistry()
        # 当前活跃 Limiter（单 Provider 场景；多 Provider 时按 key 查询）
        self._active_limiter: ProviderLimiter | None = None

        # 独占写锁注册表（§10.2）
        self._write_locks = ExclusiveWriteLockRegistry()

        # 最新快照（UI 轮询读取）
        self._latest_snapshot: QueueSnapshot | None = None
        self._snapshot_lock = threading.Lock()

        # 吞吐统计（最近窗口）
        self._throughput_window_start = time.monotonic()
        self._throughput_lines = 0

        # ETA 样本：最近完成批次的 request_seconds
        self._recent_batch_seconds: deque[float] = deque(maxlen=20)

        # 调度线程
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_started = False

    # ── 生命周期 ────────────────────────────────────────

    def start(self) -> None:
        """启动调度线程。幂等。"""
        with self._lock:
            if self._scheduler_started or self._closed:
                return
            self._scheduler_started = True
            self._scheduler_thread = threading.Thread(
                target=self._run,
                name="queue-coordinator",
                daemon=True,
            )
            self._scheduler_thread.start()

    def close(self) -> None:
        """关闭调度器：停止派发，等待在途请求结束（带超时），关闭引擎和执行池。

        幂等可安全多次调用。关闭后命令被忽略。
        """
        engines_to_stop: list[TranslatorEngine] = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # 取消所有未提交的 pending 批次
            for slot in self._tasks.values():
                slot.pending_batches.clear()
                if slot.engine is not None:
                    engines_to_stop.append(slot.engine)
            self._wake_event.set()
        for engine in engines_to_stop:
            self._stop_engine(engine)
        # 等待调度线程退出（它会 drain 在途并关闭引擎）
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=10.0)
        # 兜底关闭执行池
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._scheduler_thread is None or not self._scheduler_thread.is_alive():
            self._finalizer_executor.shutdown(wait=False, cancel_futures=True)
        else:
            logger.warning("队列调度线程在关闭 deadline 后仍在清理资源")
        # 关闭所有检查点协调器（触发最后一次关键保存）
        for slot in list(self._tasks.values()):
            if slot.checkpoint is not None:
                try:
                    slot.checkpoint.close()
                except Exception:
                    pass
        # 关闭所有引擎
        for slot in list(self._tasks.values()):
            self._close_engine(slot)
        # 释放写锁
        for slot in list(self._tasks.values()):
            self._release_write_lock(slot)
        if self._owns_limiter_registry:
            self._limiter_registry.close_all()

    # ── 命令接口（Tk 主线程调用） ────────────────────────

    def submit_command(self, kind: str, task_id: str | None = None) -> None:
        """提交命令到 Coordinator（非阻塞）。关闭后忽略。"""
        with self._lock:
            if self._closed and kind != "close":
                return
            self._commands.append(_Command(kind=kind, task_id=task_id))
        self._wake_event.set()

    def add_task(
        self,
        task_id: str,
        file_path: str,
        file_name: str,
        file_type: str,
        mapping_dir: str | None,
        source_lines: List[str],
        target_lines: List[str],
    ) -> bool:
        """注册新任务到 Coordinator。返回是否成功（同路径独占锁失败返回 False）。

        P1-UX-2：TXT 任务接入 ProjectRepository，按路径 + SHA-256 指纹
        查找已有项目并恢复译文、手工编辑、失败行和完成索引。指纹变化时
        通过 ``fingerprint_mismatch_callback`` 询问用户。
        """
        write_key = self._write_key_for(file_path, file_type, mapping_dir)
        if not self._write_locks.try_acquire(write_key):
            return False

        # P1-UX-2：TXT 跨重启恢复。在持锁前完成 IO，避免与 Coordinator 主循环耦合。
        source_fingerprint = ""
        project: TranslationProject | None = None
        restored_target_lines = list(target_lines)
        restored_manually_edited: set = set()
        restored_failed_indices: List[int] = []
        restored_completed_indices: set = set()

        if file_type == "txt" and self._project_repository is not None and source_lines:
            try:
                source_fingerprint = compute_file_fingerprint(Path(file_path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("计算源文件指纹失败 %s: %s", file_path, exc)
                source_fingerprint = ""

            if source_fingerprint:
                project_id = compute_project_id(file_path, source_fingerprint)
                try:
                    existing = self._project_repository.load(project_id)
                except ProjectCorruptError as exc:
                    logger.error("拒绝覆盖损坏项目 %s: %s", project_id, exc)
                    self._write_locks.release(write_key)
                    return False
                except Exception as exc:  # noqa: BLE001
                    logger.warning("加载项目 %s 失败: %s", project_id, exc)
                    existing = None

                if existing is not None and existing.source_fingerprint == source_fingerprint:
                    # 指纹匹配：恢复译文与行级标记
                    if len(existing.translated_lines) == len(source_lines):
                        restored_target_lines = list(existing.translated_lines)
                    restored_manually_edited = set(existing.manually_edited_indices)
                    restored_failed_indices = list(existing.failed_indices)
                    restored_completed_indices = set(existing.completed_indices)
                    project = existing
                    logger.info(
                        "TXT 任务 %s 恢复项目 %s：译文 %d 行，失败 %d 行，手工 %d 行",
                        task_id,
                        project_id,
                        sum(1 for t in restored_target_lines if t and t.strip()),
                        len(restored_failed_indices),
                        len(restored_manually_edited),
                    )
                else:
                    # 指纹不匹配或新建：检查同路径历史项目
                    self._handle_fingerprint_mismatch(
                        file_path=file_path,
                        new_fingerprint=source_fingerprint,
                        source_lines=source_lines,
                        existing=existing,
                        restored_target_lines=restored_target_lines,
                        restored_manually_edited=restored_manually_edited,
                        restored_failed_indices=restored_failed_indices,
                        restored_completed_indices=restored_completed_indices,
                    )
                    # 创建新项目记录恢复状态
                    project = self._create_new_project(
                        file_path=file_path,
                        source_fingerprint=source_fingerprint,
                        file_type=file_type,
                        mapping_dir=mapping_dir or "",
                        source_lines=source_lines,
                        target_lines=restored_target_lines,
                        manually_edited=restored_manually_edited,
                        failed_indices=restored_failed_indices,
                        completed_indices=restored_completed_indices,
                    )
            else:
                # 指纹计算失败：仍创建空项目记录，便于后续保存
                project = self._create_new_project(
                    file_path=file_path,
                    source_fingerprint="",
                    file_type=file_type,
                    mapping_dir=mapping_dir or "",
                    source_lines=source_lines,
                    target_lines=restored_target_lines,
                    manually_edited=restored_manually_edited,
                    failed_indices=restored_failed_indices,
                    completed_indices=restored_completed_indices,
                )

        with self._lock:
            if task_id in self._tasks:
                self._write_locks.release(write_key)
                return False
            slot = _TaskSlot(
                task_id=task_id,
                file_path=file_path,
                file_name=file_name,
                file_type=file_type,
                mapping_dir=mapping_dir,
                source_lines=list(source_lines),
                target_lines=restored_target_lines,
                checkpoint=CheckpointCoordinator(task_id),
                source_fingerprint=source_fingerprint,
                project=project,
                manually_edited_indices=set(restored_manually_edited),
                completed_indices=set(restored_completed_indices),
            )
            slot.failed_indices = list(restored_failed_indices)
            self._tasks[task_id] = slot
            self._task_order.append(task_id)
        self._wake_event.set()
        return True

    # ── P1-UX-2：项目仓库辅助 ──────────────────────────

    def _handle_fingerprint_mismatch(
        self,
        *,
        file_path: str,
        new_fingerprint: str,
        source_lines: List[str],
        existing: TranslationProject | None,
        restored_target_lines: List[str],
        restored_manually_edited: set,
        restored_failed_indices: List[int],
        restored_completed_indices: set,
    ) -> None:
        """P1-UX-2：处理指纹不匹配场景（源文件内容已变化）。

        - 同路径历史项目存在时，询问用户：新任务 / 尝试映射 / 放弃旧进度。
        - "map"：若行数相同，复用旧译文与行级标记。
        - "discard"：删除旧项目。
        - "new" 或无回调：不恢复任何状态。

        所有修改直接写回传入的可变集合/列表，调用方据此创建新项目。
        """
        repository = self._project_repository
        if repository is None:
            return

        if existing is not None:
            # existing 是同 ID 加载的：指纹不匹配说明 fingerprint 字段与当前不符，
            # 但 project_id 由 (path, fingerprint) 决定，所以 existing 的 fingerprint
            # 应当与 new_fingerprint 相同——此处分支理论上不可达。
            # 真正的"指纹变化"通过 find_by_source_path 检测。
            pass

        same_path_project = repository.find_by_source_path(file_path)
        if same_path_project is None:
            return
        if same_path_project.source_fingerprint == new_fingerprint:
            # 已被 find_by_project_id 覆盖，不会到这里
            return

        choice = "new"
        callback = self._fingerprint_mismatch_callback
        if callback is not None:
            try:
                result = callback(
                    {
                        "file_path": file_path,
                        "old_fingerprint": same_path_project.source_fingerprint,
                        "new_fingerprint": new_fingerprint,
                        "old_total_lines": len(same_path_project.original_lines),
                        "new_total_lines": len(source_lines),
                    }
                )
                if result in ("new", "map", "discard"):
                    choice = result
            except Exception as exc:  # noqa: BLE001
                logger.warning("指纹变化回调异常，默认按新建处理: %s", exc)

        if choice == "discard":
            try:
                repository.delete(same_path_project.project_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("删除旧项目失败: %s", exc)
            return

        if choice == "map":
            # 仅在行数相同时复用，避免错位
            if len(same_path_project.original_lines) != len(source_lines):
                logger.info(
                    "指纹变化且行数不同（旧 %d / 新 %d），不映射旧进度",
                    len(same_path_project.original_lines),
                    len(source_lines),
                )
                return
            if len(same_path_project.translated_lines) == len(source_lines):
                restored_target_lines[:] = list(same_path_project.translated_lines)
            restored_manually_edited.update(same_path_project.manually_edited_indices)
            restored_failed_indices.extend(same_path_project.failed_indices)
            restored_completed_indices.update(same_path_project.completed_indices)
            logger.info(
                "指纹变化但用户选择映射，复用旧译文 %d 行",
                sum(1 for t in restored_target_lines if t and t.strip()),
            )
        # choice == "new"：不恢复任何状态

    def _create_new_project(
        self,
        *,
        file_path: str,
        source_fingerprint: str,
        file_type: str,
        mapping_dir: str,
        source_lines: List[str],
        target_lines: List[str],
        manually_edited: set,
        failed_indices: List[int],
        completed_indices: set,
    ) -> TranslationProject | None:
        """P1-UX-2：创建新项目并保存初始状态。失败不阻塞任务添加。"""
        if self._project_repository is None:
            return None
        try:
            project = self._project_repository.create(
                source_path=file_path,
                source_fingerprint=source_fingerprint,
                file_type=file_type,
                mapping_dir=mapping_dir,
                original_lines=source_lines,
            )
            project.translated_lines = list(target_lines)
            project.manually_edited_indices = set(manually_edited)
            project.failed_indices = set(failed_indices)
            project.completed_indices = set(completed_indices)
            self._project_repository.save(project)
            return project
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建新项目失败，任务仍会继续: %s", exc)
            return None

    def remove_task(self, task_id: str) -> bool:
        """请求移除任务。实际清理在 Coordinator 线程完成。"""
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return False
            # running/pause_requested 不允许直接移除（需先取消）
            if slot.state in (
                QueueTaskState.RUNNING,
                QueueTaskState.PAUSE_REQUESTED,
                QueueTaskState.FINALIZING,
            ):
                return False
        self.submit_command("remove", task_id)
        return True

    # ── 快照接口（Tk 主线程轮询） ────────────────────────

    def get_snapshot(self) -> QueueSnapshot | None:
        """返回最新不可变快照。无任务时返回 None。"""
        with self._snapshot_lock:
            return self._latest_snapshot

    # ── 调度线程主循环 ──────────────────────────────────

    def _run(self) -> None:
        """Coordinator 主循环（§6.2 伪代码）。"""
        while True:
            try:
                # 1. 排空命令
                self._drain_commands()
                # 2. 收割完成的 Outcome
                self._drain_completed_outcomes()
                # 2.5 回填锁外 finalizer 的终态
                self._drain_finalizers()
                # 3. 应用结果 + 调度检查点
                self._apply_pending_outcomes()
                # 4. 派发新批次
                self._dispatch()
                # 5. 检查终态任务并触发检查点 flush
                self._handle_terminal_tasks()
                # 6. 发布快照
                self._publish_snapshot()
            except Exception:  # noqa: BLE001
                logger.exception("Coordinator 主循环异常")

            with self._lock:
                # 关闭中：drain 在途后退出
                if (
                    self._closed
                    and not self._in_flight
                    and not self._finalizing
                    and not any(s.pending_batches for s in self._tasks.values())
                ):
                    self._finalizer_executor.shutdown(wait=False, cancel_futures=True)
                    return
            # 等待下一个事件
            self._wait_for_events(timeout=0.5)

    def _wait_for_events(self, timeout: float) -> None:
        """等待命令、Future 完成或 limiter cooldown 到期。"""
        wait_timeout = timeout
        if self._active_limiter is not None and self._active_limiter.is_blocked():
            remaining = self._active_limiter.cooldown_remaining()
            if remaining > 0:
                wait_timeout = min(wait_timeout, remaining)
        self._wake_event.wait(timeout=max(0.0, wait_timeout))
        self._wake_event.clear()

    # ── 命令处理 ────────────────────────────────────────

    def _drain_commands(self) -> None:
        with self._lock:
            commands = list(self._commands)
            self._commands.clear()
        for cmd in commands:
            try:
                self._apply_command(cmd)
            except Exception:  # noqa: BLE001
                logger.exception("应用命令失败: %s", cmd)

    def _apply_command(self, cmd: _Command) -> None:
        if cmd.kind == "start":
            self._cmd_start_task(cmd.task_id)
        elif cmd.kind == "start_all":
            self._cmd_start_all()
        elif cmd.kind == "pause":
            self._cmd_pause_task(cmd.task_id)
        elif cmd.kind == "pause_all":
            self._cmd_pause_all()
        elif cmd.kind == "resume":
            self._cmd_resume_task(cmd.task_id)
        elif cmd.kind == "cancel":
            self._cmd_cancel_task(cmd.task_id)
        elif cmd.kind == "cancel_all":
            self._cmd_cancel_all()
        elif cmd.kind == "remove":
            self._cmd_remove_task(cmd.task_id)
        elif cmd.kind == "close":
            pass  # 由 close() 处理

    def _cmd_start_task(self, task_id: str | None) -> None:
        if task_id is None:
            return
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return
            # 从 PENDING/PAUSED/CANCELLED/ERROR/PARTIAL 都可以开始/重启。
            # 任务会在活动窗口有空位时才真正 prepare，避免大队列一次性
            # 创建 O(任务数) 的连接池、心跳线程和批次计划。
            if slot.state in (
                QueueTaskState.RUNNING,
                QueueTaskState.PREPARING,
                QueueTaskState.READY,
                QueueTaskState.PAUSE_REQUESTED,
            ):
                return
            slot.start_requested = True
            self._activate_pending_tasks_locked()

    def _cmd_start_all(self) -> None:
        with self._lock:
            for task_id in list(self._task_order):
                slot = self._tasks.get(task_id)
                if slot is None:
                    continue
                if slot.state in (
                    QueueTaskState.PENDING,
                    QueueTaskState.PAUSED,
                    QueueTaskState.CANCELLED,
                    QueueTaskState.ERROR,
                    QueueTaskState.PARTIAL,
                ):
                    slot.start_requested = True
            self._activate_pending_tasks_locked()

    def _active_task_count_locked(self) -> int:
        """Return the number of tasks which currently own runnable resources."""
        return sum(
            slot.state
            in (
                QueueTaskState.READY,
                QueueTaskState.PREPARING,
                QueueTaskState.RUNNING,
                QueueTaskState.PAUSE_REQUESTED,
            )
            for slot in self._tasks.values()
        )

    def _activate_pending_tasks_locked(self) -> None:
        """Prepare queued start requests up to ``max_active_tasks`` fairly.

        Callers hold ``_lock``.  Tasks retain insertion order so a terminal
        task releases its active window to the earliest waiting task.
        """
        max_active = max(1, self._policy.max_active_tasks)
        for task_id in self._task_order:
            if self._active_task_count_locked() >= max_active:
                return
            slot = self._tasks.get(task_id)
            if slot is None or not slot.start_requested:
                continue
            if slot.state not in (
                QueueTaskState.PENDING,
                QueueTaskState.PAUSED,
                QueueTaskState.CANCELLED,
                QueueTaskState.ERROR,
                QueueTaskState.PARTIAL,
            ):
                continue
            self._prepare_attempt_locked(slot)
            # A failed preparation is terminal and should not be retried until
            # the user explicitly starts the task again.
            if slot.state == QueueTaskState.ERROR:
                slot.start_requested = False

    def _prepare_attempt_locked(self, slot: _TaskSlot) -> None:
        """准备一次新的翻译尝试：生成 attempt_id、重建引擎、规划批次。

        在持有 ``self._lock`` 时调用。
        """
        # 关闭旧引擎（重启覆盖前必须 close，§3.5）
        self._close_engine_locked(slot)
        # 生成新 attempt_id
        slot.attempt_id = uuid.uuid4().hex[:12]
        slot.cancel_event = threading.Event()
        slot.error_message = None
        slot.failed_indices = []
        slot.progress = 0.0
        slot.completed_batch_ids = set()
        slot.in_flight_batches = {}
        slot.pending_batches = deque()
        slot.completed_lines = 0
        # P1-UX-3：任务恢复时清空旧错误（开始新一轮 attempt）
        slot.actionable_error = None
        slot.start_requested = False
        slot.finalization_scheduled = False
        slot.finalization_complete = False
        slot.final_state_hint = None
        slot.state = QueueTaskState.PREPARING

        # API client creation and context construction can allocate sockets or
        # initialize a provider runtime. Keep that work outside the coordinator
        # state lock, then validate the attempt before accepting its result.
        engine = TranslatorEngine(self._config_manager)
        slot.engine = engine
        preparation_error: Exception | None = None
        preparation_stage = "engine_init"
        run_context = None
        input_token_budget = 0
        self._lock.release()
        try:
            engine._ensure_api()
            preparation_stage = "run_context"
            run_context = engine.build_run_context()
            preparation_stage = "input_budget"
            input_token_budget = engine.compute_input_token_budget(
                self._policy.target_batch_input_tokens
            )
        except Exception as exc:  # noqa: BLE001
            preparation_error = exc
        finally:
            self._lock.acquire()

        if self._closed or slot.cancel_event.is_set() or slot.state != QueueTaskState.PREPARING:
            self._close_engine_locked(slot)
            return

        if preparation_error is not None:
            logger.error("任务 %s 准备失败: %s", slot.task_id, preparation_error)
            slot.state = QueueTaskState.ERROR
            slot.error_message = f"准备任务失败：{preparation_error}"
            # P1-UX-3：引擎初始化失败大概率是认证/配置问题，归类后 UI 显示
            actionable = classify_error(preparation_error)
            slot.actionable_error = actionable
            log_classified_error(
                preparation_error,
                actionable,
                context={"task_id": slot.task_id, "phase": preparation_stage},
            )
            self._close_engine_locked(slot)
            return

        # 注册/获取共享 Limiter
        self._get_or_create_limiter_locked()

        # 只翻译缺失行（R2-BUG-024）
        # P1-UX-2：跳过手工编辑行（不被自动任务覆盖）和显式完成行
        # （译文非空 OR 之前已成功的行）。空字符串可能是有效译文结果，
        # 所以显式 completed_indices 优先于"译文非空"判定。
        missing_indices = self._compute_missing_indices_locked(slot)
        slot.total_to_translate = len(missing_indices)

        # 空内容：直接标记完成并保存（R2-BUG-027）
        if not missing_indices:
            self._schedule_checkpoint_save_locked(slot)
            slot.state = QueueTaskState.COMPLETED
            slot.progress = 100.0
            return

        # 规划批次
        assert run_context is not None
        jobs = plan_batches(
            slot.source_lines,
            missing_indices,
            self._policy,
            run_context,
            input_token_budget,
        )
        # 填充 task_id 和 attempt_id
        filled_jobs = [
            BatchJob(
                task_id=slot.task_id,
                attempt_id=slot.attempt_id,
                batch_id=j.batch_id,
                source_indices=j.source_indices,
                source_lines=j.source_lines,
                estimated_input_tokens=j.estimated_input_tokens,
                run_context=j.run_context,
            )
            for j in jobs
        ]
        slot.pending_batches = deque(filled_jobs)
        slot.state = QueueTaskState.READY

    def _compute_missing_indices_locked(self, slot: _TaskSlot) -> List[int]:
        """P1-UX-2：计算待翻译行索引。

        跳过规则（按优先级）：
        1. 原文为空或纯空白：不翻译（保留空行）。
        2. 在 ``manually_edited_indices`` 中：用户已手工处理，不自动覆盖。
        3. 在 ``completed_indices`` 中：之前已成功翻译，不重译。

        兜底：译文非空且非空白视为完成，兼容未接入项目仓库的路径
        （如 EPUB 旧路径或外部修改过的 target_lines）。
        """
        missing: List[int] = []
        for i in range(len(slot.source_lines)):
            src = slot.source_lines[i]
            if not src or not src.strip():
                continue
            if i in slot.manually_edited_indices:
                continue
            if i in slot.completed_indices:
                continue
            if i < len(slot.target_lines):
                t = slot.target_lines[i]
                if t and t.strip():
                    # 兜底：未显式标记完成但译文非空（旧路径兼容）
                    continue
            missing.append(i)
        return missing

    def _cmd_pause_task(self, task_id: str | None) -> None:
        if task_id is None:
            return
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return
            if slot.state in (QueueTaskState.RUNNING, QueueTaskState.READY):
                slot.state = QueueTaskState.PAUSE_REQUESTED
            elif slot.state == QueueTaskState.PAUSE_REQUESTED:
                pass  # 已请求暂停

    def _cmd_pause_all(self) -> None:
        with self._lock:
            for slot in self._tasks.values():
                if slot.state in (QueueTaskState.RUNNING, QueueTaskState.READY):
                    slot.state = QueueTaskState.PAUSE_REQUESTED

    def _cmd_resume_task(self, task_id: str | None) -> None:
        if task_id is None:
            return
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return
            if slot.state == QueueTaskState.PAUSED:
                # Resume participates in the active window just like a new
                # start.  Paused tasks retain their existing batches.
                slot.start_requested = True
                self._activate_pending_tasks_locked()

    def _cmd_cancel_task(self, task_id: str | None) -> None:
        if task_id is None:
            return
        engine: TranslatorEngine | None = None
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return
            if slot.state in (QueueTaskState.COMPLETED, QueueTaskState.CANCELLED):
                return
            # 立即删除未提交 Job
            slot.pending_batches.clear()
            # 取消令牌：在途请求看到后中止
            slot.cancel_event.set()
            # bump attempt_id：旧 Outcome 一律丢弃（§9.3）
            slot.attempt_id = uuid.uuid4().hex[:12]
            # 停止引擎（关闭该任务的 API 客户端，不影响其他任务）
            engine = slot.engine
            slot.state = QueueTaskState.CANCELLED
            slot.start_requested = False
        if engine is not None:
            self._stop_engine(engine)

    def _cmd_cancel_all(self) -> None:
        engines: list[TranslatorEngine] = []
        with self._lock:
            for slot in self._tasks.values():
                if slot.state in (QueueTaskState.COMPLETED, QueueTaskState.CANCELLED):
                    continue
                slot.pending_batches.clear()
                slot.cancel_event.set()
                slot.attempt_id = uuid.uuid4().hex[:12]
                if slot.engine is not None:
                    engines.append(slot.engine)
                slot.state = QueueTaskState.CANCELLED
                slot.start_requested = False
        for engine in engines:
            self._stop_engine(engine)

    def _cmd_remove_task(self, task_id: str | None) -> None:
        if task_id is None:
            return
        checkpoint: CheckpointCoordinator | None = None
        engine: TranslatorEngine | None = None
        final_snapshot: CheckpointSnapshot | None = None
        slot: _TaskSlot | None = None
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return
            # 清理 pending
            slot.pending_batches.clear()
            slot.cancel_event.set()
            checkpoint = slot.checkpoint
            # Removing a paused/terminal task needs one final consistent copy,
            # but the subsequent flush and close must happen outside this lock.
            if checkpoint is not None:
                slot.checkpoint_generation += 1
                snapshot_data = {
                    "target_lines": list(slot.target_lines),
                    "failed_indices": list(slot.failed_indices),
                    "manually_edited_indices": set(slot.manually_edited_indices),
                    "completed_indices": set(slot.completed_indices),
                }
                save_fn = self._make_save_fn(slot, snapshot_data)
                if save_fn is not None:
                    final_snapshot = CheckpointSnapshot(
                        task_id=slot.task_id,
                        generation=slot.checkpoint_generation,
                        save_fn=save_fn,
                    )
            engine = slot.engine
            slot.engine = None
            slot.checkpoint = None
            # 从注册表移除
            self._tasks.pop(task_id, None)
            if task_id in self._task_order:
                self._task_order.remove(task_id)
        if checkpoint is not None and final_snapshot is not None:
            checkpoint.mark_dirty(final_snapshot)

        def _release_after_finalization(_future: Future) -> None:
            if slot is not None:
                self._release_write_lock(slot)
            self._wake_event.set()

        finalizer = self._finalizer_executor.submit(
            self._finalize_task_resources,
            checkpoint,
            engine,
        )
        finalizer.add_done_callback(_release_after_finalization)

    # ── 结果收割与应用 ──────────────────────────────────

    def _drain_completed_outcomes(self) -> None:
        """收割已完成的 future，构造待应用 Outcome 列表。"""
        if not self._in_flight:
            return
        done, _ = wait(tuple(self._in_flight), timeout=0.0, return_when=FIRST_COMPLETED)
        for future in done:
            meta = self._in_flight.pop(future, None)
            if meta is None:
                continue
            task_id, batch_id = meta
            # 释放 limiter 槽位（与派发时的 try_acquire 配对）
            if self._active_limiter is not None:
                self._active_limiter.release()
            try:
                outcome: BatchOutcome = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.exception("BatchJob future 异常: task=%s batch=%s", task_id, batch_id)
                # 合成失败 Outcome
                with self._lock:
                    slot = self._tasks.get(task_id)
                    attempt_id = slot.attempt_id if slot else ""
                    src_indices: Tuple[int, ...] = ()
                    src_len = 0
                    if slot is not None:
                        job = slot.in_flight_batches.pop(batch_id, None)
                        if job is not None:
                            src_indices = job.source_indices
                            src_len = len(job.source_lines)
                outcome = BatchOutcome(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    batch_id=batch_id,
                    source_indices=src_indices,
                    translated_lines=tuple(),
                    failed_relative_indices=tuple(range(src_len)),
                    retry_count=0,
                    rate_limited=False,
                    retry_after_seconds=None,
                    request_seconds=0.0,
                    error_message=f"Worker 异常: {exc}",
                )
            self._pending_outcomes.append(outcome)

    def _apply_pending_outcomes(self) -> None:
        """应用 Outcome：attempt_id 校验 + 按 source_indices 写回 + 状态更新。"""
        if not self._pending_outcomes:
            return
        outcomes = list(self._pending_outcomes)
        self._pending_outcomes.clear()
        for outcome in outcomes:
            self._apply_outcome(outcome)

    def _apply_outcome(self, outcome: BatchOutcome) -> None:
        """应用单个 Outcome（§10.1）。

        步骤：
        1. 校验 attempt_id（不匹配丢弃，§9.3）
        2. 校验返回长度和相对失败索引
        3. 通过 source_indices 写回固定位置
        4. 成功译文非空时才覆盖
        5. 更新完成数、失败数和指标
        6. 标记 dirty 触发检查点保存
        7. 状态转换
        """
        with self._lock:
            slot = self._tasks.get(outcome.task_id)
            if slot is None:
                return
            # attempt_id 校验：取消/重启后旧 Outcome 一律丢弃
            if outcome.attempt_id != slot.attempt_id:
                # 即使结果属于旧 attempt，也必须释放当前 slot 对该 batch
                # 的跟踪；否则取消后的任务会永远认为仍有 in-flight batch。
                slot.in_flight_batches.pop(outcome.batch_id, None)
                logger.debug(
                    "丢弃过期 Outcome: task=%s batch=%s (attempt %s != current %s)",
                    outcome.task_id,
                    outcome.batch_id,
                    outcome.attempt_id,
                    slot.attempt_id,
                )
                return
            # 取消的 Outcome：从 in_flight 移除，不写回
            if outcome.cancelled:
                slot.in_flight_batches.pop(outcome.batch_id, None)
                self._check_pause_completed_locked(slot)
                self._check_task_completion_locked(slot)
                return

            # 从 in_flight 移除
            job = slot.in_flight_batches.pop(outcome.batch_id, None)
            if job is None:
                return

            # 写回：按 source_indices 写入固定位置
            written = 0
            for rel_idx, abs_idx in enumerate(outcome.source_indices):
                if rel_idx >= len(outcome.translated_lines):
                    break
                translated = outcome.translated_lines[rel_idx]
                # 成功译文非空时才覆盖（§10.1）
                if translated and translated.strip() and abs_idx < len(slot.target_lines):
                    slot.target_lines[abs_idx] = translated.strip()
                    # P1-UX-2：显式标记完成（空字符串也可能是有效结果，
                    # 所以不依赖"译文非空"作为唯一完成信号）
                    slot.completed_indices.add(abs_idx)
                    written += 1

            # 失败索引（相对 -> 绝对）
            for rel_idx in outcome.failed_relative_indices:
                if rel_idx < len(outcome.source_indices):
                    abs_idx = outcome.source_indices[rel_idx]
                    if abs_idx not in slot.failed_indices:
                        slot.failed_indices.append(abs_idx)

            # 错误消息（保留最后一次）
            if outcome.error_message and not outcome.cancelled:
                slot.error_message = outcome.error_message

            # P1-UX-3：分类错误落到 slot，供 UI 显示安全文案与建议动作。
            # 保留最后一次错误（与 error_message 行为一致）；取消不算错误。
            if outcome.cancelled:
                slot.actionable_error = None
            elif outcome.error_category is not None:
                slot.actionable_error = ActionableError(
                    category=ErrorCategory(outcome.error_category),
                    safe_message=outcome.error_safe_message or slot.error_message or "",
                    recommended_action=outcome.recommended_action or "",
                    retryable=outcome.error_retryable,
                    correlation_id=outcome.correlation_id or "",
                )

            # 标记批次完成
            slot.completed_batch_ids.add(outcome.batch_id)
            slot.completed_lines += written
            self._throughput_lines += written
            self._recent_batch_seconds.append(outcome.request_seconds)

            # 更新进度
            if slot.total_to_translate > 0:
                slot.progress = min(
                    100.0,
                    (slot.completed_lines / slot.total_to_translate) * 100.0,
                )

            # 触发检查点保存（每个成功 Batch 都标记 dirty，§10.2）
            self._schedule_checkpoint_save_locked(slot)

            # 检查暂停完成 / 任务完成
            self._check_pause_completed_locked(slot)
            self._check_task_completion_locked(slot)

    def _check_pause_completed_locked(self, slot: _TaskSlot) -> None:
        """PAUSE_REQUESTED 且 in_flight==0 时转为 PAUSED（§9.1）。"""
        if slot.state == QueueTaskState.PAUSE_REQUESTED and not slot.in_flight_batches:
            slot.state = QueueTaskState.PAUSED

    def _check_task_completion_locked(self, slot: _TaskSlot) -> None:
        """所有批次完成时转换到终态（COMPLETED/PARTIAL/ERROR）。"""
        if slot.state in (
            QueueTaskState.COMPLETED,
            QueueTaskState.PARTIAL,
            QueueTaskState.ERROR,
            QueueTaskState.CANCELLED,
            QueueTaskState.PAUSED,
            QueueTaskState.PAUSE_REQUESTED,
        ):
            return
        if slot.pending_batches or slot.in_flight_batches:
            return
        # 检查点 flush 与引擎 close 可能等待磁盘、网络或线程；锁内只切换
        # 内存状态。_handle_terminal_tasks 会将资源交给独立 finalizer。
        slot.final_state_hint = (
            QueueTaskState.PARTIAL if slot.failed_indices else QueueTaskState.COMPLETED
        )
        slot.state = QueueTaskState.FINALIZING

    def _handle_terminal_tasks(self) -> None:
        """Detach terminal resources and finalize them outside the state lock."""
        work: list[tuple[str, CheckpointCoordinator | None, TranslatorEngine | None]] = []
        with self._lock:
            for slot in self._tasks.values():
                terminal = slot.state in (
                    QueueTaskState.COMPLETED,
                    QueueTaskState.PARTIAL,
                    QueueTaskState.ERROR,
                    QueueTaskState.CANCELLED,
                    QueueTaskState.FINALIZING,
                )
                if (
                    not terminal
                    or slot.finalization_scheduled
                    or slot.finalization_complete
                    or slot.in_flight_batches
                ):
                    continue
                if slot.state != QueueTaskState.FINALIZING:
                    slot.final_state_hint = slot.state
                    slot.state = QueueTaskState.FINALIZING
                slot.finalization_scheduled = True
                engine = slot.engine
                slot.engine = None
                work.append((slot.task_id, slot.checkpoint, engine))
            # Detached finalizers no longer consume an active-task slot.
            self._activate_pending_tasks_locked()

        for task_id, checkpoint, engine in work:
            future = self._finalizer_executor.submit(
                self._finalize_task_resources,
                checkpoint,
                engine,
            )
            future.add_done_callback(lambda _future: self._wake_event.set())
            self._finalizing[future] = task_id

    @staticmethod
    def _finalize_task_resources(
        checkpoint: CheckpointCoordinator | None,
        engine: TranslatorEngine | None,
    ) -> tuple[bool, str | None]:
        """Flush persistence and close runtime resources without coordinator lock."""
        checkpoint_ok = True
        checkpoint_error: str | None = None
        if checkpoint is not None:
            try:
                checkpoint_ok = checkpoint.flush_blocking(timeout=5.0)
                checkpoint_error = checkpoint.last_error
            except Exception as exc:  # noqa: BLE001
                checkpoint_ok = False
                checkpoint_error = str(exc)
            try:
                checkpoint.close()
            except Exception as exc:  # noqa: BLE001
                checkpoint_ok = False
                checkpoint_error = checkpoint_error or str(exc)
        if engine is not None:
            try:
                engine.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("队列任务引擎关闭失败: %s", exc)
        return checkpoint_ok, checkpoint_error

    def _drain_finalizers(self) -> None:
        """Apply completed finalizer results as a short in-memory transition."""
        done = [future for future in self._finalizing if future.done()]
        for future in done:
            task_id = self._finalizing.pop(future)
            try:
                checkpoint_ok, checkpoint_error = future.result()
            except Exception as exc:  # noqa: BLE001
                checkpoint_ok, checkpoint_error = False, str(exc)
            with self._lock:
                slot = self._tasks.get(task_id)
                if slot is None:
                    continue
                slot.finalization_scheduled = False
                slot.finalization_complete = True
                hint = slot.final_state_hint or QueueTaskState.COMPLETED
                if not checkpoint_ok:
                    slot.state = QueueTaskState.ERROR
                    slot.error_message = f"翻译完成但保存失败：{checkpoint_error or '未知保存错误'}"
                    checkpoint_exc = OSError(checkpoint_error or "checkpoint save failed")
                    slot.actionable_error = classify_error(checkpoint_exc)
                    log_classified_error(
                        checkpoint_exc,
                        slot.actionable_error,
                        context={"task_id": slot.task_id, "phase": "checkpoint_flush"},
                    )
                elif hint == QueueTaskState.CANCELLED:
                    slot.state = QueueTaskState.CANCELLED
                elif hint == QueueTaskState.ERROR:
                    slot.state = QueueTaskState.ERROR
                elif slot.failed_indices:
                    slot.state = QueueTaskState.PARTIAL
                    if not slot.error_message:
                        slot.error_message = f"{len(slot.failed_indices)} 行翻译失败"
                else:
                    slot.state = QueueTaskState.COMPLETED
                    slot.progress = 100.0
                slot.final_state_hint = None

    # ── 调度（round-robin + 空闲槽位借用） ──────────────

    def _dispatch(self) -> None:
        """派发新批次（§6.2 调度伪代码）。

        第一轮：每个 READY/RUNNING 任务最多取得 1 个槽位（per_task_soft_limit）。
        第二轮：没有其他竞争者时，把空闲槽位借给仍有工作的任务。
        """
        if self._active_limiter is None:
            return
        limiter = self._active_limiter
        if limiter.is_blocked():
            return  # cooldown 期间不派发

        with self._lock:
            if self._closed:
                return
            # 收集可派发任务（READY/RUNNING 且未暂停/取消）
            dispatchable: List[_TaskSlot] = []
            for task_id in self._task_order:
                slot = self._tasks.get(task_id)
                if slot is None:
                    continue
                if (
                    slot.state in (QueueTaskState.READY, QueueTaskState.RUNNING)
                    and slot.pending_batches
                    and not slot.cancel_event.is_set()
                ):
                    dispatchable.append(slot)

            if not dispatchable:
                return

            # 第一轮：round-robin，每任务最多 per_task_soft_limit 个
            soft_limit = max(1, self._policy.per_task_soft_limit)
            dispatched_any = True
            while dispatched_any:
                dispatched_any = False
                for slot in dispatchable:
                    if limiter.available_capacity <= 0:
                        return
                    in_flight_count = len(slot.in_flight_batches)
                    if in_flight_count >= soft_limit:
                        continue
                    if not slot.pending_batches:
                        continue
                    if self._try_dispatch_one_locked(slot, limiter):
                        dispatched_any = True
                        # 第一轮每个任务只派一个，然后轮到下一个
                        break
                else:
                    # 第一轮所有任务都已达到 soft_limit 或无批次
                    break

            # 第二轮：空闲槽位借用给仍有工作的任务（无其他竞争者）
            # 简化策略：若仍有容量，且只有少数任务还有 pending，借给它们
            while limiter.available_capacity > 0:
                # 找到还有 pending 且 in_flight < hard_cap 的任务
                borrower = None
                for slot in dispatchable:
                    if (
                        slot.pending_batches
                        and len(slot.in_flight_batches) < self._policy.hard_request_cap
                    ):
                        borrower = slot
                        break
                if borrower is None:
                    break
                if not self._try_dispatch_one_locked(borrower, limiter):
                    break

    def _try_dispatch_one_locked(self, slot: _TaskSlot, limiter: ProviderLimiter) -> bool:
        """尝试派发一个批次。在持有 ``self._lock`` 时调用。

        先获取 limiter 槽位，再提交到执行池。失败（无容量）时不派发。
        """
        if not slot.pending_batches:
            return False
        job = slot.pending_batches.popleft()
        # 获取 limiter 槽位（带预估 token）
        if not limiter.try_acquire(estimated_tokens=job.estimated_input_tokens):
            # 无容量：放回队首，下次再试
            slot.pending_batches.appendleft(job)
            return False
        # 首个 Job 提交后才进入 RUNNING（§9.1）
        if slot.state == QueueTaskState.READY:
            slot.state = QueueTaskState.RUNNING
        slot.in_flight_batches[job.batch_id] = job
        # 提交到执行池（锁外执行 Worker）
        engine = slot.engine
        if engine is None:
            slot.pending_batches.appendleft(job)
            slot.in_flight_batches.pop(job.batch_id, None)
            limiter.release()
            return False
        cancel_event = slot.cancel_event
        # 释放锁后提交？为简化，在锁内 submit（submit 不阻塞，只入队）
        future = self._executor.submit(execute_batch_job, job, engine, cancel_event, limiter)
        future.add_done_callback(lambda _future: self._wake_event.set())
        self._in_flight[future] = (slot.task_id, job.batch_id)
        slot.last_dispatch_at = time.monotonic()
        return True

    # ── 检查点 ──────────────────────────────────────────

    def _schedule_checkpoint_save_locked(self, slot: _TaskSlot) -> None:
        """Mark dirty without copying document state for every completed batch.

        The checkpoint coordinator debounces generations and calls
        ``_build_checkpoint_snapshot`` only once when it is ready to save.
        """
        if slot.checkpoint is None:
            return
        slot.checkpoint_generation += 1
        gen = slot.checkpoint_generation
        task_id = slot.task_id
        slot.checkpoint.mark_dirty_lazy(
            gen,
            lambda generation, task_id=task_id: self._build_checkpoint_snapshot(
                task_id,
                generation,
            ),
        )

    def _build_checkpoint_snapshot(self, task_id: str, generation: int) -> CheckpointSnapshot:
        """Copy a task's persistence state once, after checkpoint debounce expires."""
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                # The task was removed after a queued save was superseded. Its
                # explicit removal finalizer owns persistence, so this request
                # is safely a no-op.
                return CheckpointSnapshot(
                    task_id=task_id,
                    generation=generation,
                    save_fn=lambda _generation: None,
                )
            # Construct the immutable payload only when it will actually be
            # saved. This keeps hot batch completion O(changed rows).
            snapshot_data = {
                "target_lines": list(slot.target_lines),
                "failed_indices": list(slot.failed_indices),
                "manually_edited_indices": set(slot.manually_edited_indices),
                "completed_indices": set(slot.completed_indices),
            }
            save_fn = self._make_save_fn(slot, snapshot_data)
        if save_fn is None:

            def save_fn(generation: int) -> None:
                return None

        return CheckpointSnapshot(
            task_id=task_id,
            generation=generation,
            save_fn=save_fn,
        )

    def _make_save_fn(self, slot: _TaskSlot, snapshot_data: dict):
        """Construct a save function from a single immutable checkpoint payload.

        Called after debounce while the coordinator takes a short consistency
        snapshot; the returned function performs all disk I/O in the checkpoint
        worker.
        """
        if slot.file_type == "epub" and slot.mapping_dir:
            mapping_dir = slot.mapping_dir
            epub_proc = self._epub_processor
            lines_copy = list(snapshot_data["target_lines"])

            def _save_epub(generation: int) -> None:
                epub_proc.save_translations(mapping_dir, lines_copy)

            return _save_epub

        # TXT 路径：保持 make_txt_save_fn 的旧行为以复用其原子写入逻辑，
        # 再叠加项目仓库保存。
        tgt_path = Path(slot.file_path).with_name(f"{Path(slot.file_path).stem}_译文.txt")
        file_handler = self._file_handler
        target_lines_copy = list(snapshot_data["target_lines"])
        # P1-UX-2：项目状态快照
        project = slot.project
        project_repo = self._project_repository
        failed_indices_snapshot = list(snapshot_data["failed_indices"])
        manually_edited_snapshot = set(snapshot_data["manually_edited_indices"])
        completed_snapshot = set(snapshot_data["completed_indices"])

        def _save_txt_with_project(generation: int) -> None:
            # 1. 写 _译文.txt（导出/向后兼容格式）
            content = "\n".join(target_lines_copy)
            file_handler.write_file(str(tgt_path), content)
            # 2. 持久化项目状态（主恢复源）
            if project is not None and project_repo is not None:
                project.translated_lines = list(target_lines_copy)
                project.failed_indices = set(failed_indices_snapshot)
                project.manually_edited_indices = set(manually_edited_snapshot)
                project.completed_indices = set(completed_snapshot)
                project_repo.save(project)

        return _save_txt_with_project

    # ── 快照发布 ────────────────────────────────────────

    def _publish_snapshot(self) -> None:
        """构造并存储最新不可变快照。UI 轮询读取。"""
        with self._lock:
            task_snapshots: List[QueueTaskSnapshot] = []
            for task_id in self._task_order:
                slot = self._tasks.get(task_id)
                if slot is None:
                    continue
                task_snapshots.append(self._build_task_snapshot_locked(slot))
            # 聚合指标
            limiter = self._active_limiter
            if limiter is not None:
                m = limiter.metrics()
                # 吞吐：最近窗口行/分钟
                now = time.monotonic()
                window = max(0.001, now - self._throughput_window_start)
                throughput = (self._throughput_lines / window) * 60.0
                # ETA：基于最近完成批次的平均耗时
                eta: float | None = None
                remaining = sum(
                    len(s.pending_batches) + len(s.in_flight_batches)
                    for s in self._tasks.values()
                    if s.state
                    in (
                        QueueTaskState.READY,
                        QueueTaskState.RUNNING,
                        QueueTaskState.PAUSE_REQUESTED,
                    )
                )
                if remaining > 0 and self._recent_batch_seconds:
                    avg_batch = sum(self._recent_batch_seconds) / len(self._recent_batch_seconds)
                    current_cap = max(1, m["current_limit"])
                    eta = (remaining / current_cap) * avg_batch
                metrics = QueueMetricsSnapshot(
                    active_requests=m["in_flight"],
                    current_limit=m["current_limit"],
                    hard_cap=m["hard_cap"],
                    configured_max=m["configured_max"],
                    is_blocked=m["blocked"],
                    cooldown_remaining=m["cooldown_remaining"],
                    total_429=m["total_429"],
                    total_timeouts=m["total_timeouts"],
                    total_success=m["total_success"],
                    consecutive_successes=m["consecutive_successes"],
                    throughput_lines_per_minute=throughput,
                    eta_seconds=eta,
                )
            else:
                metrics = QueueMetricsSnapshot(
                    active_requests=0,
                    current_limit=self._policy.max_in_flight_requests,
                    hard_cap=self._policy.hard_request_cap,
                    configured_max=self._policy.max_in_flight_requests,
                    is_blocked=False,
                    cooldown_remaining=0.0,
                    total_429=0,
                    total_timeouts=0,
                    total_success=0,
                    consecutive_successes=0,
                    throughput_lines_per_minute=0.0,
                    eta_seconds=None,
                )
            snapshot = QueueSnapshot(
                tasks=tuple(task_snapshots),
                metrics=metrics,
            )
        with self._snapshot_lock:
            self._latest_snapshot = snapshot

    def _build_task_snapshot_locked(self, slot: _TaskSlot) -> QueueTaskSnapshot:
        state = slot.state
        ae = slot.actionable_error
        return QueueTaskSnapshot(
            task_id=slot.task_id,
            file_name=slot.file_name,
            file_type=slot.file_type,
            state=state,
            state_display=STATE_DISPLAY_MAP.get(state, state.value),
            progress=slot.progress,
            completed_lines=slot.completed_lines,
            total_lines=slot.total_to_translate,
            in_flight_batches=len(slot.in_flight_batches),
            pending_batches=len(slot.pending_batches),
            error_message=slot.error_message,
            failed_indices=tuple(slot.failed_indices),
            status=state.value,  # 兼容旧 UI
            checkpoint_dirty=(slot.checkpoint.is_dirty if slot.checkpoint else False),
            checkpoint_error=(slot.checkpoint.last_error if slot.checkpoint else None),
            # P1-UX-3：分类错误字段（None 表示无错误或未分类）
            error_category=(ae.category.value if ae else None),
            error_safe_message=(ae.safe_message if ae else None),
            recommended_action=(ae.recommended_action if ae else None),
            correlation_id=(ae.correlation_id if ae else None),
            failed_count=len(slot.failed_indices),
        )

    # ── Limiter / 引擎 / 写锁 辅助 ──────────────────────

    def _get_or_create_limiter_locked(self) -> ProviderLimiter:
        """获取或创建共享 ProviderLimiter（§8.1）。在持有 ``self._lock`` 时调用。"""
        api_config = self._config_manager.get_api_config()
        key = provider_runtime_key_for(api_config)
        limiter = self._limiter_registry.get_or_create(
            key,
            configured_max=self._policy.max_in_flight_requests,
            hard_cap=self._policy.hard_request_cap,
            rpm_limit=self._policy.rpm_limit,
            tpm_limit=self._policy.tpm_limit,
        )
        self._active_limiter = limiter
        return limiter

    def _close_engine_locked(self, slot: _TaskSlot) -> None:
        """Detach an engine under lock and close it in the finalizer executor."""
        engine = slot.engine
        slot.engine = None
        if engine is None:
            return
        future = self._finalizer_executor.submit(
            self._finalize_task_resources,
            None,
            engine,
        )
        future.add_done_callback(lambda _future: self._wake_event.set())

    def _close_engine(self, slot: _TaskSlot) -> None:
        with self._lock:
            engine = slot.engine
            slot.engine = None
        if engine is not None:
            self._finalize_task_resources(None, engine)

    @staticmethod
    def _stop_engine(engine: TranslatorEngine) -> None:
        """Stop request activity outside the coordinator state lock."""
        try:
            engine.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("队列任务引擎停止失败: %s", exc)

    def _write_key_for(self, file_path: str, file_type: str, mapping_dir: str | None) -> str:
        if file_type == "epub" and mapping_dir:
            return f"epub:{mapping_dir}"
        return f"txt:{Path(file_path).with_name(f'{Path(file_path).stem}_译文.txt')}"

    def _release_write_lock(self, slot: _TaskSlot) -> None:
        key = self._write_key_for(slot.file_path, slot.file_type, slot.mapping_dir)
        self._write_locks.release(key)

    # ── 公开查询（供 Manager 兼容层） ───────────────────

    def get_task_data(self, task_id: str) -> dict | None:
        """返回任务数据的不可变拷贝（供旧 API 兼容）。"""
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return None
            return self._task_data_from_slot_locked(slot)

    def get_all_task_data(self) -> List[dict]:
        with self._lock:
            return [
                self._task_data_from_slot_locked(self._tasks[task_id])
                for task_id in self._task_order
                if task_id in self._tasks
            ]

    @staticmethod
    def _task_data_from_slot_locked(slot: _TaskSlot) -> dict:
        """在已持有 coordinator 锁时构造旧 API 兼容视图。"""
        actionable_error = slot.actionable_error
        return {
            "task_id": slot.task_id,
            "file_path": slot.file_path,
            "file_name": slot.file_name,
            "file_type": slot.file_type,
            "mapping_dir": slot.mapping_dir,
            "status": slot.state.value,
            "progress": slot.progress,
            "source_lines": list(slot.source_lines),
            "target_lines": list(slot.target_lines),
            "error_message": slot.error_message,
            "failed_indices": list(slot.failed_indices),
            "error_category": actionable_error.category.value if actionable_error else None,
            "error_safe_message": actionable_error.safe_message if actionable_error else None,
            "recommended_action": actionable_error.recommended_action if actionable_error else None,
            "error_retryable": actionable_error.retryable if actionable_error else False,
            "correlation_id": actionable_error.correlation_id if actionable_error else None,
            "failed_count": len(slot.failed_indices),
        }

    def save_task_now(self, task_id: str) -> bool:
        """同步保存任务（详情页编辑后落盘）。"""
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None or slot.checkpoint is None:
                return False
            self._schedule_checkpoint_save_locked(slot)
        # 在锁外等待 flush
        with self._lock:
            checkpoint = slot.checkpoint if slot else None
        if checkpoint is None:
            return False
        return checkpoint.flush_blocking(timeout=5.0)

    def update_task_line(self, task_id: str, row_idx: int, new_value: str) -> bool:
        """P0-2：更新任务内部 target_lines 并标记检查点为脏。

        详情页编辑不再修改 get_task 返回的快照副本，而是通过此方法
        直接更新 Coordinator 内部 _TaskSlot.target_lines，确保后续
        save_task_now 持久化的是用户编辑后的新值。

        P1-UX-2：用户编辑过的行加入 ``manually_edited_indices``，
        防止后续"重新翻译全部"或"重试失败行"自动覆盖用户译文。
        非空译文同时加入 ``completed_indices``，空译文仅标记手工编辑
        （用户可能想清空让它重新翻译，但默认不自动重译以避免覆盖意图）。
        """
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return False
            if row_idx < 0 or row_idx >= len(slot.target_lines):
                return False
            if slot.target_lines[row_idx] == new_value and row_idx in slot.manually_edited_indices:
                return True
            slot.target_lines[row_idx] = new_value
            slot.manually_edited_indices.add(row_idx)
            if new_value and new_value.strip():
                slot.completed_indices.add(row_idx)
            else:
                # 用户清空译文：移除完成标记但保留手工编辑，
                # 下次 _compute_missing_indices_locked 会把它视为待翻译。
                slot.completed_indices.discard(row_idx)
            # 标记检查点为脏，使下次保存写入新值
            if slot.checkpoint is not None:
                slot.checkpoint_generation += 1
                self._schedule_checkpoint_save_locked(slot)
            return True
