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
import threading
import time
from dataclasses import dataclass
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
    def write_file(self, path, content):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")

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


def _patch_engine_factory(*, base_latency: float, fail_rate: float):
    """返回一个工厂函数，用于替换 queue_scheduler.TranslatorEngine。"""

    def _factory(config_manager):
        return BenchmarkEngine(
            config_manager,
            base_latency=base_latency,
            fail_rate=fail_rate,
        )

    return _factory


def run_scenario(
    *,
    scenario: str,
    task_count: int,
    lines_per_task: int,
    policy: QueuePolicy,
    base_latency: float,
    fail_rate: float,
    timeout: float = 60.0,
) -> BenchmarkResult:
    """运行单个基准场景。"""
    import src.core.queue_scheduler as qsch

    original_engine_cls = qsch.TranslatorEngine
    qsch.TranslatorEngine = _patch_engine_factory(base_latency=base_latency, fail_rate=fail_rate)

    try:
        coord = QueueTranslationCoordinator(
            _MockConfigManager(),
            policy,
            file_handler=_MockFileHandler(),
            epub_processor=_MockEpubProcessor(),
        )
        coord.start()

        # 注册任务
        for i in range(task_count):
            file_path = f"/bench/task_{i}.txt"
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
                print(f"  [警告] 任务 t{i} 注册失败（路径冲突）")

        # 启动全部任务
        coord.submit_command("start_all")

        # 轮询等待所有任务进入终态
        start_time = time.monotonic()
        deadline = start_time + timeout
        last_snapshot: QueueSnapshot | None = None
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
                    break
            time.sleep(0.1)

        elapsed = time.monotonic() - start_time

        # 收集指标
        total_429 = 0
        final_limit = policy.max_in_flight_requests
        if last_snapshot is not None:
            total_429 = last_snapshot.metrics.total_429
            final_limit = last_snapshot.metrics.current_limit

        total_lines = task_count * lines_per_task
        throughput = total_lines / elapsed if elapsed > 0 else 0.0

        coord.close()

        return BenchmarkResult(
            scenario=scenario,
            task_count=task_count,
            total_lines=total_lines,
            elapsed_seconds=elapsed,
            throughput_lines_per_sec=throughput,
            total_429=total_429,
            final_current_limit=final_limit,
            avg_batch_latency=base_latency,
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
    args = parser.parse_args()

    print("=" * 72)
    print("队列翻译并发优化基准工具")
    print(f"任务数: {args.tasks}  每任务行数: {args.lines}")
    print("=" * 72)

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
                f"final_limit: {r.final_current_limit}"
            )


def _print_result(r: BenchmarkResult):
    print(f"  场景: {r.scenario}")
    print(f"  任务数: {r.task_count}  总行数: {r.total_lines}")
    print(f"  耗时: {r.elapsed_seconds:.2f}s")
    print(f"  吞吐: {r.throughput_lines_per_sec:.1f} 行/秒")
    print(f"  429 次数: {r.total_429}")
    print(f"  最终并发上限: {r.final_current_limit}")


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
                ]
            )


if __name__ == "__main__":
    main()
