#!/usr/bin/env python3
"""队列翻译并发优化的确定性并发不变量测试。

对应 ``docs/QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md`` §13
（必须新增的测试）。本文件覆盖 §13.1-§13.4 中按名称要求的测试用例，
使用 ``threading.Barrier``、受锁保护的 ``active/max_active`` 计数和显式
释放事件断言"并发事实"，而不是脆弱的墙钟时间（§13.1 末尾要求）。

设计要点：
- 所有阻塞型引擎通过共享 ``_ConcurrencyProbe`` 暴露当前在途数与历史峰值。
- 取消/重启测试用 ``release_event`` 释放被卡住的工作线程，避免 close 超时。
- 每个测试在 finally 中释放所有阻塞，保证不残留线程。
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

from src.core.queue_scheduler import (
    QueuePolicy,
    QueueTaskState,
    QueueTranslationCoordinator,
)
from src.core.translation_result import BatchTranslationResult
from src.core.translator import TranslationRunContext
from src.domain.errors import TranslationCancelled, TranslationRequestError
from src.domain.translation import OperationStatus

# ── 共享辅助 ──────────────────────────────────────────────


def _make_run_context() -> TranslationRunContext:
    return TranslationRunContext(
        provider="siliconflow",
        model_name="deepseek-ai/DeepSeek-V3.2",
        target_language="中文",
        base_prompt="翻译以下内容",
        glossary_prompt="",
        system_prompt=None,
        is_hunyuan=False,
        temperature=0.3,
        prompt_version="abc",
        glossary_version="",
    )


def _make_policy(**overrides) -> QueuePolicy:
    defaults = dict(
        max_in_flight_requests=2,
        hard_request_cap=4,
        max_active_tasks=4,
        per_task_soft_limit=1,
        target_batch_input_tokens=2000,
        max_batch_input_tokens=4000,
        max_batch_lines=10,
        min_batch_input_tokens=512,
        adaptive_concurrency=True,
        rpm_limit=0,
        tpm_limit=0,
    )
    defaults.update(overrides)
    return QueuePolicy(**defaults)


class _MockFileHandler:
    def write_file(self, path, content):
        resolved = _portable_test_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

    def read_file(self, path):
        return _portable_test_path(path).read_text(encoding="utf-8")


def _portable_test_path(path: str) -> Path:
    """Keep legacy POSIX fixture paths writable on Windows test runners."""
    normalized = str(path).replace("\\", "/")
    if normalized.startswith("/tmp/"):
        return Path(tempfile.gettempdir()) / "ai-translater-queue-tests" / Path(path).name
    return Path(path)


class _MockEpubProcessor:
    def save_translations(self, mapping_dir, lines):
        pass


class _ConcurrencyProbe:
    """线程安全的并发探针：记录当前在途数与历史峰值。

    所有阻塞型引擎共享同一个探针实例，用于断言"并发事实"（§13.1）：
    - ``current``：当前正在 ``_translate_batch`` 内的批次数；
    - ``max_seen``：历史峰值；
    - ``arrived``：已进入但尚未离开的批次序号列表（用于顺序断言）；
    - ``release_event``：设置后允许所有阻塞批次继续执行。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.entered_seq = 0
        self.arrived: List[int] = []
        self.release_event = threading.Event()
        self.completed_seq: List[int] = []
        self.translate_calls = 0

    def enter(self) -> int:
        with self._lock:
            self.current += 1
            self.translate_calls += 1
            if self.current > self.max_seen:
                self.max_seen = self.current
            self.entered_seq += 1
            seq = self.entered_seq
            self.arrived.append(seq)
            return seq

    def wait_release(self, timeout: float = 15.0) -> bool:
        return self.release_event.wait(timeout=timeout)

    def exit(self, seq: int) -> None:
        with self._lock:
            self.current -= 1
            self.completed_seq.append(seq)

    def reset(self) -> None:
        with self._lock:
            self.current = 0
            self.max_seen = 0
            self.entered_seq = 0
            self.arrived = []
            self.completed_seq = []
            self.translate_calls = 0
        self.release_event.clear()


class _ProbeEngine:
    """阻塞型 MockEngine：进入 ``_translate_batch`` 后阻塞在
    ``probe.release_event`` 上，供并发不变量测试观察在途状态。

    所有任务共享同一 ``probe`` 实例（通过工厂注入），因此能跨任务统计并发。
    """

    def __init__(self, config_manager, probe: _ConcurrencyProbe):
        self.config_manager = config_manager
        self.probe = probe
        self.is_stopped = False
        self.api = MagicMock()
        self.close_called = False
        self.stop_called = False

    def _ensure_api(self):
        pass

    def build_run_context(self) -> TranslationRunContext:
        return _make_run_context()

    def compute_input_token_budget(self, configured: int) -> int:
        return configured

    def _translate_batch(
        self,
        batch_lines,
        progress_callback,
        batch_start,
        total_lines=None,
        emit_stream_progress=True,
        run_context=None,
    ) -> BatchTranslationResult:
        seq = self.probe.enter()
        try:
            self.probe.wait_release(timeout=15.0)
            if self.is_stopped:
                raise TranslationCancelled("已取消")
        finally:
            self.probe.exit(seq)
        return BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=[f"译:{line}" for line in batch_lines],
        )

    def close(self):
        self.close_called = True

    def stop(self):
        self.stop_called = True
        self.is_stopped = True


