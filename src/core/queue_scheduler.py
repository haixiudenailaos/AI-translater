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

from ..domain.errors import TranslationCancelled, TranslationRequestError
from ..utils.logger import get_logger
from ..utils.token_estimator import estimate_tokens
from .queue_checkpoint import (
    CheckpointCoordinator,
    CheckpointSnapshot,
    ExclusiveWriteLockRegistry,
    make_txt_save_fn,
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
        PENDING -> READY -> RUNNING -> COMPLETED
                       |    |
                       |    +-> PARTIAL / ERROR
                       |
                       +-> PAUSE_REQUESTED -> PAUSED -> READY
                       |
                       +-> CANCELLED
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    ERROR = "error"


# UI 友好的状态文案映射（保留与旧状态字符串兼容，便于 ConcurrentWindow 复用）
STATE_DISPLAY_MAP = {
    QueueTaskState.PENDING: "等待中",
    QueueTaskState.READY: "准备中",
    QueueTaskState.RUNNING: "翻译中",
    QueueTaskState.PAUSE_REQUESTED: "暂停中",
    QueueTaskState.PAUSED: "已暂停",
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


@dataclass(frozen=True, slots=True)
class QueueTaskSnapshot:
    """任务级不可变快照（供 UI 轮询，§11.2）。"""

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
        )
    except Exception as exc:  # noqa: BLE001 - Worker 边界必须兜底
        request_seconds = time.monotonic() - started_at
        logger.exception("BatchJob 执行未预期异常: task=%s batch=%s", job.task_id, job.batch_id)
        limiter.record_timeout()
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
    ) -> None:
        self._config_manager = config_manager
        self._policy = policy
        self._file_handler = file_handler
        self._epub_processor = epub_processor
        self._app_paths = app_paths

        self._lock = threading.Lock()
        self._tasks: Dict[str, _TaskSlot] = {}
        self._task_order: List[str] = []  # 添加顺序（round-robin 基准）
        self._closed = False

        # 命令队列（Tk 主线程 -> Coordinator）
        self._commands: deque[_Command] = deque()
        self._command_event = threading.Event()

        # 待应用的 Outcome（从 future 收割后暂存，下一步统一应用）
        self._pending_outcomes: deque[BatchOutcome] = deque()

        # 全局执行池：max_workers = hard_cap，硬上限不可突破（§6.1）
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, policy.hard_request_cap),
            thread_name_prefix="queue-batch",
        )
        self._in_flight: Dict[Future, Tuple[str, int]] = {}  # future -> (task_id, batch_id)

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
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # 取消所有未提交的 pending 批次
            for slot in self._tasks.values():
                slot.pending_batches.clear()
                if slot.engine is not None:
                    try:
                        slot.engine.stop()
                    except Exception:
                        pass
            self._command_event.set()
        # 等待调度线程退出（它会 drain 在途并关闭引擎）
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=10.0)
        # 兜底关闭执行池
        self._executor.shutdown(wait=False, cancel_futures=True)
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
        self._command_event.set()

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
        """注册新任务到 Coordinator。返回是否成功（同路径独占锁失败返回 False）。"""
        write_key = self._write_key_for(file_path, file_type, mapping_dir)
        if not self._write_locks.try_acquire(write_key):
            return False
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
                target_lines=list(target_lines),
                checkpoint=CheckpointCoordinator(task_id),
            )
            self._tasks[task_id] = slot
            self._task_order.append(task_id)
        return True

    def remove_task(self, task_id: str) -> bool:
        """请求移除任务。实际清理在 Coordinator 线程完成。"""
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return False
            # running/pause_requested 不允许直接移除（需先取消）
            if slot.state in (QueueTaskState.RUNNING, QueueTaskState.PAUSE_REQUESTED):
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
                    and not any(s.pending_batches for s in self._tasks.values())
                ):
                    return
            # 等待下一个事件
            self._wait_for_events(timeout=0.1)

    def _wait_for_events(self, timeout: float) -> None:
        """等待命令、未来完成或 limiter 容量。"""
        # 命令事件
        if self._command_event.wait(timeout=timeout):
            self._command_event.clear()
            return
        # 检查在途 future 是否有完成（非阻塞）
        if self._in_flight:
            done, _ = wait(tuple(self._in_flight), timeout=0.0, return_when=FIRST_COMPLETED)
            if done:
                return
        # 检查 limiter cooldown 是否结束
        if self._active_limiter is not None and self._active_limiter.is_blocked():
            remaining = self._active_limiter.cooldown_remaining()
            if remaining > 0:
                time.sleep(min(remaining, timeout))

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
            # 从 PENDING/PAUSED/CANCELLED/ERROR/PARTIAL 都可以开始/重启
            if slot.state in (
                QueueTaskState.RUNNING,
                QueueTaskState.READY,
                QueueTaskState.PAUSE_REQUESTED,
            ):
                return
            self._prepare_attempt_locked(slot)

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
                    self._prepare_attempt_locked(slot)

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

        # 创建引擎并初始化 API
        engine = TranslatorEngine(self._config_manager)
        slot.engine = engine
        try:
            engine._ensure_api()
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务 %s 引擎初始化失败", slot.task_id)
            slot.state = QueueTaskState.ERROR
            slot.error_message = f"引擎初始化失败：{exc}"
            self._close_engine_locked(slot)
            return

        # 注册/获取共享 Limiter
        self._get_or_create_limiter_locked()

        # 构造 run_context
        try:
            run_context = engine.build_run_context()
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务 %s 构造运行上下文失败", slot.task_id)
            slot.state = QueueTaskState.ERROR
            slot.error_message = f"构造运行上下文失败：{exc}"
            self._close_engine_locked(slot)
            return

        # 计算输入 token 预算
        input_token_budget = engine.compute_input_token_budget(
            self._policy.target_batch_input_tokens
        )

        # 只翻译缺失行（R2-BUG-024）
        missing_indices = [
            i
            for i, t in enumerate(slot.target_lines)
            if not t.strip() and i < len(slot.source_lines) and slot.source_lines[i].strip()
        ]
        slot.total_to_translate = len(missing_indices)

        # 空内容：直接标记完成并保存（R2-BUG-027）
        if not missing_indices:
            self._schedule_checkpoint_save_locked(slot)
            slot.state = QueueTaskState.COMPLETED
            slot.progress = 100.0
            return

        # 规划批次
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
                # 恢复：保持同一 attempt_id，继续派发剩余批次
                slot.state = QueueTaskState.READY

    def _cmd_cancel_task(self, task_id: str | None) -> None:
        if task_id is None:
            return
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
            if slot.engine is not None:
                try:
                    slot.engine.stop()
                except Exception:
                    pass
            slot.state = QueueTaskState.CANCELLED

    def _cmd_cancel_all(self) -> None:
        with self._lock:
            for slot in self._tasks.values():
                if slot.state in (QueueTaskState.COMPLETED, QueueTaskState.CANCELLED):
                    continue
                slot.pending_batches.clear()
                slot.cancel_event.set()
                slot.attempt_id = uuid.uuid4().hex[:12]
                if slot.engine is not None:
                    try:
                        slot.engine.stop()
                    except Exception:
                        pass
                slot.state = QueueTaskState.CANCELLED

    def _cmd_remove_task(self, task_id: str | None) -> None:
        if task_id is None:
            return
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return
            # 清理 pending
            slot.pending_batches.clear()
            slot.cancel_event.set()
            # flush 检查点（保留已成功批次）
            if slot.checkpoint is not None:
                try:
                    slot.checkpoint.flush_blocking(timeout=3.0)
                    slot.checkpoint.close()
                except Exception:
                    pass
            # 关闭引擎
            self._close_engine_locked(slot)
            # 从注册表移除
            self._tasks.pop(task_id, None)
            if task_id in self._task_order:
                self._task_order.remove(task_id)
        # 释放写锁
        self._release_write_lock(slot)

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
            # flush 检查点：暂停确认后保存最新结果
            if slot.checkpoint is not None:
                try:
                    slot.checkpoint.flush_blocking(timeout=3.0)
                except Exception:
                    pass

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
        # 所有批次结束
        # 先 flush 检查点（保存失败保留 dirty，不假装完成）
        checkpoint_ok = True
        if slot.checkpoint is not None:
            try:
                checkpoint_ok = slot.checkpoint.flush_blocking(timeout=5.0)
            except Exception:
                checkpoint_ok = False
        # 判定终态
        if slot.failed_indices:
            slot.state = QueueTaskState.PARTIAL
            if not slot.error_message:
                slot.error_message = f"{len(slot.failed_indices)} 行翻译失败"
        else:
            slot.state = QueueTaskState.COMPLETED
            slot.progress = 100.0
        # 检查点保存失败：降级为 ERROR，保留 dirty
        if not checkpoint_ok and slot.checkpoint is not None and slot.checkpoint.has_error:
            slot.state = QueueTaskState.ERROR
            slot.error_message = f"翻译完成但保存失败：{slot.checkpoint.last_error}"

    def _handle_terminal_tasks(self) -> None:
        """终态任务的清理：关闭引擎、释放资源（保留任务记录供 UI 查看）。"""
        with self._lock:
            for slot in self._tasks.values():
                if (
                    slot.state
                    in (
                        QueueTaskState.COMPLETED,
                        QueueTaskState.PARTIAL,
                        QueueTaskState.ERROR,
                        QueueTaskState.CANCELLED,
                    )
                    and slot.engine is not None
                ):
                    self._close_engine_locked(slot)

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
        cancel_event = slot.cancel_event
        # 释放锁后提交？为简化，在锁内 submit（submit 不阻塞，只入队）
        future = self._executor.submit(execute_batch_job, job, engine, cancel_event, limiter)
        self._in_flight[future] = (slot.task_id, job.batch_id)
        slot.last_dispatch_at = time.monotonic()
        return True

    # ── 检查点 ──────────────────────────────────────────

    def _schedule_checkpoint_save_locked(self, slot: _TaskSlot) -> None:
        """标记 dirty 并提交检查点快照（§10.2）。在持有 ``self._lock`` 时调用。"""
        if slot.checkpoint is None:
            return
        slot.checkpoint_generation += 1
        gen = slot.checkpoint_generation
        # 构造不可变保存快照（拷贝 target_lines）
        target_lines_copy = list(slot.target_lines)
        save_fn = self._make_save_fn(slot, target_lines_copy)
        if save_fn is None:
            return
        snapshot = CheckpointSnapshot(
            task_id=slot.task_id,
            generation=gen,
            save_fn=save_fn,
        )
        slot.checkpoint.mark_dirty(snapshot)

    def _make_save_fn(self, slot: _TaskSlot, target_lines: List[str]):
        """构造检查点保存函数（TXT / EPUB）。"""
        if slot.file_type == "epub" and slot.mapping_dir:
            mapping_dir = slot.mapping_dir
            epub_proc = self._epub_processor
            lines_copy = list(target_lines)

            def _save_epub(generation: int) -> None:
                epub_proc.save_translations(mapping_dir, lines_copy)

            return _save_epub
        else:
            tgt_path = Path(slot.file_path).with_name(f"{Path(slot.file_path).stem}_译文.txt")
            return make_txt_save_fn(self._file_handler, tgt_path, target_lines)

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
        """关闭任务引擎（§3.5）。在持有 ``self._lock`` 时调用。"""
        if slot.engine is not None:
            try:
                slot.engine.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("任务 %s 引擎关闭失败: %s", slot.task_id, exc)
            slot.engine = None

    def _close_engine(self, slot: _TaskSlot) -> None:
        with self._lock:
            self._close_engine_locked(slot)

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
            }

    def get_all_task_data(self) -> List[dict]:
        with self._lock:
            return [self.get_task_data(tid) for tid in self._task_order if tid in self._tasks]

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
        """
        with self._lock:
            slot = self._tasks.get(task_id)
            if slot is None:
                return False
            if row_idx < 0 or row_idx >= len(slot.target_lines):
                return False
            if slot.target_lines[row_idx] == new_value:
                return True
            slot.target_lines[row_idx] = new_value
            # 标记检查点为脏，使下次保存写入新值
            if slot.checkpoint is not None:
                slot.checkpoint_generation += 1
                self._schedule_checkpoint_save_locked(slot)
            return True
