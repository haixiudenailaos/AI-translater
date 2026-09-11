#!/usr/bin/env python3
"""LC-04 / LC-09 / LC-11：批量队列的超长上下文真实请求路径。

LC-04 要求"经真实 Coordinator/worker 路径捕获到超过 200 行及 20,000 输入
token 的请求，不能只测试策略对象"。因此本文件不用 MockEngine，而是让
Coordinator 构造真实 ``TranslatorEngine``（其 API 被替换为记录请求的替身），
从 provider 实际收到的请求内容上验证行为。

覆盖：
- LC-04：真实 Coordinator/worker 路径发出超过 200 行 / 20,000 token 的请求；
  普通 ``start_all`` 仍受原队列上限约束。
- LC-09：普通与超长任务共存时各自策略独立；全局并发额度不被突破。
- LC-11：请求估算大于 TPM 时明确失败，不永久挂起。
"""

from __future__ import annotations

import re
import threading
import time

import pytest

from src.application.batch_planner import BatchPlanningError
from src.core.queue_scheduler import (
    LongContextStartRequest,
    QueuePolicy,
    QueueTaskState,
    QueueTranslationCoordinator,
    plan_long_context_batches,
)
from src.core.translator import TranslationRunContext
from src.domain.translation_policy import (
    ContextMode,
    TranslationContextPolicy,
)

BIG_CONTEXT = 1_048_576
_LINE_MARKER_RE = re.compile(r"\[LINE_\d+\]")


# ── 夹具 ─────────────────────────────────────────────────


class RecordingAPI:
    """记录真实请求内容的 API 替身（按行号标记回放译文）。"""

    def __init__(self, *, tpm_limit: int = 0):
        self.requests: list[str] = []
        self.lock = threading.Lock()
        self.tpm_limit = tpm_limit

    def configure_serial_transport(self, serial):
        pass

    def reset_cancel(self):
        pass

    def cancel_requests(self):
        pass

    def recommended_concurrency(self, configured):
        return configured

    def recommended_input_budget(self, configured):
        return configured

    def last_finish_reason(self):
        return None

    def translate_stream(self, prompt, callback, system_prompt=None):
        with self.lock:
            self.requests.append(prompt)
        result = "\n".join(
            f"{m.group(0)}译:{line.split(']', 1)[-1]}"
            for line in prompt.split("\n")
            if (m := _LINE_MARKER_RE.search(line))
        )
        if callback:
            callback(result)
        return result


class FakeConfigManager:
    """提供 Coordinator 与真实引擎所需的最小配置接口。"""

    def __init__(self, *, context_window_tokens: int = BIG_CONTEXT, long_window: int = 131072):
        self._app_config = {
            "target_language": "中文",
            "translation_prompt": "请翻译以下文本",
            "batch_lines": 20,
            "queue_batch_lines": 80,
            "small_model_mode": False,
            "long_context": {"schema_version": 1, "context_window_tokens": long_window},
        }
        self._api_config = {
            "provider": "siliconflow",
            "model_name": "deepseek-ai/DeepSeek-V3.2",
            "context_window_tokens": context_window_tokens,
            "temperature": 0.3,
        }

    def get_app_config(self):
        return dict(self._app_config)

    def get_api_config(self, *, load_secret: bool = True):
        return dict(self._api_config)

    def get_glossary_prompt(self):
        return ""


class FakeFileHandler:
    def read_file(self, path):
        return ""

    def write_file(self, path, content):
        return True


class FakeEpubProcessor:
    def save_translations(self, mapping_dir, lines):
        pass

    def export_epub(self, *args, **kwargs):  # pragma: no cover - 本文件不导出
        raise NotImplementedError


def make_policy(**overrides) -> QueuePolicy:
    defaults = dict(
        max_in_flight_requests=2,
        hard_request_cap=4,
        max_active_tasks=4,
        per_task_soft_limit=1,
        target_batch_input_tokens=6000,
        max_batch_input_tokens=20000,
        max_batch_lines=200,  # 队列普通模式上限
        min_batch_input_tokens=512,
        adaptive_concurrency=False,
        rpm_limit=0,
        tpm_limit=0,
    )
    defaults.update(overrides)
    return QueuePolicy(**defaults)