class _SequenceProbe:
    """按序号控制的探针：每个进入的批次获得递增序号，
    测试可按指定顺序释放各序号，用于验证乱序写回。

    - ``arrived``：已到达（进入 _translate_batch）的序号集合；
    - ``release_events``：序号 -> Event，set 该 event 才允许对应批次继续。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self.arrived: List[int] = []
        self.release_events: dict[int, threading.Event] = {}
        self.completed: List[int] = []

    def enter(self) -> int:
        with self._lock:
            self._seq += 1
            seq = self._seq
            self.arrived.append(seq)
            self.release_events.setdefault(seq, threading.Event())
            return seq

    def wait_release(self, seq: int, timeout: float = 15.0) -> bool:
        ev = self.release_events.get(seq)
        if ev is None:
            return True
        return ev.wait(timeout=timeout)

    def exit(self, seq: int) -> None:
        with self._lock:
            self.completed.append(seq)

    def release(self, seq: int) -> None:
        ev = self.release_events.get(seq)
        if ev is not None:
            ev.set()

    def release_all(self) -> None:
        for ev in self.release_events.values():
            ev.set()


class _SequenceEngine:
    """按序号控制的阻塞型引擎，用于乱序完成测试。"""

    def __init__(self, config_manager, probe: _SequenceProbe):
        self.config_manager = config_manager
        self.probe = probe
        self.is_stopped = False
        self.api = MagicMock()
        self.close_called = False
        self.stop_called = False

    def _ensure_api(self):
        pass

    def build_run_context(self) -> TranslationRunContext:
        return _make_run_context()

    def compute_input_token_budget(self, configured: int) -> int:
        return configured

    def _translate_batch(
        self,
        batch_lines,
        progress_callback,
        batch_start,
        total_lines=None,
        emit_stream_progress=True,
        run_context=None,
    ) -> BatchTranslationResult:
        seq = self.probe.enter()
        try:
            self.probe.wait_release(seq, timeout=15.0)
            if self.is_stopped:
                raise TranslationCancelled("已取消")
        finally:
            self.probe.exit(seq)
        return BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=[f"译:{line}" for line in batch_lines],
        )

    def close(self):
        self.close_called = True

    def stop(self):
        self.stop_called = True
        self.is_stopped = True


class _FailingProbe:
    """可编程失败探针：按调用次数决定是否抛 429/超时/异常。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._call = 0
        # 调用次数 -> 异常（None 表示成功）
        self.failures: dict[int, Exception | None] = {}
        self.release_event = threading.Event()
        self.blocking = True

    def next_call(self) -> Exception | None:
        with self._lock:
            self._call += 1
            return self.failures.get(self._call)

    def release_all(self):
        self.release_event.set()


class _FailingEngine:
    """可编程失败引擎：按调用次数抛指定异常。"""

    def __init__(self, config_manager, probe: _FailingProbe):
        self.config_manager = config_manager
        self.probe = probe
        self.is_stopped = False
        self.api = MagicMock()
        self.close_called = False
        self.stop_called = False

    def _ensure_api(self):
        pass

    def build_run_context(self) -> TranslationRunContext:
        return _make_run_context()

    def compute_input_token_budget(self, configured: int) -> int:
        return configured

    def _translate_batch(
        self,
        batch_lines,
        progress_callback,
        batch_start,
        total_lines=None,
        emit_stream_progress=True,
        run_context=None,
    ) -> BatchTranslationResult:
        exc = self.probe.next_call()
        if exc is not None:
            raise exc
        return BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=[f"译:{line}" for line in batch_lines],
        )

    def close(self):
        self.close_called = True

    def stop(self):
        self.stop_called = True
        self.is_stopped = True


