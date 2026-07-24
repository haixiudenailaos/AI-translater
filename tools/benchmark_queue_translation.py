#!/usr/bin/env python3
"""队列翻译并发优化基准工具（阶段 4 性能护栏）。

用途：
- 测量 ``QueueTranslationCoordinator`` 在多任务、多批次场景下的调度开销和吞吐。
- 验证 AIMD 在模拟 429 时的降级和恢复行为。
- 输出 CSV/控制台报告，对比"单任务串行"vs"多任务并发"vs"429 降级"三种场景。

不依赖真实 API——使用 MockEngine 模拟可变延迟的 _translate_batch。
运行方式：
    python -m tools.benchmark_queue_translation
    python -m tools.benchmark_queue_translation --tasks 4 --lines 200 --scenario concurrent
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

# 将项目根加入 sys.path（必须在 src 导入前）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.queue_scheduler import (  # noqa: E402
    QueuePolicy,
    QueueSnapshot,
    QueueTaskState,
    QueueTranslationCoordinator,
)
from src.core.translation_result import BatchTranslationResult  # noqa: E402
from src.core.translator import TranslationRunContext  # noqa: E402
from src.domain.translation import OperationStatus  # noqa: E402

# ── Mock 组件 ────────────────────────────────────────────


class BenchmarkEngine:
    """Mock 引擎：模拟可变延迟的 _translate_batch。

    - ``base_latency``：每次调用的基础延迟（秒）。
    - ``fail_rate``：模拟失败概率（0-1），失败时抛 429。
    """

    def __init__(self, config_manager, *, base_latency: float = 0.05, fail_rate: float = 0.0):
        self.config_manager = config_manager
        self.is_stopped = False
        self.api = MagicMock()
        self.base_latency = base_latency
        self.fail_rate = fail_rate
        self.call_count = 0
        self._lock = threading.Lock()

    def _ensure_api(self):
        pass

    def build_run_context(self) -> TranslationRunContext:
        return TranslationRunContext(
            provider="siliconflow",
            model_name="deepseek-ai/DeepSeek-V3.2",
            target_language="中文",
            base_prompt="translate",
            glossary_prompt="",
            system_prompt=None,
            is_hunyuan=False,
            temperature=0.3,
            prompt_version="bench",
            glossary_version="",
        )

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
        with self._lock:
            self.call_count += 1
        # 模拟网络延迟
        time.sleep(self.base_latency)
        # 模拟随机失败
        import random

        if self.fail_rate > 0 and random.random() < self.fail_rate:
            from src.domain.errors import TranslationRequestError

            raise TranslationRequestError("simulated 429", status_code=429, retry_after_seconds=1.0)
        return BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=[f"译:{line}" for line in batch_lines],
        )

    def close(self):
        pass

    def stop(self):
        self.is_stopped = True


class _MockFileHandler:
    """写入计数的 mock 文件处理器。

    记录累计写入字节与写入次数，用于观测检查点写放大。
    """

    def __init__(self) -> None:
        self.bytes_written = 0
        self.write_count = 0

    def write_file(self, path, content):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        Path(path).write_bytes(data)
        self.bytes_written += len(data)
        self.write_count += 1

    def read_file(self, path):
        return Path(path).read_text(encoding="utf-8")


class _MockEpubProcessor:
    def save_translations(self, mapping_dir, lines):
        pass


class _MockConfigManager:
    """最小 ConfigManager，避免加载真实配置文件。"""

    def get_app_config(self):
        return {
            "queue_max_in_flight_requests": 2,
            "queue_hard_request_cap": 4,
            "queue_max_active_tasks": 4,
            "queue_per_task_soft_limit": 1,
            "queue_batch_lines": 50,
            "queue_batch_max_input_tokens": 8000,
            "queue_translation_concurrency": 2,
        }

    def get_api_config(self):
        return {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model_name": "deepseek-ai/DeepSeek-V3.2",
            "api_key": "sk-benchmark",
            "temperature": 0.3,
            "context_window_tokens": 32768,
        }

    def get_glossary_prompt(self):
        return ""


# ── 基准场景 ─────────────────────────────────────────────


@dataclass
class BenchmarkResult:
    scenario: str
    task_count: int
    total_lines: int
    elapsed_seconds: float
    throughput_lines_per_sec: float
    total_429: int
    final_current_limit: int
    avg_batch_latency: float
    # ── 批次 A 新增观测指标 ──
    # 引擎调用总次数（含重试，反映真实请求放大）
    total_attempts: int = 0
    total_success: int = 0
    total_timeouts: int = 0
    # 检查点累计写入字节与写入次数（写放大观测）
    checkpoint_bytes_written: int = 0
    checkpoint_write_count: int = 0
    # 协调器关闭耗时（终态保存排空）
    close_seconds: float = 0.0
    # 失败语义：任何任务保存失败 / 超时 / 未达终态即为 True
    failed: bool = False
    failure_reasons: List[str] = field(default_factory=list)


def _patch_engine_factory(*, base_latency: float, fail_rate: float, engines: list):
    """返回一个工厂函数，用于替换 queue_scheduler.TranslatorEngine。

    每创建一个引擎就登记到 ``engines``，便于场景结束后聚合调用次数。
    """

    def _factory(config_manager):
        engine = BenchmarkEngine(
            config_manager,
            base_latency=base_latency,
            fail_rate=fail_rate,
        )
        engines.append(engine)
        return engine

    return _factory


def run_scenario(
    *,
    scenario: str,
    task_count: int,
    lines_per_task: int,
    policy: QueuePolicy,
    base_latency: float,
    fail_rate: float,
    workdir: Path,
    timeout: float = 60.0,
) -> BenchmarkResult:
    """运行单个基准场景。

    ``workdir`` 必须是真实可写目录：任务 ``file_path`` 指向其中的文件，
    检查点保存（``_译文.txt``）才能真实落盘。任何保存失败、任务进入
    ERROR 终态或等待超时都会让结果 ``failed=True``——基准内部错误
    不得返回成功。
    """
    import src.core.queue_scheduler as qsch

    original_engine_cls = qsch.TranslatorEngine
    engines: list = []
    qsch.TranslatorEngine = _patch_engine_factory(
        base_latency=base_latency,
        fail_rate=fail_rate,
        engines=engines,
    )

    try:
        file_handler = _MockFileHandler()
        coord = QueueTranslationCoordinator(
            _MockConfigManager(),
            policy,
            file_handler=file_handler,
            epub_processor=_MockEpubProcessor(),
        )
        coord.start()

        # 注册任务：使用 workdir 下真实可写路径（Windows 上 /bench 会
        # 解析到盘符根目录触发 PermissionError，导致结果不可比）。
        failure_reasons: List[str] = []
        for i in range(task_count):
            file_path = str(workdir / f"task_{i}.txt")
            source_lines = [f"line-{i}-{j}" for j in range(lines_per_task)]
            target_lines = [""] * lines_per_task
            ok = coord.add_task(
                task_id=f"t{i}",
                file_path=file_path,
                file_name=f"task_{i}.txt",
                file_type="txt",
                mapping_dir=None,
                source_lines=source_lines,
                target_lines=target_lines,
            )
            if not ok:
                failure_reasons.append(f"任务 t{i} 注册失败（路径冲突）")

        # 启动全部任务
        coord.submit_command("start_all")

        # 轮询等待所有任务进入终态
        start_time = time.monotonic()
        deadline = start_time + timeout
        last_snapshot: QueueSnapshot | None = None
        reached_terminal = False
        while time.monotonic() < deadline:
            snapshot = coord.get_snapshot()
            if snapshot is not None:
                last_snapshot = snapshot
                all_terminal = all(
                    t.state
                    in (
                        QueueTaskState.COMPLETED,
                        QueueTaskState.PARTIAL,
                        QueueTaskState.ERROR,
                        QueueTaskState.CANCELLED,
                    )
                    for t in snapshot.tasks
                )
                if all_terminal and snapshot.tasks:
                    reached_terminal = True
                    break
            time.sleep(0.1)

        elapsed = time.monotonic() - start_time

        # 收集指标
        total_429 = 0
        total_timeouts = 0
        total_success = 0
        final_limit = policy.max_in_flight_requests
        if last_snapshot is not None:
            total_429 = last_snapshot.metrics.total_429
            total_timeouts = last_snapshot.metrics.total_timeouts
            total_success = last_snapshot.metrics.total_success
            final_limit = last_snapshot.metrics.current_limit

            # 失败语义：检查点保存失败或任务 ERROR 终态都使场景失败
            for task in last_snapshot.tasks:
                if task.checkpoint_error:
                    failure_reasons.append(
                        f"任务 {task.task_id} 检查点保存失败: {task.checkpoint_error}"
                    )
                if task.state == QueueTaskState.ERROR:
                    failure_reasons.append(
                        f"任务 {task.task_id} 进入 ERROR 终态: {task.error_message}"
                    )
        else:
            failure_reasons.append("场景运行期间未获得任何队列快照")

        if not reached_terminal:
            failure_reasons.append(f"等待 {timeout:.0f}s 后仍有任务未达终态")

        total_lines = task_count * lines_per_task
        throughput = total_lines / elapsed if elapsed > 0 else 0.0

        close_start = time.monotonic()
        coord.close()
        close_seconds = time.monotonic() - close_start

        total_attempts = sum(engine.call_count for engine in engines)

        return BenchmarkResult(
            scenario=scenario,
            task_count=task_count,
            total_lines=total_lines,
            elapsed_seconds=elapsed,
            throughput_lines_per_sec=throughput,
            total_429=total_429,
            final_current_limit=final_limit,
            avg_batch_latency=base_latency,
            total_attempts=total_attempts,
            total_success=total_success,
            total_timeouts=total_timeouts,
            checkpoint_bytes_written=file_handler.bytes_written,
            checkpoint_write_count=file_handler.write_count,
            close_seconds=close_seconds,
            failed=bool(failure_reasons),
            failure_reasons=failure_reasons,
        )
    finally:
        qsch.TranslatorEngine = original_engine_cls


def main():
    parser = argparse.ArgumentParser(description="队列翻译并发优化基准工具")
    parser.add_argument(
        "--tasks",
        type=int,
        default=3,
        help="任务数量（默认 3）",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=100,
        help="每任务行数（默认 100）",
    )
    parser.add_argument(
        "--scenario",
        choices=["serial", "concurrent", "rate_limited", "all"],
        default="all",
        help="运行场景（默认 all）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="CSV 输出路径（可选）",
    )
    parser.add_argument(
        "--workdir",
        type=str,
        default=None,
        help="检查点写入目录（默认使用临时目录，运行结束自动清理）",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("队列翻译并发优化基准工具")
    print(f"任务数: {args.tasks}  每任务行数: {args.lines}")
    print("=" * 72)

    # 工作目录：默认 TemporaryDirectory，避免 Windows 下 /bench 解析到
    # 盘符根目录导致 PermissionError（审计 §2.2）。
    if args.workdir is not None:
        workdir = Path(args.workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        _workdir_cm = None
    else:
        _workdir_cm = tempfile.TemporaryDirectory(prefix="queue_bench_")
        workdir = Path(_workdir_cm.name)
    print(f"工作目录: {workdir}")

    results: List[BenchmarkResult] = []

    if args.scenario in ("serial", "all"):
        # 串行场景：单任务，低并发
        print("\n[1/3] 串行场景（单任务，并发=1）...")
        policy = QueuePolicy(
            max_in_flight_requests=1,
            hard_request_cap=1,
            max_active_tasks=1,
            per_task_soft_limit=1,
            target_batch_input_tokens=2000,
            max_batch_input_tokens=4000,
            max_batch_lines=20,
            min_batch_input_tokens=512,
            adaptive_concurrency=False,
            rpm_limit=0,
            tpm_limit=0,
        )
        r = run_scenario(
            scenario="serial",
            task_count=min(1, args.tasks),
            lines_per_task=args.lines,
            policy=policy,
            base_latency=0.02,
            fail_rate=0.0,
            workdir=workdir / "serial",
        )
        results.append(r)
        _print_result(r)

    if args.scenario in ("concurrent", "all"):
        # 并发场景：多任务，AIMD 启用
        print("\n[2/3] 并发场景（多任务，并发=2，硬上限=4）...")
        policy = QueuePolicy(
            max_in_flight_requests=2,
            hard_request_cap=4,
            max_active_tasks=4,
            per_task_soft_limit=1,
            target_batch_input_tokens=2000,
            max_batch_input_tokens=4000,
            max_batch_lines=20,
            min_batch_input_tokens=512,
            adaptive_concurrency=True,
            rpm_limit=0,
            tpm_limit=0,
        )
        r = run_scenario(
            scenario="concurrent",
            task_count=args.tasks,
            lines_per_task=args.lines,
            policy=policy,
            base_latency=0.02,
            fail_rate=0.0,
            workdir=workdir / "concurrent",
        )
        results.append(r)
        _print_result(r)

    if args.scenario in ("rate_limited", "all"):
        # 限流场景：模拟 30% 429 率，验证 AIMD 降级
        print("\n[3/3] 限流场景（30% 429，验证 AIMD 降级）...")
        policy = QueuePolicy(
            max_in_flight_requests=4,
            hard_request_cap=4,
            max_active_tasks=2,
            per_task_soft_limit=1,
            target_batch_input_tokens=2000,
            max_batch_input_tokens=4000,
            max_batch_lines=20,
            min_batch_input_tokens=512,
            adaptive_concurrency=True,
            rpm_limit=0,
            tpm_limit=0,
        )
        r = run_scenario(
            scenario="rate_limited",
            task_count=max(1, args.tasks // 2),
            lines_per_task=max(20, args.lines // 2),
            policy=policy,
            base_latency=0.02,
            fail_rate=0.3,
            workdir=workdir / "rate_limited",
            timeout=120.0,
        )
        results.append(r)
        _print_result(r)

    # CSV 输出
    if args.output and results:
        _write_csv(args.output, results)
        print(f"\nCSV 报告已写入: {args.output}")

    # 汇总
    if len(results) >= 2:
        print("\n" + "=" * 72)
        print("汇总对比")
        print("=" * 72)
        for r in results:
            print(
                f"  {r.scenario:14s} | "
                f"耗时 {r.elapsed_seconds:6.2f}s | "
                f"吞吐 {r.throughput_lines_per_sec:6.1f} 行/秒 | "
                f"429: {r.total_429} | "
                f"attempts: {r.total_attempts} | "
                f"checkpoint: {r.checkpoint_bytes_written / 1024:.1f} KiB/"
                f"{r.checkpoint_write_count} 次 | "
                f"close: {r.close_seconds:.2f}s | "
                f"final_limit: {r.final_current_limit}"
            )

    # 失败语义：任何场景失败 -> 进程非零退出（审计 §6 统一规则）。
    failed_results = [r for r in results if r.failed]
    if _workdir_cm is not None:
        _workdir_cm.cleanup()
    if failed_results:
        print("\n[失败] 以下场景未通过：")
        for r in failed_results:
            print(f"  - {r.scenario}:")
            for reason in r.failure_reasons:
                print(f"      {reason}")
        sys.exit(1)
    print("\n全部场景通过。")


def _print_result(r: BenchmarkResult):
    print(f"  场景: {r.scenario}")
    print(f"  任务数: {r.task_count}  总行数: {r.total_lines}")
    print(f"  耗时: {r.elapsed_seconds:.2f}s  关闭耗时: {r.close_seconds:.2f}s")
    print(f"  吞吐: {r.throughput_lines_per_sec:.1f} 行/秒")
    print(f"  引擎调用总次数（含重试）: {r.total_attempts}")
    print(f"  429 次数: {r.total_429}  超时次数: {r.total_timeouts}  成功批次: {r.total_success}")
    print(
        f"  检查点写入: {r.checkpoint_bytes_written} 字节 "
        f"({r.checkpoint_bytes_written / 1024:.1f} KiB), {r.checkpoint_write_count} 次"
    )
    print(f"  最终并发上限: {r.final_current_limit}")
    if r.failed:
        print("  状态: 失败")
        for reason in r.failure_reasons:
            print(f"    - {reason}")
    else:
        print("  状态: 通过")


def _write_csv(path: str, results: List[BenchmarkResult]):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "scenario",
                "task_count",
                "total_lines",
                "elapsed_seconds",
                "throughput_lines_per_sec",
                "total_429",
                "final_current_limit",
                "avg_batch_latency",
                "total_attempts",
                "total_success",
                "total_timeouts",
                "checkpoint_bytes_written",
                "checkpoint_write_count",
                "close_seconds",
                "failed",
                "failure_reasons",
            ]
        )
        for r in results:
            writer.writerow(
                [
                    r.scenario,
                    r.task_count,
                    r.total_lines,
                    f"{r.elapsed_seconds:.3f}",
                    f"{r.throughput_lines_per_sec:.2f}",
                    r.total_429,
                    r.final_current_limit,
                    f"{r.avg_batch_latency:.3f}",
                    r.total_attempts,
                    r.total_success,
                    r.total_timeouts,
                    r.checkpoint_bytes_written,
                    r.checkpoint_write_count,
                    f"{r.close_seconds:.3f}",
                    int(r.failed),
                    "; ".join(r.failure_reasons),
                ]
            )


if __name__ == "__main__":
    main()