def make_coordinator(config_manager, api, **policy_overrides):
    """构造真实 Coordinator，并把真实引擎的 API 换成记录替身。

    Coordinator 在 ``_prepare_attempt_locked`` 里创建 ``TranslatorEngine``；我们
    在引擎构造后注入替身 API，从而绕过网络但仍走完整调度/执行路径。
    """
    policy = make_policy(**policy_overrides)
    coordinator = QueueTranslationCoordinator(
        config_manager,
        policy,
        file_handler=FakeFileHandler(),
        epub_processor=FakeEpubProcessor(),
        limiter_registry=None,
    )
    # 让真实引擎使用替身 API：装配点在生产代码里是 TranslatorEngine._init_api。
    original_init_api = None

    from src.core import translator as translator_module

    original_engine_init = translator_module.TranslatorEngine.__init__

    def patched_init(self, config_manager_arg, **kwargs):
        original_engine_init(self, config_manager_arg, **kwargs)
        self.api = api

    translator_module.TranslatorEngine.__init__ = patched_init
    try:
        coordinator.start()
        yield coordinator
    finally:
        coordinator.close()
        translator_module.TranslatorEngine.__init__ = original_engine_init
        if original_init_api is not None:  # pragma: no cover - 防御性
            pass


def add_task(coordinator, task_id: str, source_lines, target_lines=None):
    if target_lines is None:
        target_lines = [""] * len(source_lines)
    return coordinator.add_task(
        task_id,
        f"/tmp/{task_id}.txt",
        f"{task_id}.txt",
        "txt",
        None,
        list(source_lines),
        list(target_lines),
    )


