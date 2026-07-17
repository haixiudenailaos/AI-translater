#!/usr/bin/env python3
"""PERF：自动保存协调器。

基于 generation 的保存状态机，取代旧的 ``_save_in_progress`` +
``_save_dirty_again`` 跨线程布尔标志。

详见 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §8。

设计要点：
- 保存状态只在 Tk 主线程修改（CLEAN/DIRTY/SAVE_SCHEDULED/SAVING/SAVE_FAILED）。
- 后台 worker 只做文件 I/O，通过 ``queue.SimpleQueue`` 返回 ``SaveResult``。
- worker 不调用 ``root.after()`` 或任何 Tk API。
- generation 等于 ``TranslationDocument.version``，用于判断保存是否覆盖最新版本。
- 单飞：同一时刻最多一个保存线程，期间的新编辑只增加 version，不启动并行写入。
- 保存结果回主线程后按 generation 校验决定 CLEAN/DIRTY/SAVE_FAILED。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from typing import Callable

from ..utils.logger import get_logger
from .translation_document import TranslationDocument

logger = get_logger(__name__)


# ── 不可变数据结构 ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SaveSnapshot:
    """保存快照：主线程构造，后台线程消费。

    - ``generation``：发起保存时的 ``document.version``，用于结果校验。
    - ``target_lines``：不可变 tuple，后台线程遍历期间主线程编辑不影响快照。
    """

    generation: int
    target_path: Path
    mapping_dir: Path | None
    target_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SaveResult:
    """保存结果：后台线程构造，主线程消费。

    - ``generation``：对应发起保存时的版本号。
    - ``succeeded``：是否写入成功。
    - ``elapsed_seconds``：文件 I/O 耗时（不含调度等待）。
    - ``bytes_written``：写入字节数（失败时为 0）。
    - ``error_message``：失败时的异常信息。
    """

    generation: int
    succeeded: bool
    elapsed_seconds: float
    bytes_written: int
    error_message: str | None = None


# ── 状态机 ──────────────────────────────────────────────

# 状态常量
CLEAN = "CLEAN"
DIRTY = "DIRTY"
SAVE_SCHEDULED = "SAVE_SCHEDULED"
SAVING = "SAVING"
SAVE_FAILED = "SAVE_FAILED"


class AutosaveCoordinator:
    """自动保存协调器。

    状态转换（只在主线程修改）::

        CLEAN
          -> DIRTY                  编辑发生
          -> SAVE_SCHEDULED         debounce 到期
          -> SAVING(generation=N)  worker 启动
          -> CLEAN                  persisted_generation == document.version
          -> DIRTY                 保存期间产生新版本
          -> SAVE_FAILED           写入失败

    用法::

        coordinator = AutosaveCoordinator(
            document=doc,
            file_handler=file_handler,
            epub_processor=epub_proc,
            schedule_callback=root.after,
            result_callback=on_save_result,
        )
        coordinator.mark_dirty(source="edit")       # 编辑后调用
        coordinator.flush()                          # 关闭时同步等待
    """

    def __init__(
        self,
        document: TranslationDocument,
        file_handler,
        epub_processor,
        schedule_callback: Callable[[int, Callable], object],
        result_callback: Callable[[SaveResult], None],
        cancel_callback: Callable[[object], None] | None = None,
    ) -> None:
        self._document = document
        self._file_handler = file_handler
        self._epub_processor = epub_processor
        self._schedule = schedule_callback
        self._cancel = cancel_callback
        self._on_result = result_callback

        self._state = CLEAN
        self._save_generation: int = 0
        self._save_thread: threading.Thread | None = None
        self._result_queue: SimpleQueue[SaveResult] = SimpleQueue()
        # 主线程轮询结果队列的 after_id
        self._poll_after_id: str | None = None
        # debounce 的 after_id
        self._debounce_after_id: str | None = None
        # 最大延迟的 after_id（避免普通 debounce 永远被推迟）
        self._max_delay_after_id: str | None = None
        # flush 等待事件
        self._flush_event = threading.Event()
        self._flush_event.set()  # 初始无保存，无需等待
        self._closed = False

    @property
    def state(self) -> str:
        """当前保存状态。"""
        return self._state

    @property
    def is_saving(self) -> bool:
        """是否正在保存。"""
        return self._state == SAVING

    # ── 主线程：标记 dirty 并调度 ────────────────────────

    def mark_dirty(
        self,
        *,
        source: str = "edit",
        debounce_ms: int = 1000,
        max_delay_ms: int = 5000,
    ) -> None:
        """标记文档为脏，按来源调度保存。

        - ``source="edit"``：人工编辑，debounce 1000ms，最大延迟 5000ms。
        - ``source="batch"``：批次完成，debounce 3000ms，最大延迟 10000ms。
        - ``source="flush"``：立即保存（暂停/停止/完成/显式保存/关闭）。
        """
        if self._closed:
            return

        if source == "flush":
            # 立即保存：取消 debounce，直接启动
            self._cancel_debounce()
            self._start_save()
            return

        # 非立即保存：状态至少为 DIRTY
        if self._state in (CLEAN, SAVE_FAILED):
            self._state = DIRTY
        # SAVING 期间也允许 mark_dirty（只增加 version，不启动并行保存）
        # SAVE_SCHEDULED 期间保持原状态

        # 取消旧的 debounce，重新计时
        self._cancel_debounce()
        self._debounce_after_id = self._schedule(debounce_ms, self._on_debounce_expired)

        # 设置最大延迟（避免持续编辑导致 debounce 永远推迟）
        if self._max_delay_after_id is None and max_delay_ms > 0:
            self._max_delay_after_id = self._schedule(max_delay_ms, self._on_max_delay_expired)

    def flush(self, timeout: float = 5.0) -> bool:
        """同步等待当前保存完成（关闭窗口时调用）。

        返回是否在超时前完成。超时必须记录并显示明确失败，
        不可假装已保存。

        PERF §8 D-3 修正：原实现仅 ``_flush_event.wait()``，但
        ``_flush_event`` 由 ``_handle_result`` 设置，而后者通过 Tk 调度的
        ``_poll_result`` 触发。当 ``flush`` 在主线程被调用时（典型关闭路径），
        Tk 主循环被阻塞，``_poll_result`` 无法触发，导致正在进行的保存必然
        超时。改为内联轮询结果队列（不依赖 Tk 调度）。
        """
        if self._closed:
            return self._state == CLEAN
        # 若正在保存且无 pending dirty，直接进入轮询循环
        # 若有 pending dirty / SAVE_FAILED，立即启动一轮保存（不调度 Tk 轮询，
        # 由本方法内联接管）
        if self._state in (DIRTY, SAVE_FAILED):
            self._cancel_debounce()
            self._start_save(begin_polling=False)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._state == CLEAN:
                return True
            if self._drain_result_inline():
                if self._state == CLEAN:
                    return True
                # 保存完成但保存期间产生了新版本（state=DIRTY）：再启动一轮
                if self._state == DIRTY and not self.is_saving:
                    self._start_save(begin_polling=False)
            else:
                # 队列空：短暂让出 CPU，等待 worker 产出结果
                time.sleep(0.02)

        # 超时返回前，若仍有保存进行中且协调器未关闭，重启 Tk 轮询
        # 以便主循环接管结果处理（避免结果滞留队列）
        if self.is_saving and self._poll_after_id is None and not self._closed:
            self._poll_result()
        return self._state == CLEAN

    def close(self) -> None:
        """关闭协调器，不再接受新的保存请求。"""
        self._closed = True
        self._cancel_debounce()
        # 唤醒任何等待 flush 的线程，避免 close 后死锁
        self._flush_event.set()

    # ── 主线程：debounce 回调 ────────────────────────────

    def _on_debounce_expired(self) -> None:
        if self._closed:
            return
        self._debounce_after_id = None
        if self._state in (DIRTY, SAVE_FAILED):
            self._start_save()

    def _on_max_delay_expired(self) -> None:
        if self._closed:
            return
        self._max_delay_after_id = None
        # 最大延迟到期：无论 debounce 是否完成，都启动保存
        if self._state in (DIRTY, SAVE_FAILED):
            self._cancel_debounce()
            self._start_save()

    def _cancel_debounce(self) -> None:
        # PERF §8 D-1 修正：必须实际调用 after_cancel 取消已调度回调，
        # 否则旧回调仍会在 Tk 队列中触发（即使状态检查会跳过，也会造成
        # 无谓的调度与潜在的状态紊乱）。
        if self._cancel is not None:
            if self._debounce_after_id is not None:
                try:
                    self._cancel(self._debounce_after_id)
                except Exception as exc:
                    logger.warning("取消 debounce after_id 失败: %s", exc)
            if self._max_delay_after_id is not None:
                try:
                    self._cancel(self._max_delay_after_id)
                except Exception as exc:
                    logger.warning("取消 max_delay after_id 失败: %s", exc)
        self._debounce_after_id = None
        self._max_delay_after_id = None

    # ── 主线程：启动保存 ─────────────────────────────────

    def _start_save(self, begin_polling: bool = True) -> None:
        """在主线程捕获快照，启动后台保存线程。

        :param begin_polling: 是否启动 Tk 调度的结果轮询。
            - ``True``（默认）：常规 mark_dirty / debounce 路径，由 Tk 主循环
              驱动 ``_poll_result``。
            - ``False``：``flush`` 路径，主线程被阻塞无法驱动 Tk 调度，由
              ``flush`` 内联调用 ``_drain_result_inline`` 接管结果处理。
        """
        if self._closed:
            return
        if self._state == SAVING:
            # 单飞：已有保存进行中，新编辑只增加 version
            return

        # 获取目标路径和映射目录
        # 这些由调用方通过 callable 提供，这里通过 file_handler 获取
        # 实际路径由 MainWindow 在 mark_dirty 前确保 file_importer.current_target_path 有效
        target_path = getattr(self, "_target_path", None)
        mapping_dir = getattr(self, "_mapping_dir", None)
        if target_path is None:
            return

        # 在主线程捕获不可变快照
        generation, target_lines = self._document.snapshot_targets()
        snapshot = SaveSnapshot(
            generation=generation,
            target_path=Path(str(target_path)),
            mapping_dir=Path(str(mapping_dir)) if mapping_dir else None,
            target_lines=target_lines,
        )

        self._state = SAVING
        self._save_generation = generation
        self._flush_event.clear()

        # 启动后台线程
        self._save_thread = threading.Thread(
            target=self._save_worker, args=(snapshot,), daemon=True
        )
        self._save_thread.start()

        # 开始轮询结果队列（flush 路径由其自身内联轮询，无需 Tk 调度）
        if begin_polling:
            self._poll_result()

    def set_save_paths(self, target_path, mapping_dir) -> None:
        """设置保存路径（MainWindow 在导入文件后调用）。"""
        self._target_path = target_path
        self._mapping_dir = mapping_dir

    # ── 主线程：轮询结果 ─────────────────────────────────

    def _poll_result(self) -> None:
        """在主线程轮询结果队列。"""
        if self._closed:
            return
        try:
            result = self._result_queue.get_nowait()
        except Exception:
            # 队列为空，继续轮询
            self._poll_after_id = self._schedule(50, self._poll_result)
            return

        self._handle_result(result)

    def _handle_result(self, result: SaveResult) -> None:
        """在主线程处理保存结果。"""
        self._poll_after_id = None
        self._flush_event.set()

        current_version = self._document.version

        if not result.succeeded:
            # 保存失败：保留 dirty，状态为 SAVE_FAILED
            self._state = SAVE_FAILED
            logger.error(
                "自动保存失败 (generation=%d): %s",
                result.generation,
                result.error_message,
            )
        elif result.generation < current_version:
            # 保存期间产生了新版本：状态保持 DIRTY，调度下一次保存
            self._state = DIRTY
            # 调度下一次保存（较短 debounce，因为已有积压）
            self._schedule(100, self._on_debounce_expired)
        else:
            # 保存成功且覆盖最新版本：状态变为 CLEAN
            self._state = CLEAN
            logger.debug(
                "自动保存成功 (generation=%d, %.0fms, %d bytes)",
                result.generation,
                result.elapsed_seconds * 1000,
                result.bytes_written,
            )

        # 通知主线程
        try:
            self._on_result(result)
        except Exception as exc:
            logger.warning("保存结果回调异常: %s", exc)

    def _drain_result_inline(self) -> bool:
        """从结果队列抽取并应用状态，不依赖 Tk 调度。

        供 ``flush`` 在主线程被阻塞时内联调用：直接从队列取结果并按
        generation 校验更新状态，不调用 ``_schedule`` / ``_on_result``。
        ``_on_result`` 的 UI 更新对关闭路径无意义（窗口即将销毁），
        因此省略以保持最小副作用。
        """
        try:
            result = self._result_queue.get_nowait()
        except Exception:
            return False
        self._flush_event.set()
        current_version = self._document.version
        if not result.succeeded:
            self._state = SAVE_FAILED
            logger.error(
                "自动保存失败 (generation=%d): %s",
                result.generation,
                result.error_message,
            )
        elif result.generation < current_version:
            self._state = DIRTY
        else:
            self._state = CLEAN
            logger.debug(
                "自动保存成功 (generation=%d, %.0fms, %d bytes)",
                result.generation,
                result.elapsed_seconds * 1000,
                result.bytes_written,
            )
        return True

    # ── 后台线程：文件 I/O ───────────────────────────────

    def _save_worker(self, snapshot: SaveSnapshot) -> None:
        """后台保存线程：只做文件 I/O，不调用 Tk API。

        结果通过 ``_result_queue`` 返回主线程。
        """
        start = time.monotonic()
        succeeded = False
        bytes_written = 0
        error_message: str | None = None

        try:
            content = "\n".join(snapshot.target_lines)
            self._file_handler.write_file(str(snapshot.target_path), content)
            bytes_written = len(content.encode("utf-8"))

            # P0-5：EPUB 映射保存——只有主文件和映射都成功才算成功。
            # 映射失败时保持 succeeded=False，状态机将进入 SAVE_FAILED，
            # 用户可看到保存失败提示并重试。
            if snapshot.mapping_dir:
                try:
                    self._epub_processor.save_translations(
                        str(snapshot.mapping_dir),
                        list(snapshot.target_lines),
                    )
                except Exception as exc:
                    logger.error("自动保存EPUB映射失败: %s", exc)
                    error_message = f"EPUB 映射保存失败: {exc}"
                    # P0-5：映射失败不算整体成功，保持 SAVE_FAILED 状态
                    elapsed = time.monotonic() - start
                    result = SaveResult(
                        generation=snapshot.generation,
                        succeeded=False,
                        elapsed_seconds=elapsed,
                        bytes_written=bytes_written,
                        error_message=error_message,
                    )
                    self._result_queue.put(result)
                    return

            succeeded = True
        except Exception as exc:
            error_message = str(exc)
            logger.error("自动保存失败: %s", exc)

        elapsed = time.monotonic() - start
        result = SaveResult(
            generation=snapshot.generation,
            succeeded=succeeded,
            elapsed_seconds=elapsed,
            bytes_written=bytes_written,
            error_message=error_message,
        )
        self._result_queue.put(result)