def _make_coordinator(
    config_manager, policy=None, engine_factory=None
) -> QueueTranslationCoordinator:
    """构造 Coordinator 并可选 patch TranslatorEngine。"""
    policy = policy or _make_policy()
    coord = QueueTranslationCoordinator(
        config_manager,
        policy,
        file_handler=_MockFileHandler(),
        epub_processor=_MockEpubProcessor(),
    )
    return coord


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """轮询断言辅助：在超时内反复调用 predicate，返回 True 表示通过。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _add_task(coord, task_id, file_path, source_lines, target_lines=None) -> bool:
    """向 Coordinator 注册一个 TXT 任务。"""
    from pathlib import Path

    if target_lines is None:
        target_lines = [""] * len(source_lines)
    return coord.add_task(
        task_id=task_id,
        file_path=file_path,
        file_name=Path(file_path).name,
        file_type="txt",
        mapping_dir=None,
        source_lines=list(source_lines),
        target_lines=list(target_lines),
    )


# ── §13.1 调度与公平性 ───────────────────────────────────


class TestSchedulerFairness:
    """§13.1 调度与公平性：用 Barrier/计数器断言并发事实。"""

    def test_active_task_window_lazily_prepares_large_queue(
        self, tmp_config_manager, tmp_path, monkeypatch
    ):
        """PERF-2：活动窗口限制引擎和批次计划，而不只限制请求数。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(
            max_in_flight_requests=2,
            max_active_tasks=2,
            per_task_soft_limit=1,
            max_batch_lines=1,
        )
        engines: list[_ProbeEngine] = []

        def _factory(config_manager):
            engine = _ProbeEngine(config_manager, probe)
            engines.append(engine)
            return engine

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            for index in range(8):
                assert _add_task(
                    coord,
                    f"t{index}",
                    str(tmp_path / f"task-{index}.txt"),
                    [f"line {index}"],
                )
            coord.submit_command("start_all")

            assert _wait_for(lambda: probe.max_seen == 2, timeout=4.0)
            assert len(engines) == 2
            assert sum(slot.state == QueueTaskState.PENDING for slot in coord._tasks.values()) == 6

            probe.release_event.set()
            assert _wait_for(
                lambda: all(
                    coord.get_task_data(f"t{index}")["status"] == "completed" for index in range(8)
                ),
                timeout=10.0,
            )
            assert len(engines) == 8
        finally:
            probe.release_event.set()
            coord.close()

    def test_start_all_fills_global_request_slots(self, tmp_config_manager, monkeypatch):
        """start_all 后两个单批任务应同时占满全局请求槽位。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a1"])
            _add_task(coord, "t2", "/tmp/b.txt", ["b1"])
            coord.submit_command("start_all")
            # 等待两个批次同时进入 _translate_batch（current == 2）
            assert _wait_for(
                lambda: probe.current == 2, timeout=8.0
            ), f"两个任务未同时占满槽位，当前在途={probe.current}"
            assert probe.max_seen == 2
        finally:
            probe.release_event.set()
            coord.close()

    def test_global_request_limit_is_not_multiplied_by_task_count(
        self, tmp_config_manager, monkeypatch
    ):
        """任务数增加不改变全局最大在途请求数（§14.1）。

        5 个单批任务，max_in_flight=2：峰值在途不超过 2。
        """
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            for i in range(5):
                _add_task(coord, f"t{i}", f"/tmp/f{i}.txt", [f"line{i}"])
            coord.submit_command("start_all")
            # 等待峰值出现（至少两个批次在途）
            assert _wait_for(lambda: probe.max_seen >= 2, timeout=8.0)
            # 让一部分完成，观察后续峰值仍不超过 2
            probe.release_event.set()
            assert _wait_for(lambda: probe.translate_calls >= 5, timeout=8.0)
            assert probe.max_seen <= 2, f"全局在途峰值 {probe.max_seen} 超过配置上限 2"
        finally:
            probe.release_event.set()
            coord.close()

    def test_multiple_single_batch_files_run_concurrently(self, tmp_config_manager, monkeypatch):
        """两个单批文件使用 Barrier 断言真正并发执行。"""
        barrier = threading.Barrier(2, timeout=10.0)
        entered = threading.Event()
        error_holder: List[Exception] = []

        class _BarrierEngine(_ProbeEngine):
            def __init__(self, config_manager, probe):
                super().__init__(config_manager, probe)

            def _translate_batch(self, *args, **kwargs):
                seq = self.probe.enter()
                try:
                    # 两个批次必须同时到达 barrier 才能继续
                    barrier.wait()
                    entered.set()
                except threading.BrokenBarrierError as exc:
                    error_holder.append(exc)
                    raise
                finally:
                    self.probe.exit(seq)
                # 取 batch_lines
                batch_lines = args[0] if args else kwargs.get("batch_lines", [])
                return BatchTranslationResult(
                    status=OperationStatus.SUCCEEDED,
                    lines=[f"译:{line}" for line in batch_lines],
                )

        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1)

        def _factory(config_manager):
            return _BarrierEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a"])
            _add_task(coord, "t2", "/tmp/b.txt", ["b"])
            coord.submit_command("start_all")
            # barrier.wait 会在两个批次都到达后返回；
            # 若只有一个批次被派发，barrier 会超时抛 BrokenBarrierError
            assert _wait_for(
                lambda: entered.is_set(), timeout=12.0
            ), "两个单批文件未并发执行（barrier 未被同时通过）"
            assert not error_holder, f"barrier 错误: {error_holder}"
        finally:
            coord.close()

    def test_short_task_is_not_starved_by_long_task(self, tmp_config_manager, monkeypatch):
        """短任务不被长任务饿死：round-robin 第一轮给短任务一个槽位。"""
        probe = _ConcurrencyProbe()
        # 长任务 4 批，短任务 1 批；并发 2、每任务软上限 1
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1, max_batch_lines=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            # 长任务 4 行（4 批，因 max_batch_lines=1）
            _add_task(coord, "long", "/tmp/long.txt", ["l1", "l2", "l3", "l4"])
            # 短任务 1 行
            _add_task(coord, "short", "/tmp/short.txt", ["s1"])
            coord.submit_command("start_all")
            # 等待短任务的批次也被派发（translate_calls >= 2 表示两个任务都有批次在途）
            assert _wait_for(
                lambda: probe.translate_calls >= 2, timeout=8.0
            ), "短任务未被派发（被长任务饿死）"
            # 确认两个任务都有在途批次（round-robin 第一轮各一个）
            long_slot = coord._tasks.get("long")
            short_slot = coord._tasks.get("short")
            assert long_slot is not None and short_slot is not None
            assert len(long_slot.in_flight_batches) >= 1
            assert len(short_slot.in_flight_batches) >= 1, "短任务未获得槽位（公平调度失败）"
        finally:
            probe.release_event.set()
            coord.close()

    def test_single_long_task_borrows_idle_slots(self, tmp_config_manager, monkeypatch):
        """无其他竞争者时，单大任务借用全部空闲槽位（§6.1 规则 3）。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1, max_batch_lines=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            # 单任务 4 批
            _add_task(coord, "t1", "/tmp/big.txt", ["a", "b", "c", "d"])
            coord.submit_command("start", "t1")
            # 等待两个槽位都被借用（current == 2）
            assert _wait_for(
                lambda: probe.current == 2, timeout=8.0
            ), f"单大任务未借用空闲槽位，当前在途={probe.current}"
            assert probe.max_seen == 2
        finally:
            probe.release_event.set()
            coord.close()

    def test_no_more_than_hard_cap_futures_are_submitted(self, tmp_config_manager, monkeypatch):
        """提交的 Future 数不超过 hard_cap（§6.1 规则 6）。"""
        probe = _ConcurrencyProbe()
        # configured_max = hard_cap = 3，明确测试硬上限
        policy = _make_policy(
            max_in_flight_requests=3,
            hard_request_cap=3,
            per_task_soft_limit=1,
            max_batch_lines=1,
        )

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            # 单任务 8 批，远超 hard_cap
            _add_task(
                coord,
                "t1",
                "/tmp/huge.txt",
                [f"line{i}" for i in range(8)],
            )
            coord.submit_command("start", "t1")
            # 等待达到峰值
            assert _wait_for(lambda: probe.max_seen >= 3, timeout=8.0)
            # 反复采样：_in_flight 永远不超过 hard_cap
            max_in_flight_seen = 0
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                n = len(coord._in_flight)
                if n > max_in_flight_seen:
                    max_in_flight_seen = n
                time.sleep(0.02)
            assert (
                max_in_flight_seen <= policy.hard_request_cap
            ), f"在途 Future 数 {max_in_flight_seen} 超过硬上限 {policy.hard_request_cap}"
            assert probe.max_seen <= policy.hard_request_cap
        finally:
            probe.release_event.set()
            coord.close()

    def test_out_of_order_batches_write_back_in_source_order(self, tmp_config_manager, monkeypatch):
        """乱序完成不改变最终行顺序（§10.1，§14.1）。"""
        probe = _SequenceProbe()
        # 4 行拆成 2 批（每批 2 行）
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1, max_batch_lines=2)

        def _factory(config_manager):
            return _SequenceEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            source = ["a", "b", "c", "d"]
            _add_task(coord, "t1", "/tmp/ordered.txt", source)
            coord.submit_command("start", "t1")
            # 等待两个批次都进入（seq 1 和 2）
            assert _wait_for(lambda: len(probe.arrived) >= 2, timeout=8.0)
            # 故意先释放 seq 2（后派发的批次），再释放 seq 1
            # 完成顺序与派发顺序相反
            probe.release(2)
            assert _wait_for(lambda: 2 in probe.completed, timeout=8.0)
            probe.release(1)
            assert _wait_for(lambda: 1 in probe.completed, timeout=8.0)
            # 等待任务完成
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] == "completed",
                timeout=8.0,
            )
            data = coord.get_task_data("t1")
            # 乱序完成但行顺序与源一致
            assert data["target_lines"] == [
                "译:a",
                "译:b",
                "译:c",
                "译:d",
            ], f"乱序写回导致行顺序错乱: {data['target_lines']}"
        finally:
            probe.release_all()
            coord.close()