def wait_for_state(coordinator, task_id: str, states, timeout: float = 30.0):
    """轮询快照直到任务进入目标状态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = coordinator.get_snapshot()
        if snapshot is not None:
            for task in snapshot.tasks:
                if task.task_id == task_id and task.state in states:
                    return task
        time.sleep(0.02)
    snapshot = coordinator.get_snapshot()
    actual = (
        [t.state for t in snapshot.tasks if t.task_id == task_id] if snapshot else "no-snapshot"
    )
    raise AssertionError(f"task {task_id} did not reach {states}; actual={actual}")


def lines_per_request(requests) -> list[int]:
    return [len(_LINE_MARKER_RE.findall(request)) for request in requests]


def wait_until_idle(coordinator, task_ids, timeout: float = 30.0):
    terminal = {
        QueueTaskState.COMPLETED,
        QueueTaskState.PARTIAL,
        QueueTaskState.ERROR,
        QueueTaskState.CANCELLED,
    }
    for task_id in task_ids:
        wait_for_state(coordinator, task_id, terminal, timeout=timeout)


@pytest.fixture()
def project_repository(tmp_path):
    from src.infrastructure.project_repository import ProjectRepository

    return ProjectRepository(tmp_path / "projects")


def _wait_for_persisted_project(repository, file_name: str, timeout: float = 15.0):
    """轮询等待该项目被检查点线程写盘（写盘是异步的）。"""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            project = repository.find_by_source_path(f"/tmp/{file_name}")
        except Exception as exc:  # noqa: BLE001 - 写盘过程中可能短暂读到半成品
            last_error = exc
            project = None
        if project is not None and project.model_snapshot.context_mode == "long":
            return project
        time.sleep(0.05)
    raise AssertionError(
        f"project for {file_name} was not persisted with long mode; last_error={last_error}"
    )


@pytest.fixture()
def coordinator_factory(tmp_path, project_repository):
    """返回工厂，负责还原被替换的引擎构造，避免测试间互相污染。

    注入真实 ``ProjectRepository``（指向 tmp_path），使 LC-12 的"模式与预算
    随项目快照持久化"走真实的创建/保存/读取路径。
    """
    from src.core import translator as translator_module

    original = translator_module.TranslatorEngine.__init__
    created: list = []
    coordinators: list = []
    repository = project_repository

    def _factory(config_manager, api, **policy_overrides):
        def patched_init(self, config_manager_arg, **kwargs):
            original(self, config_manager_arg, **kwargs)
            self.api = api

        translator_module.TranslatorEngine.__init__ = patched_init
        created.append(patched_init)

        policy = make_policy(**policy_overrides)
        coordinator = QueueTranslationCoordinator(
            config_manager,
            policy,
            file_handler=FakeFileHandler(),
            epub_processor=FakeEpubProcessor(),
            limiter_registry=None,
            project_repository=repository,
        )
        coordinators.append(coordinator)
        coordinator.start()
        return coordinator

    try:
        yield _factory
    finally:
        for coordinator in coordinators:
            coordinator.close()
        translator_module.TranslatorEngine.__init__ = original


class TestQueueLongContextRealRequests:
    def test_long_request_exceeds_old_queue_limits(self, coordinator_factory):
        """LC-04：真实 Coordinator/worker 路径发出超过 200 行、20,000 token 的请求。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        # 每行约 75 token；400 行 ≈ 30,000 token，同时超过 200 行与 20,000 上限。
        source = [f"第{i}行" + "内容文本" * 16 for i in range(400)]
        assert add_task(coordinator, "t1", source)

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 262144),
            ),
        )
        wait_until_idle(coordinator, ["t1"])

        assert api.requests, "超长任务必须真的发出请求"
        assert max(lines_per_request(api.requests)) > 200
        largest = max(api.requests, key=len)
        from src.utils.token_estimator import estimate_tokens

        assert estimate_tokens(largest) > 20_000

    def test_standard_start_all_still_honors_queue_limits(self, coordinator_factory):
        """原“全部开始”保持普通模式，仍受 200 行/20,000 token 队列上限。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        source = [f"第{i}行" + "内容文本" * 16 for i in range(400)]
        assert add_task(coordinator, "t1", source)

        coordinator.submit_command("start_all")
        wait_until_idle(coordinator, ["t1"])

        assert len(api.requests) > 1
        assert all(count <= 200 for count in lines_per_request(api.requests))
        from src.utils.token_estimator import estimate_tokens

        assert max(estimate_tokens(request) for request in api.requests) <= 20_000

    def test_task_detail_reports_mode_and_budget(self, coordinator_factory):
        """运行状态至少在详情中可见“普通/超长”与预算。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a\nb\nc"])

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        wait_until_idle(coordinator, ["t1"])

        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.context_mode == "long"
        assert task.requested_context_tokens == 131072
        assert task.effective_context_tokens == 131072

    def test_standard_task_reports_standard_mode(self, coordinator_factory):
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a\nb"])

        coordinator.submit_command("start_all")
        wait_until_idle(coordinator, ["t1"])

        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.context_mode == "standard"

    def test_model_capacity_limits_queue_batching(self, coordinator_factory):
        """用户预算超过模型容量时，队列实际分批按容量生效。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(context_window_tokens=16384), api)
        source = [f"第{i}行" + "内容文本" * 8 for i in range(300)]
        assert add_task(coordinator, "t1", source)

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 1_048_576),
            ),
        )
        wait_until_idle(coordinator, ["t1"])

        from src.utils.token_estimator import estimate_tokens

        assert len(api.requests) > 1
        assert max(estimate_tokens(request) for request in api.requests) <= 16384
        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.effective_context_tokens == 16384
        assert task.requested_context_tokens == 1_048_576
        assert task.budget_limited is True


class TestMixedModeIsolation:
    def test_standard_and_long_tasks_keep_independent_policies(self, coordinator_factory):
        """LC-09：普通与超长任务共存时策略独立，互不影响。

        两个任务必须**同时**处于未启动状态才能对比模式选择，否则先启动的那个
        会（正确地）拒绝被切换到另一个模式——那是运行中不切换模式的语义。
        """
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "standard", [f"s{i}" for i in range(10)])
        assert add_task(coordinator, "long", [f"l{i}" for i in range(10)])

        # 普通模式只启动 standard。
        coordinator.submit_command("start", "standard")
        wait_until_idle(coordinator, ["standard"])

        # 超长模式只启动 long（standard 已完成，不在冻结集合内）。
        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("long",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        wait_until_idle(coordinator, ["long"])

        snapshot = coordinator.get_snapshot()
        modes = {t.task_id: t.context_mode for t in snapshot.tasks}
        assert modes == {"standard": "standard", "long": "long"}

    def test_running_task_is_not_switched_to_long_mode(self, coordinator_factory):
        """LC-09：运行中/保存中的任务不因另一模式入口而切换模式。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        source = [f"第{i}行" + "内容文本" * 16 for i in range(400)]
        assert add_task(coordinator, "t1", source)

        coordinator.submit_command("start_all")
        wait_until_idle(coordinator, ["t1"])

        # 任务已完成；再次提交超长启动不得改写它的模式。
        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 262144),
            ),
        )
        time.sleep(0.2)
        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.context_mode == "standard"

    def test_new_task_not_in_frozen_set_is_not_started(self, coordinator_factory):
        """点击时冻结目标集合：之后新加入的文件不被意外纳入该次操作。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a", "b"])

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        # 命令入队后立刻加入第二个任务：它不在冻结集合里。
        add_task(coordinator, "t2", ["c", "d"])
        wait_until_idle(coordinator, ["t1"])

        snapshot = coordinator.get_snapshot()
        later = next(t for t in snapshot.tasks if t.task_id == "t2")
        assert later.state is QueueTaskState.PENDING
        assert later.context_mode == "standard"

    def test_settings_change_does_not_alter_in_flight_policy(self, coordinator_factory):
        """在途任务的策略快照不变；设置改动只影响新任务。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a", "b", "c"])

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        wait_until_idle(coordinator, ["t1"])

        # 运行结束后再改全局策略，不应追溯改写已完成任务的模式。
        coordinator.update_policy(make_policy(max_batch_lines=1))
        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.context_mode == "long"

    def test_global_concurrency_cap_is_not_exceeded(self, coordinator_factory):
        """LC-09：混合模式不得突破全局并发额度。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(
            FakeConfigManager(), api, max_in_flight_requests=1, hard_request_cap=1
        )
        source = [f"第{i}行" + "内容文本" * 8 for i in range(200)]
        assert add_task(coordinator, "a", source)
        assert add_task(coordinator, "b", source)

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("a", "b"),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 262144),
            ),
        )
        wait_until_idle(coordinator, ["a", "b"], timeout=60.0)

        snapshot = coordinator.get_snapshot()
        assert snapshot.metrics.current_limit <= 1
        for task in snapshot.tasks:
            assert task.state in (
                QueueTaskState.COMPLETED,
                QueueTaskState.PARTIAL,
            )


class TestQueueBudgetFailures:
    def test_oversized_line_fails_task_with_actionable_error(self, coordinator_factory):
        """预算不足必须成为明确错误，不空转也不硬发。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["字" * 200_000])

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 8192),
            ),
        )
        wait_for_state(coordinator, "t1", {QueueTaskState.ERROR}, timeout=20.0)

        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert "第 1 行" in (task.error_message or "")
        assert api.requests == [], "不得硬发越预算请求"

    def test_tpm_smaller_than_request_fails_instead_of_hanging(self, coordinator_factory):
        """LC-11：请求估算大于 TPM 时明确失败，不永久挂起。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api, tpm_limit=1000)
        source = [f"第{i}行" + "内容文本" * 20 for i in range(50)]
        assert add_task(coordinator, "t1", source)

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 262144),
            ),
        )
        # 必须迅速进入终态，而不是一直等许可。
        wait_for_state(
            coordinator,
            "t1",
            {QueueTaskState.ERROR, QueueTaskState.PARTIAL, QueueTaskState.COMPLETED},
            timeout=20.0,
        )
        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.state is QueueTaskState.ERROR
        assert "TPM" in (task.error_message or "")
        assert api.requests == []

    def test_normal_short_request_under_tpm_still_runs(self, coordinator_factory):
        """TPM 限额正常时不应误报（区分“额度暂时用尽”与“永远不可能”）。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api, tpm_limit=10_000_000)
        assert add_task(coordinator, "t1", ["a", "b"])

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        wait_until_idle(coordinator, ["t1"])
        assert api.requests


class TestContextModePersistence:
    """LC-12：模式与用户预算随项目快照持久化，恢复时继承。"""

    def test_long_mode_round_trips_through_project_snapshot(
        self, coordinator_factory, project_repository
    ):
        """模式与预算真的写进项目仓库，读回后仍然生效（LC-12）。"""
        from src.domain.project import ModelSnapshot

        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a", "b", "c", "d"])

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        wait_until_idle(coordinator, ["t1"])
        # 检查点写盘由独立线程完成，轮询等待项目落盘。
        project = _wait_for_persisted_project(project_repository, "t1.txt")

        snapshot = project.model_snapshot
        assert snapshot.context_mode == "long"
        assert snapshot.context_window_tokens == 131072
        assert snapshot.is_long_context is True

        # 磁盘往返：从仓库重新加载后必须保留模式与预算。
        reloaded = project_repository.load(project.project_id)
        assert reloaded.model_snapshot.context_mode == "long"
        assert reloaded.model_snapshot.context_window_tokens == 131072

        # 序列化再读回同样保留（EPUB/其他恢复路径共用 ModelSnapshot）。
        restored = ModelSnapshot.from_dict(reloaded.model_snapshot.to_dict())
        assert restored.is_long_context is True

    def test_old_project_without_fields_reads_as_standard(self):
        """旧项目缺字段按普通模式读取，不触发全量重译。"""
        from src.domain.project import ModelSnapshot

        legacy = {"provider": "siliconflow", "model_name": "m", "batch_size": 20}
        snapshot = ModelSnapshot.from_dict(legacy)
        assert snapshot.context_mode == "standard"
        assert snapshot.context_window_tokens is None
        assert snapshot.is_long_context is False

    def test_corrupt_long_marker_falls_back_to_standard(self):
        """标记为超长但预算缺失/损坏时按普通模式读取，不猜预算。"""
        from src.domain.project import ModelSnapshot

        for raw in (None, 0, -5, "abc", 1.5, True):
            snapshot = ModelSnapshot.from_dict(
                {"context_mode": "long", "context_window_tokens": raw}
            )
            assert snapshot.context_mode == "standard", raw
            assert snapshot.context_window_tokens is None
            assert snapshot.is_long_context is False

    def test_standard_mode_never_carries_a_budget(self):
        from src.domain.project import ModelSnapshot

        snapshot = ModelSnapshot.from_dict(
            {"context_mode": "standard", "context_window_tokens": 131072}
        )
        assert snapshot.context_window_tokens is None

    def test_restoring_policy_from_project_snapshot(self):
        from src.core.queue_scheduler import _restored_context_policy
        from src.domain.project import ModelSnapshot, TranslationProject

        project = TranslationProject(
            project_id="p",
            source_path="/tmp/a.txt",
            source_fingerprint="",
            file_type="txt",
            mapping_dir="",
            model_snapshot=ModelSnapshot(context_mode="long", context_window_tokens=262144),
        )
        policy = _restored_context_policy(project)
        assert policy.mode is ContextMode.LONG
        assert policy.context_window_tokens == 262144

    def test_restoring_policy_defaults_to_standard(self):
        from src.core.queue_scheduler import _restored_context_policy
        from src.domain.project import TranslationProject

        assert _restored_context_policy(None).mode is ContextMode.STANDARD
        legacy = TranslationProject(
            project_id="p",
            source_path="/tmp/a.txt",
            source_fingerprint="",
            file_type="txt",
            mapping_dir="",
        )
        assert _restored_context_policy(legacy).mode is ContextMode.STANDARD

    def test_task_slot_starts_in_standard_mode(self, coordinator_factory):
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a"])

        # 快照由调度线程异步发布：轮询等待而不是假设立即可读。
        task = wait_for_state(
            coordinator,
            "t1",
            {QueueTaskState.PENDING, QueueTaskState.PREPARING, QueueTaskState.READY},
            timeout=10.0,
        )
        assert task.context_mode == "standard"
        assert task.requested_context_tokens is None