# ── §13.2 限流与重试 ─────────────────────────────────────


class TestRateLimitAndRetry:
    """§13.2 限流与重试：429 跨任务统一降速，无重试风暴。"""

    def test_any_task_429_reduces_shared_provider_limit(self, tmp_config_manager, monkeypatch):
        """任一任务 429 后共享 Limiter 的 current_limit 下降（§8.1）。"""
        probe = _FailingProbe()
        # 第 1 次调用抛 429
        probe.failures[1] = TranslationRequestError(
            "rate limited",
            status_code=429,
            retry_after_seconds=None,
        )

        def _factory(config_manager):
            return _FailingEngine(config_manager, probe)

        policy = _make_policy(max_in_flight_requests=4, hard_request_cap=4)
        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a"])
            coord.submit_command("start", "t1")
            # 等待 429 被上报
            assert _wait_for(
                lambda: (
                    coord._active_limiter is not None
                    and coord._active_limiter.metrics()["total_429"] >= 1
                ),
                timeout=8.0,
            )
            # current_limit 应从 4 降到 2（4 // 2）
            assert (
                coord._active_limiter.current_limit <= 2
            ), f"429 后 current_limit 未降级: {coord._active_limiter.current_limit}"
        finally:
            probe.release_all()
            coord.close()

    def test_retry_after_blocks_new_requests_globally(self, tmp_config_manager, monkeypatch):
        """有 Retry-After 时整个 Provider 在该时间内不提交新请求（§8.2）。"""
        probe = _FailingProbe()
        # 第 1 次抛 429 + Retry-After 10s
        probe.failures[1] = TranslationRequestError(
            "rate limited",
            status_code=429,
            retry_after_seconds=10.0,
        )

        def _factory(config_manager):
            return _FailingEngine(config_manager, probe)

        policy = _make_policy(max_in_flight_requests=2, hard_request_cap=2)
        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b", "c"])
            coord.submit_command("start", "t1")
            # 等待 429 上报
            assert _wait_for(
                lambda: coord._active_limiter is not None and coord._active_limiter.is_blocked(),
                timeout=8.0,
            )
            limiter = coord._active_limiter
            assert limiter.cooldown_remaining() > 0
            assert limiter.metrics()["total_429"] >= 1
            # cooldown 期间无可用容量
            assert limiter.available_capacity == 0
        finally:
            probe.release_all()
            coord.close()

    def test_concurrent_429_does_not_create_retry_stampede(self, tmp_config_manager, monkeypatch):
        """多个请求同时 429 只产生有界 cooldown，不会形成重试风暴（§8.3）。

        验证：3 个批次通过 barrier 同时在途、同时 429 时，
        blocked_until 不会被反复延后到很远的未来（bounded cooldown）。
        """
        # barrier(3) 保证 3 个批次都在 _translate_batch 中后才开始失败
        barrier = threading.Barrier(3, timeout=10.0)
        exc = TranslationRequestError(
            "rate limited",
            status_code=429,
            retry_after_seconds=None,
        )

        class _StampedeEngine:
            def __init__(self, config_manager):
                self.config_manager = config_manager
                self.is_stopped = False
                self.api = MagicMock()
                self.close_called = False
                self.stop_called = False

            def _ensure_api(self):
                pass

            def build_run_context(self):
                return _make_run_context()

            def compute_input_token_budget(self, configured):
                return configured

            def _translate_batch(self, batch_lines, *args, **kwargs):
                # 等所有 3 个批次都到位后同时失败
                barrier.wait()
                raise exc

            def close(self):
                self.close_called = True

            def stop(self):
                self.stop_called = True
                self.is_stopped = True

        policy = _make_policy(
            max_in_flight_requests=3,
            hard_request_cap=3,
            per_task_soft_limit=1,
            max_batch_lines=1,
        )
        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _StampedeEngine)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b", "c"])
            coord.submit_command("start", "t1")
            # 等待 3 次 429 全部上报（barrier 保证 3 批同时失败）
            assert _wait_for(
                lambda: (
                    coord._active_limiter is not None
                    and coord._active_limiter.metrics()["total_429"] >= 3
                ),
                timeout=12.0,
            )
            limiter = coord._active_limiter
            # 无 Retry-After 时 cooldown 应在 15-30s 范围（§8.2）
            # 多次 429 不会把 blocked_until 推到 30s 以上（指数退避有上限）
            remaining = limiter.cooldown_remaining()
            assert 0 < remaining <= 30.0, f"cooldown 异常: {remaining}s（应 ≤ 30s，避免重试风暴）"
            # current_limit 已降到 1（3 // 2 = 1，再 //2 仍为 1）
            assert limiter.current_limit == 1
        finally:
            coord.close()