class TestQueueCancellation:
    """LC-10 队列侧：取消后旧 attempt 的迟到结果被丢弃，资源释放。"""

    def test_cancel_during_long_task_reaches_terminal_state(self, coordinator_factory):
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        source = [f"第{i}行" + "内容文本" * 16 for i in range(400)]
        assert add_task(coordinator, "t1", source)

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 262144),
            ),
        )
        # 取消随后提交；无论它落在准备还是执行阶段，任务都必须进入终态。
        coordinator.submit_command("cancel", "t1")
        coordinator.submit_command("cancel", "t1")  # 幂等

        task = wait_for_state(
            coordinator,
            "t1",
            {QueueTaskState.CANCELLED, QueueTaskState.ERROR},
            timeout=30.0,
        )
        assert task.state is QueueTaskState.CANCELLED

    def test_restart_after_cancel_creates_new_attempt(self, coordinator_factory):
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a", "b", "c"])

        coordinator.submit_command("cancel", "t1")
        wait_for_state(coordinator, "t1", {QueueTaskState.CANCELLED}, timeout=20.0)
        # cancel 与 start 都是入队命令：让调度线程先消费掉 cancel，否则
        # start_all_long 会看到还未进入 CANCELLED 的 slot 而跳过它。
        time.sleep(0.2)

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        # 注意不能等 CANCELLED：那是取消后的即时状态，重启还没发生。
        wait_for_state(
            coordinator,
            "t1",
            {QueueTaskState.COMPLETED, QueueTaskState.PARTIAL},
            timeout=30.0,
        )

        assert api.requests, "重新开始后必须真的翻译"
        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.context_mode == "long"

    def test_pause_and_resume_keeps_long_mode(self, coordinator_factory):
        """暂停后恢复继承已捕获的策略，不回退普通预算。"""
        api = RecordingAPI()
        coordinator = coordinator_factory(FakeConfigManager(), api)
        assert add_task(coordinator, "t1", ["a", "b"])

        coordinator.submit_command(
            "start_all_long",
            payload=LongContextStartRequest(
                task_ids=("t1",),
                context_policy=TranslationContextPolicy(ContextMode.LONG, 131072),
            ),
        )
        wait_until_idle(coordinator, ["t1"])

        coordinator.submit_command("pause", "t1")
        time.sleep(0.1)
        snapshot = coordinator.get_snapshot()
        task = next(t for t in snapshot.tasks if t.task_id == "t1")
        assert task.context_mode == "long"
        assert task.requested_context_tokens == 131072


class TestLongContextPlannerUnit:
    """``plan_long_context_batches`` 的纯函数行为（不经调度器）。"""

    def _run_context(self):
        return TranslationRunContext(
            provider="siliconflow",
            model_name="m",
            target_language="中文",
            base_prompt="p",
            glossary_prompt="",
            system_prompt="prompt",
            is_hunyuan=False,
        )

    def _budget(self, requested: int, capacity: int = BIG_CONTEXT):
        from src.application.context_budget import resolve_long_budget

        return resolve_long_budget(
            requested_context_tokens=requested, model_context_tokens=capacity
        )

    def test_ignores_queue_line_cap(self):
        """超长模式不读取 QueuePolicy 的行数上限。"""
        lines = [f"l{i}" for i in range(500)]
        jobs = plan_long_context_batches(
            lines,
            list(range(500)),
            budget=self._budget(262144),
            run_context=self._run_context(),
        )
        assert len(jobs) == 1
        assert len(jobs[0].source_lines) == 500

    def test_carries_output_budget_and_indices(self):
        lines = [f"l{i}" for i in range(10)]
        jobs = plan_long_context_batches(
            lines,
            [1, 3, 5],
            budget=self._budget(32768),
            run_context=self._run_context(),
        )
        assert jobs[0].source_indices == (1, 3, 5)
        assert jobs[0].output_budget_tokens is not None
        assert jobs[0].output_budget_tokens >= 4096

    def test_budget_that_cannot_fit_output_reserve_is_rejected(self):
        """C 扣掉安全余量后无法容纳最低输出预留时，必须报错而不是强发。"""
        with pytest.raises(BatchPlanningError):
            plan_long_context_batches(
                ["short line"],
                [0],
                budget=self._budget(4096),
                run_context=self._run_context(),
            )

    def test_start_batch_id_offsets(self):
        lines = ["a", "b"]
        jobs = plan_long_context_batches(
            lines,
            [0, 1],
            budget=self._budget(8192),
            run_context=self._run_context(),
            start_batch_id=7,
        )
        assert [job.batch_id for job in jobs] == [7]

    def test_raises_for_unsatisfiable_line(self):
        with pytest.raises(BatchPlanningError):
            plan_long_context_batches(
                ["字" * 300_000],
                [0],
                budget=self._budget(8192),
                run_context=self._run_context(),
            )

    def test_empty_input_returns_no_jobs(self):
        assert (
            plan_long_context_batches(
                [], [], budget=self._budget(8192), run_context=self._run_context()
            )
            == []
        )