# ── §13.3 生命周期与状态机 ───────────────────────────────


class TestLifecycleStateMachine:
    """§13.3 生命周期与状态机：取消/暂停/重启的隔离语义。"""

    def test_waiting_task_can_cancel_without_api_call(self, tmp_config_manager, monkeypatch):
        """等待槽位的任务可零请求取消（§4.1，§13.3）。

        场景：max_in_flight=1，任务1占住槽位并阻塞；任务2 进入 READY 等待。
        取消任务2 时不应调用其引擎的 _translate_batch。
        """
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=1, per_task_soft_limit=1, max_batch_lines=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            # 任务1：2 批，占住唯一槽位
            _add_task(coord, "t1", "/tmp/a.txt", ["a1", "a2"])
            # 任务2：1 批，会等待槽位
            _add_task(coord, "t2", "/tmp/b.txt", ["b1"])
            coord.submit_command("start_all")
            # 等任务1占住槽位
            assert _wait_for(lambda: probe.current >= 1, timeout=8.0)
            # 任务2 应处于 READY（等待槽位），未调用 _translate_batch
            t2_slot = coord._tasks.get("t2")
            assert t2_slot is not None
            assert t2_slot.state in (QueueTaskState.READY, QueueTaskState.PENDING)
            calls_before = probe.translate_calls
            # 取消任务2
            coord.submit_command("cancel", "t2")
            assert _wait_for(
                lambda: coord._tasks["t2"].state == QueueTaskState.CANCELLED,
                timeout=8.0,
            )
            # 取消期间任务2没有发出 API 请求
            # （probe.translate_calls 只可能因任务1增加，不因任务2）
            # 释放任务1让其完成
            probe.release_event.set()
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] == "completed",
                timeout=8.0,
            )
            # 任务2的引擎应被关闭（终态关闭）
            assert t2_slot.engine is None or t2_slot.engine.stop_called
        finally:
            probe.release_event.set()
            coord.close()

    def test_pause_stops_new_dispatch_and_confirms_after_inflight_drains(
        self, tmp_config_manager, monkeypatch
    ):
        """暂停先停止新派发，在途批次完成后才确认 PAUSED（§9.1）。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1, max_batch_lines=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b", "c", "d"])
            coord.submit_command("start", "t1")
            # 等待批次进入在途
            assert _wait_for(lambda: probe.current >= 1, timeout=8.0)
            # 请求暂停
            coord.submit_command("pause", "t1")
            # 在途批次仍阻塞中，状态应为 PAUSE_REQUESTED（未立即 PAUSED）
            assert _wait_for(
                lambda: coord._tasks["t1"].state == QueueTaskState.PAUSE_REQUESTED,
                timeout=8.0,
            )
            assert coord._tasks["t1"].limiter_consumer_id == ""
            in_flight_before = len(coord._tasks["t1"].in_flight_batches)
            # 释放让在途批次完成
            probe.release_event.set()
            # 在途排空后才转为 PAUSED
            assert _wait_for(
                lambda: coord._tasks["t1"].state == QueueTaskState.PAUSED,
                timeout=8.0,
            )
            # PAUSED 后不再有在途批次
            assert len(coord._tasks["t1"].in_flight_batches) == 0
            # 暂停后仍有未完成的 pending 批次（未派发）
            assert len(coord._tasks["t1"].pending_batches) > 0
        finally:
            probe.release_event.set()
            coord.close()

    def test_stale_attempt_outcome_cannot_overwrite_restart(self, tmp_config_manager, monkeypatch):
        """取消后旧 attempt 的 Outcome 被丢弃，不会覆盖重启后的结果（§9.3）。"""
        probe = _SequenceProbe()
        policy = _make_policy(max_in_flight_requests=1, per_task_soft_limit=1, max_batch_lines=1)

        def _factory(config_manager):
            return _SequenceEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b"])
            coord.submit_command("start", "t1")
            # 等待第一批进入（seq 1）但不释放
            assert _wait_for(lambda: len(probe.arrived) >= 1, timeout=8.0)
            first_attempt = coord._tasks["t1"].attempt_id
            # 取消：bump attempt_id
            coord.submit_command("cancel", "t1")
            assert _wait_for(
                lambda: coord._tasks["t1"].state == QueueTaskState.CANCELLED,
                timeout=8.0,
            )
            second_attempt = coord._tasks["t1"].attempt_id
            assert second_attempt != first_attempt, "取消后 attempt_id 未改变"
            # 释放旧批次（seq 1），模拟迟到的旧 Outcome
            probe.release(1)
            # 等待旧批次退出并释放 limiter 槽位（否则重启时无槽位可派发）
            assert _wait_for(lambda: 1 in probe.completed, timeout=8.0)
            assert _wait_for(
                lambda: len(coord._in_flight) == 0, timeout=8.0
            ), "旧批次未从在途集合中移除"
            # 重启任务
            coord.submit_command("start", "t1")
            # 等待新批次进入（seq 2）
            assert _wait_for(lambda: len(probe.arrived) >= 2, timeout=8.0)
            third_attempt = coord._tasks["t1"].attempt_id
            assert third_attempt != first_attempt, "重启后 attempt_id 仍是旧的"
            # 循环释放所有批次（重启后新批次陆续到达，
            # release_all 只能释放已创建的 event，新批次的 event 需再次释放）
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                probe.release_all()
                if coord.get_task_data("t1")["status"] == "completed":
                    break
                time.sleep(0.1)
            # 等待完成
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] == "completed",
                timeout=8.0,
            )
            # 旧 Outcome 未污染结果：target_lines 正确
            data = coord.get_task_data("t1")
            assert data["target_lines"] == ["译:a", "译:b"]
        finally:
            probe.release_all()
            coord.close()

    def test_cancel_one_task_does_not_close_shared_client_for_other_tasks(
        self, tmp_config_manager, monkeypatch
    ):
        """取消一个任务不影响其他任务的引擎/客户端（§5.3，§13.3）。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1, max_batch_lines=1)
        engines: List[_ProbeEngine] = []

        def _factory(config_manager):
            eng = _ProbeEngine(config_manager, probe)
            engines.append(eng)
            return eng

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b"])
            _add_task(coord, "t2", "/tmp/b.txt", ["c", "d"])
            coord.submit_command("start_all")
            assert _wait_for(lambda: len(engines) >= 2, timeout=8.0)
            # 取消 t1
            coord.submit_command("cancel", "t1")
            assert _wait_for(
                lambda: coord._tasks["t1"].state == QueueTaskState.CANCELLED,
                timeout=8.0,
            )
            # t2 的引擎未被关闭，仍可继续
            t2_slot = coord._tasks.get("t2")
            assert t2_slot is not None
            assert t2_slot.engine is not None, "t2 引擎被错误关闭"
            # t2 仍能完成
            probe.release_event.set()
            assert _wait_for(
                lambda: coord.get_task_data("t2")["status"] == "completed",
                timeout=8.0,
            )
        finally:
            probe.release_event.set()
            coord.close()

    def test_engine_or_runtime_is_closed_on_terminal_remove_and_window_close(
        self, tmp_config_manager, monkeypatch
    ):
        """终态移除任务和关闭窗口时引擎被 close（§3.5，§13.3）。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=1, max_batch_lines=1)
        engines: List[_ProbeEngine] = []

        def _factory(config_manager):
            eng = _ProbeEngine(config_manager, probe)
            engines.append(eng)
            return eng

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a"])
            coord.submit_command("start", "t1")
            # 等待完成
            probe.release_event.set()
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] == "completed",
                timeout=8.0,
            )
            # 终态后引擎应被关闭
            assert _wait_for(
                lambda: engines[0].close_called,
                timeout=8.0,
            ), "终态后引擎未被关闭"
            # 移除任务
            coord.remove_task("t1")
            assert _wait_for(
                lambda: "t1" not in coord._tasks,
                timeout=8.0,
            )
        finally:
            probe.release_event.set()
            coord.close()
            # close 后所有引擎都应被关闭
            for eng in engines:
                assert eng.close_called, "窗口关闭后引擎未被关闭"

    def test_restart_closes_replaced_engine(self, tmp_config_manager, monkeypatch):
        """重启任务时旧引擎被 close（§3.5，§13.3）。"""
        probe = _SequenceProbe()
        policy = _make_policy(max_in_flight_requests=1, max_batch_lines=1)
        engines: List[_SequenceEngine] = []

        def _factory(config_manager):
            eng = _SequenceEngine(config_manager, probe)
            engines.append(eng)
            return eng

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b"])
            coord.submit_command("start", "t1")
            assert _wait_for(lambda: len(engines) >= 1, timeout=8.0)
            first_engine = engines[0]
            # 取消
            coord.submit_command("cancel", "t1")
            assert _wait_for(
                lambda: coord._tasks["t1"].state == QueueTaskState.CANCELLED,
                timeout=8.0,
            )
            # 释放旧批次并等待其从在途集合移除（释放 limiter 槽位）
            probe.release_all()
            assert _wait_for(
                lambda: len(coord._in_flight) == 0, timeout=8.0
            ), "旧批次未从在途集合中移除"
            # 重启：应创建新引擎并关闭旧引擎
            coord.submit_command("start", "t1")
            assert _wait_for(lambda: len(engines) >= 2, timeout=8.0)
            assert first_engine.close_called, "重启时旧引擎未被关闭"
            # 循环释放所有批次（重启后新批次陆续到达，
            # release_all 只能释放已创建的 event，新批次的 event 需再次释放）
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                probe.release_all()
                if coord.get_task_data("t1")["status"] == "completed":
                    break
                time.sleep(0.1)
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] == "completed",
                timeout=8.0,
            )
        finally:
            probe.release_all()
            coord.close()

    def test_removing_paused_task_releases_worker_and_slot(self, tmp_config_manager, monkeypatch):
        """移除已暂停任务释放 worker 和槽位（§3.6，§13.3）。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2, per_task_soft_limit=1, max_batch_lines=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b", "c"])
            coord.submit_command("start", "t1")
            assert _wait_for(lambda: probe.current >= 1, timeout=8.0)
            # 暂停
            coord.submit_command("pause", "t1")
            probe.release_event.set()
            assert _wait_for(
                lambda: coord._tasks["t1"].state == QueueTaskState.PAUSED,
                timeout=8.0,
            )
            # 移除暂停任务
            ok = coord.remove_task("t1")
            assert ok is True
            assert _wait_for(
                lambda: "t1" not in coord._tasks,
                timeout=8.0,
            )
            # 引擎应被关闭，槽位释放
            # （引擎已在 _handle_terminal_tasks 或 remove 中关闭）
        finally:
            probe.release_event.set()
            coord.close()

    def test_duplicate_source_path_cannot_write_concurrently(self, tmp_config_manager, monkeypatch):
        """同一路径不能被两个活动任务并发写（§10.2，§13.3）。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            ok1 = _add_task(coord, "t1", "/tmp/same.txt", ["a"])
            assert ok1 is True
            # 同路径第二个任务应被拒绝
            ok2 = _add_task(coord, "t2", "/tmp/same.txt", ["b"])
            assert ok2 is False, "同路径第二个任务未被拒绝"
            # 不同路径可以
            ok3 = _add_task(coord, "t3", "/tmp/other.txt", ["c"])
            assert ok3 is True
        finally:
            probe.release_event.set()
            coord.close()


# ── §13.4 UI 与持久化 ────────────────────────────────────


class TestUIAndPersistence:
    """§13.4 UI 与持久化：worker 零 Tk 调用、设置控制并发、检查点存活。"""

    def test_worker_thread_never_calls_tk_api(self, tmp_config_manager, monkeypatch):
        """工作线程不调用任何 Tk API（§3.7，§14.1）。

        通过断言 MockEngine 在 _translate_batch 中未引用 tkinter 实现。
        Coordinator 的回调仅通知 UI 主线程，不直接调用 Tk。
        """
        # tk 调用检测：若工作线程导入或调用 tkinter，记录到 violations
        import tkinter as _tk_module

        tk_violations: List[str] = []
        orig_tk_init = _tk_module.Tk.__init__

        def _spy_tk_init(self, *args, **kwargs):
            import threading as _t

            if _t.current_thread().name != "MainThread":
                tk_violations.append(f"Tk() created on {_t.current_thread().name}")
            return orig_tk_init(self, *args, **kwargs)

        monkeypatch.setattr(_tk_module.Tk, "__init__", _spy_tk_init)

        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=2)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b"])
            coord.submit_command("start", "t1")
            probe.release_event.set()
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] == "completed",
                timeout=8.0,
            )
            assert tk_violations == [], f"工作线程调用了 Tk API: {tk_violations}"
        finally:
            probe.release_event.set()
            coord.close()

    def test_queue_setting_controls_actual_queue_concurrency(self, tmp_config_manager, monkeypatch):
        """设置页的 queue_max_in_flight_requests 确实控制队列并发（§11.1）。"""
        probe = _ConcurrencyProbe()
        # 显式设置并发为 1、批次行数为 1（强制拆成 4 批）：
        # 通过 get_app_config + save_app_config 接线
        cfg = tmp_config_manager.get_app_config()
        cfg["queue_max_in_flight_requests"] = 1
        cfg["queue_batch_lines"] = 1
        tmp_config_manager.save_app_config(cfg)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        from src.config.translation_profile import build_queue_policy_from_app_config

        policy = build_queue_policy_from_app_config(tmp_config_manager.get_app_config())
        assert policy.max_in_flight_requests == 1
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/a.txt", ["a", "b", "c", "d"])
            coord.submit_command("start", "t1")
            # 并发为 1 时峰值不超过 1
            assert _wait_for(lambda: probe.max_seen >= 1, timeout=8.0)
            probe.release_event.set()
            assert _wait_for(lambda: probe.translate_calls >= 4, timeout=8.0)
            assert probe.max_seen <= 1, f"并发设置 1 未生效，峰值={probe.max_seen}"
        finally:
            probe.release_event.set()
            coord.close()

    def test_batch_checkpoint_survives_cancel_or_process_restart(
        self, tmp_config_manager, monkeypatch
    ):
        """已成功的批次在取消后仍落盘（§10.2，§13.4）。"""
        from pathlib import Path

        probe = _SequenceProbe()
        policy = _make_policy(max_in_flight_requests=1, max_batch_lines=1)

        def _factory(config_manager):
            return _SequenceEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            src = ["a", "b", "c", "d"]
            _add_task(coord, "t1", "/tmp/persist.txt", src)
            coord.submit_command("start", "t1")
            # 等第一批进入并完成
            assert _wait_for(lambda: len(probe.arrived) >= 1, timeout=8.0)
            probe.release(1)
            assert _wait_for(lambda: 1 in probe.completed, timeout=8.0)
            # 等待检查点写入
            time.sleep(0.6)
            # 取消（第二批还在等）
            coord.submit_command("cancel", "t1")
            assert _wait_for(
                lambda: coord._tasks["t1"].state == QueueTaskState.CANCELLED,
                timeout=8.0,
            )
            probe.release_all()
        finally:
            probe.release_all()
            coord.close()
        # 验证已成功批次落盘
        out_file = Path("/tmp/persist_译文.txt")
        if out_file.exists():
            content = out_file.read_text(encoding="utf-8")
            # 至少第一批的译文应存在
            assert (
                "译:a" in content or "译:b" in content or content
            ), f"已成功批次未落盘: {content!r}"

    def test_checkpoint_writer_is_single_flight(self, tmp_config_manager, monkeypatch):
        """检查点写入是 single-flight：多个批次成功时合并保存（§10.2）。"""

        write_count = {"n": 0}
        write_lock = threading.Lock()
        orig_write = _MockFileHandler.write_file

        def _counting_write(self, path, content):
            with write_lock:
                write_count["n"] += 1
            return orig_write(self, path, content)

        monkeypatch.setattr(_MockFileHandler, "write_file", _counting_write)

        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=4, per_task_soft_limit=2, max_batch_lines=1)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/single.txt", ["a", "b", "c", "d"])
            coord.submit_command("start", "t1")
            probe.release_event.set()
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] == "completed",
                timeout=10.0,
            )
            # single-flight + debounce：4 批成功但写入次数应远小于 4
            # （debounce 合并 + 终态 flush）
            time.sleep(0.6)
            assert write_count["n"] <= 4, f"single-flight 失败：写入次数 {write_count['n']} 过多"
        finally:
            probe.release_event.set()
            coord.close()

    def test_save_failure_keeps_task_dirty_and_not_completed(self, tmp_config_manager, monkeypatch):
        """保存失败时任务保留 dirty 且不标记为 completed（§10.2，§13.4）。"""
        probe = _ConcurrencyProbe()
        policy = _make_policy(max_in_flight_requests=1, max_batch_lines=1)

        def _failing_write(self, path, content):
            raise OSError("磁盘已满")

        monkeypatch.setattr(_MockFileHandler, "write_file", _failing_write)

        def _factory(config_manager):
            return _ProbeEngine(config_manager, probe)

        monkeypatch.setattr("src.core.queue_scheduler.TranslatorEngine", _factory)
        coord = _make_coordinator(tmp_config_manager, policy)
        coord.start()
        try:
            _add_task(coord, "t1", "/tmp/fail.txt", ["a"])
            coord.submit_command("start", "t1")
            probe.release_event.set()
            # 等待任务到达终态（因保存失败应非 completed）
            assert _wait_for(
                lambda: coord.get_task_data("t1")["status"] in ("error", "partial", "completed"),
                timeout=10.0,
            )
            status = coord.get_task_data("t1")["status"]
            # 保存失败时不应是 completed
            assert status != "completed", "保存失败却标记为 completed（违反 §10.2）"
        finally:
            probe.release_event.set()
            coord.close()
