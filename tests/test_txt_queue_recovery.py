#!/usr/bin/env python3
"""UX-2：TXT 队列跨重启续传测试。

覆盖：
- ``ProjectRepository.find_by_source_path``：按路径查找历史项目。
- ``QueueTranslationCoordinator.add_task`` TXT 分支：
  - 首次添加：创建项目文件，target_lines 全空。
  - 同指纹重启：恢复 translated_lines / manually_edited / failed_indices。
  - 指纹变化：通过 callback 询问 new / map / discard。
- 检查点保存：成功批次后写入 ProjectRepository，``_译文.txt`` 同时存在。
- ``_compute_missing_indices_locked``：
  - 已完成行不重译。
  - 手工编辑行不被自动覆盖。
  - 失败行仍进入待翻译集合。
- ``update_task_line``：标记 manually_edited，防止后续自动翻译覆盖。

这些测试通过 monkey-patch ``TranslatorEngine`` 替换为 MockEngine，
避免真实 API 调用，但验证完整的项目仓库恢复流程。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from src.core.concurrent_manager import ConcurrentTranslationManager
from src.core.translation_result import BatchTranslationResult
from src.domain.translation import OperationStatus
from src.infrastructure.project_repository import (
    ProjectRepository,
    compute_file_fingerprint,
    compute_project_id,
)

# ── MockEngine 与 fixtures ─────────────────────────────


class MockEngine:
    """Mock TranslatorEngine 用于项目恢复测试。

    模拟 ``_translate_batch`` 成功翻译每行为 ``译:<原文>``。
    """

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.is_stopped = False
        self.api = MagicMock()
        self._translate_calls: List[List[str]] = []
        self.close_called = False
        self.stop_called = False
        self.raise_on_next: Exception | None = None
        # 自定义结果工厂：用于让某些行失败
        self.result_factory = None

    def _ensure_api(self):
        pass

    def _init_api(self):
        pass

    def build_run_context(self):
        from src.core.translator import TranslationRunContext

        return TranslationRunContext(
            provider="siliconflow",
            model_name="deepseek-ai/DeepSeek-V3.2",
            target_language="中文",
            base_prompt="translate",
            glossary_prompt="",
            system_prompt=None,
            is_hunyuan=False,
            temperature=0.3,
            prompt_version="abc",
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
        self._translate_calls.append(list(batch_lines))
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        if self.result_factory is not None:
            return self.result_factory(batch_lines)
        return BatchTranslationResult(
            status=OperationStatus.SUCCEEDED,
            lines=[f"译:{line}" for line in batch_lines],
        )

    def refresh_api(self):
        pass

    def close(self):
        self.close_called = True

    def stop(self):
        self.stop_called = True
        self.is_stopped = True

    def reset(self):
        self.is_stopped = False


@pytest.fixture
def patch_engine(monkeypatch):
    """替换 ``TranslatorEngine`` 为 ``MockEngine``。"""
    from src.core import queue_scheduler

    instances: List[MockEngine] = []

    def _factory(config_manager):
        engine = MockEngine(config_manager)
        instances.append(engine)
        return engine

    monkeypatch.setattr(queue_scheduler, "TranslatorEngine", _factory)
    return instances


def _wait_for_completion(manager, task_id, timeout=5.0):
    """等待任务进入终态（COMPLETED / PARTIAL / ERROR / CANCELLED）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.get_task(task_id)
        if task is None:
            return None
        if task.status in ("completed", "partial", "error", "cancelled"):
            return task
        time.sleep(0.05)
    return manager.get_task(task_id)


# ── ProjectRepository.find_by_source_path ──────────────


class TestFindBySourcePath:
    def test_returns_none_when_no_projects(self, tmp_path):
        repo = ProjectRepository(tmp_path / "projects")
        assert repo.find_by_source_path("/nonexistent/path.txt") is None

    def test_finds_project_by_path(self, tmp_path):
        repo = ProjectRepository(tmp_path / "projects")
        # 创建一个项目
        project = repo.create(
            source_path="/some/path/novel.txt",
            source_fingerprint="abc123",
            file_type="txt",
            mapping_dir="",
            original_lines=["line1", "line2"],
        )
        project.translated_lines = ["译1", "译2"]
        repo.save(project)

        found = repo.find_by_source_path("/some/path/novel.txt")
        assert found is not None
        assert found.project_id == project.project_id
        assert found.translated_lines == ["译1", "译2"]

    def test_returns_most_recent_for_duplicate_paths(self, tmp_path):
        """同一路径多个历史项目（指纹不同）时返回最近打开的。

        ``_now_iso`` 用秒精度时间戳，同秒内保存的两个项目时间戳相同，
        排序后相对顺序由插入顺序决定。这里通过 sleep 让两个项目时间戳
        不同，验证 list_recent 按 last_opened_at 倒序的语义。
        """
        import time as _time

        repo = ProjectRepository(tmp_path / "projects")
        # 第一个项目
        p1 = repo.create(
            source_path="/some/novel.txt",
            source_fingerprint="fp1",
            file_type="txt",
            mapping_dir="",
            original_lines=["a"],
        )
        repo.save(p1)
        # 让 last_opened_at 不同（秒精度）
        _time.sleep(1.1)
        # 第二个项目（同路径，不同指纹）
        p2 = repo.create(
            source_path="/some/novel.txt",
            source_fingerprint="fp2",
            file_type="txt",
            mapping_dir="",
            original_lines=["a"],
        )
        repo.save(p2)

        found = repo.find_by_source_path("/some/novel.txt")
        assert found is not None
        # 应返回最近保存的（p2 在 p1 之后）
        assert found.source_fingerprint == "fp2"


# ── TXT 队列跨重启恢复 ────────────────────────────────


class TestTxtQueueRecovery:
    """TXT 任务在重启后恢复译文与行级标记。"""

    def test_first_add_creates_project_file(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """首次添加 TXT 任务：创建空项目文件，target_lines 全空。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            assert task.task_id is not None
            # 等待 Coordinator 处理 add_task（项目创建是同步的）
            time.sleep(0.1)

            # 项目文件应已创建
            fingerprint = compute_file_fingerprint(source)
            project_id = compute_project_id(str(source), fingerprint)
            project_file = Path(tmp_app_paths.data_dir) / "projects" / f"{project_id}.json"
            assert project_file.exists(), "项目文件应在 add_task 后创建"

            # 重新加载项目：译文应为空
            repo = ProjectRepository(Path(tmp_app_paths.data_dir) / "projects")
            loaded = repo.load(project_id)
            assert loaded is not None
            assert loaded.original_lines == ["第一行", "第二行", "第三行"]
            assert loaded.translated_lines == ["", "", ""]
        finally:
            manager.close()

    def test_same_fingerprint_restores_translated_lines(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """同指纹重启：恢复译文，已完成行不重新请求。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        # 第一阶段：翻译到完成，保存项目状态
        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            manager1.start_task(task1.task_id)
            completed = _wait_for_completion(manager1, task1.task_id)
            assert completed is not None
            assert completed.status == "completed"
            assert completed.target_lines == ["译:第一行", "译:第二行", "译:第三行"]
        finally:
            manager1.close()

        # 等待检查点 flush 完成
        time.sleep(0.3)

        # 记录 manager2 之前的 MockEngine 数量，便于只检查 manager2 的调用
        engines_before_manager2 = len(patch_engine)

        # 第二阶段：重新添加同一文件，应恢复译文
        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task2 = manager2.add_task(str(source))
            time.sleep(0.1)
            restored = manager2.get_task(task2.task_id)
            assert restored.target_lines == ["译:第一行", "译:第二行", "译:第三行"]

            # 启动任务：应该没有批次需要翻译（全部已完成）
            manager2.start_task(task2.task_id)
            completed2 = _wait_for_completion(manager2, task2.task_id, timeout=2.0)
            assert completed2 is not None
            assert completed2.status == "completed"
            # 不应触发 API 调用（只检查 manager2 创建的引擎）
            manager2_engines = patch_engine[engines_before_manager2:]
            assert all(len(e._translate_calls) == 0 for e in manager2_engines), (
                f"manager2 引擎不应发起翻译调用，"
                f"实际调用: {[e._translate_calls for e in manager2_engines]}"
            )
        finally:
            manager2.close()

    def test_different_fingerprint_invokes_callback(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """指纹变化（同路径不同内容）：触发回调。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        # 第一阶段：翻译完成
        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            manager1.start_task(task1.task_id)
            _wait_for_completion(manager1, task1.task_id)
        finally:
            manager1.close()
        time.sleep(0.3)

        # 修改源文件内容（行数相同，内容不同 → 指纹变化）
        source.write_text("新的第一行\n新的第二行\n新的第三行", encoding="utf-8")

        # 第二阶段：注册回调，添加同路径文件
        callback_calls: List[dict] = []

        def mismatch_callback(info: dict) -> str:
            callback_calls.append(info)
            return "new"  # 不复用旧进度

        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            manager2.set_fingerprint_mismatch_callback(mismatch_callback)
            task2 = manager2.add_task(str(source))
            time.sleep(0.1)

            # 回调应被调用
            assert len(callback_calls) == 1
            assert callback_calls[0]["file_path"] == str(source)
            assert callback_calls[0]["old_fingerprint"] != callback_calls[0]["new_fingerprint"]

            # 新任务：target_lines 全空
            restored = manager2.get_task(task2.task_id)
            assert restored.target_lines == ["", "", ""]
        finally:
            manager2.close()

    def test_fingerprint_mismatch_map_choice_restores_progress(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """指纹变化时用户选择 map：行数相同则复用旧译文。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        # 第一阶段：翻译完成
        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            manager1.start_task(task1.task_id)
            _wait_for_completion(manager1, task1.task_id)
        finally:
            manager1.close()
        time.sleep(0.3)

        # 修改源文件：仅修改每行内容（保持行数相同）
        source.write_text("改第一行\n改第二行\n改第三行", encoding="utf-8")

        def map_callback(info: dict) -> str:
            return "map"

        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            manager2.set_fingerprint_mismatch_callback(map_callback)
            task2 = manager2.add_task(str(source))
            time.sleep(0.1)

            # 用户选 map：复用旧译文
            restored = manager2.get_task(task2.task_id)
            assert restored.target_lines == ["译:第一行", "译:第二行", "译:第三行"]
        finally:
            manager2.close()

    def test_fingerprint_mismatch_discard_deletes_old_project(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """指纹变化时用户选择 discard：删除旧项目。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行", encoding="utf-8")

        # 第一阶段：翻译完成
        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            manager1.start_task(task1.task_id)
            _wait_for_completion(manager1, task1.task_id)
        finally:
            manager1.close()
        time.sleep(0.3)

        old_fingerprint = compute_file_fingerprint(source)
        old_project_id = compute_project_id(str(source), old_fingerprint)
        old_project_file = Path(tmp_app_paths.data_dir) / "projects" / f"{old_project_id}.json"
        assert old_project_file.exists()

        # 修改源文件
        source.write_text("新的第一行\n新的第二行", encoding="utf-8")

        def discard_callback(info: dict) -> str:
            return "discard"

        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            manager2.set_fingerprint_mismatch_callback(discard_callback)
            manager2.add_task(str(source))
            time.sleep(0.1)

            # 旧项目文件应被删除
            assert not old_project_file.exists()
        finally:
            manager2.close()


# ── manually_edited_indices 保护 ──────────────────────


class TestManuallyEditedProtection:
    """手工编辑行不被自动翻译覆盖。"""

    def test_update_task_line_marks_manually_edited(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            time.sleep(0.1)

            # 用户手工编辑第 1 行（索引 1）
            assert manager.update_task_line(task.task_id, 1, "手工译文") is True

            # 启动翻译：第 1 行应被跳过
            manager.start_task(task.task_id)
            completed = _wait_for_completion(manager, task.task_id)
            assert completed is not None
            assert completed.target_lines[1] == "手工译文"  # 未被覆盖
            assert completed.target_lines[0] == "译:第一行"
            assert completed.target_lines[2] == "译:第三行"

            # MockEngine 不应收到第 1 行
            engines = patch_engine
            assert engines, "至少应有一个 MockEngine 实例"
            # 收集所有被翻译的行
            translated_lines = []
            for e in engines:
                for batch in e._translate_calls:
                    translated_lines.extend(batch)
            assert "第二行" not in translated_lines, "手工编辑行不应被发送给翻译引擎"
        finally:
            manager.close()

    def test_manually_edited_persists_across_restart(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """手工编辑在重启后保留，且不被自动翻译覆盖。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        # 第一阶段：手工编辑第 1 行并翻译其余
        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            time.sleep(0.1)
            manager1.update_task_line(task1.task_id, 1, "手工译文")
            manager1.start_task(task1.task_id)
            completed = _wait_for_completion(manager1, task1.task_id)
            assert completed.target_lines[1] == "手工译文"
        finally:
            manager1.close()
        time.sleep(0.3)

        # 第二阶段：重新添加同一文件，恢复状态
        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task2 = manager2.add_task(str(source))
            time.sleep(0.1)
            restored = manager2.get_task(task2.task_id)
            assert restored.target_lines[1] == "手工译文"

            # 启动翻译：手工行应被跳过
            manager2.start_task(task2.task_id)
            completed2 = _wait_for_completion(manager2, task2.task_id)
            assert completed2.target_lines[1] == "手工译文"  # 仍为手工值
        finally:
            manager2.close()


# ── failed_indices 恢复 ────────────────────────────────


class TestFailedIndicesRecovery:
    """失败行在重启后保留。"""

    def test_failed_indices_restored_on_restart(
        self, tmp_path, tmp_app_paths, tmp_config_manager, monkeypatch
    ):
        """失败行在重启后保留。"""
        from src.core import queue_scheduler

        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行\n第三行", encoding="utf-8")

        # 自定义 engine 工厂：让第 1 行（"第二行"）失败
        def fail_on_second(batch_lines):
            lines = []
            failed = []
            for i, line in enumerate(batch_lines):
                if "第二行" in line:
                    lines.append("")
                    failed.append(i)
                else:
                    lines.append(f"译:{line}")
            return BatchTranslationResult(
                status=OperationStatus.PARTIAL,
                lines=lines,
                failed_indices=failed,
            )

        class FailingMockEngine(MockEngine):
            def __init__(self, config_manager):
                super().__init__(config_manager)
                self.result_factory = fail_on_second

        monkeypatch.setattr(queue_scheduler, "TranslatorEngine", FailingMockEngine)

        # 第一阶段：第 1 行失败
        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            manager1.start_task(task1.task_id)
            completed = _wait_for_completion(manager1, task1.task_id, timeout=5.0)
            assert completed is not None
            # 第 1 行应失败
            assert (
                1 in completed.failed_indices
            ), f"第 1 行应失败，实际 failed_indices={completed.failed_indices}"
        finally:
            manager1.close()
        time.sleep(0.3)

        # 第二阶段：重启后失败行应保留
        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task2 = manager2.add_task(str(source))
            time.sleep(0.1)
            restored = manager2.get_task(task2.task_id)
            # 失败行索引应被恢复
            assert (
                1 in restored.failed_indices
            ), f"重启后应保留失败行索引 1，实际 failed_indices={restored.failed_indices}"
        finally:
            manager2.close()


# ── 检查点保存写入 ProjectRepository ───────────────────


class TestCheckpointSavesProject:
    """检查点保存同时写 ``_译文.txt`` 和 ProjectRepository。"""

    def test_checkpoint_writes_both_translation_file_and_project(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            manager.start_task(task.task_id)
            completed = _wait_for_completion(manager, task.task_id)
            assert completed is not None
            assert completed.status == "completed"
        finally:
            manager.close()
        # 等待 close 完成最后一次 flush
        time.sleep(0.3)

        # 1. _译文.txt 应存在（向后兼容导出格式）
        translated_file = source.with_name("novel_译文.txt")
        assert translated_file.exists()
        content = translated_file.read_text(encoding="utf-8")
        assert "译:第一行" in content
        assert "译:第二行" in content

        # 2. 项目文件应存在且包含译文
        fingerprint = compute_file_fingerprint(source)
        project_id = compute_project_id(str(source), fingerprint)
        project_file = Path(tmp_app_paths.data_dir) / "projects" / f"{project_id}.json"
        assert project_file.exists()

        repo = ProjectRepository(Path(tmp_app_paths.data_dir) / "projects")
        project = repo.load(project_id)
        assert project is not None
        assert project.translated_lines == ["译:第一行", "译:第二行"]
        assert project.original_lines == ["第一行", "第二行"]
        assert project.source_fingerprint == fingerprint

    def test_project_repository_is_primary_recovery_source(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """即使 _译文.txt 被删除，项目仓库仍能完整恢复状态。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行", encoding="utf-8")

        # 第一阶段：翻译并保存
        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            manager1.start_task(task1.task_id)
            _wait_for_completion(manager1, task1.task_id)
        finally:
            manager1.close()
        time.sleep(0.3)

        # 删除 _译文.txt，模拟只依赖项目仓库的场景
        translated_file = source.with_name("novel_译文.txt")
        if translated_file.exists():
            translated_file.unlink()

        # 第二阶段：重启，应仍能恢复译文
        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task2 = manager2.add_task(str(source))
            time.sleep(0.1)
            restored = manager2.get_task(task2.task_id)
            assert restored.target_lines == ["译:第一行", "译:第二行"]
        finally:
            manager2.close()


# ── _compute_missing_indices_locked 行为 ───────────────


class TestComputeMissingIndices:
    """``_compute_missing_indices_locked`` 的边界条件。"""

    def test_empty_string_as_valid_translation(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """空字符串作为有效译文结果：通过 completed_indices 跳过，不被重译。

        这是 UX-2 的核心要求："不能用译文非空推断完成，因为空字符串可能是有效结果"。
        通过显式 completed_indices 标记，空译文行不会被自动重译。
        """
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            time.sleep(0.1)

            # 直接通过 Coordinator API 标记第 0 行为完成（译文为空字符串）
            slot = manager._coordinator._tasks[task.task_id]
            slot.target_lines[0] = ""  # 空译文
            slot.completed_indices.add(0)  # 但显式标记为完成

            # 启动翻译：第 0 行应被跳过，只翻译第 1 行
            manager.start_task(task.task_id)
            completed = _wait_for_completion(manager, task.task_id)
            assert completed is not None

            # 第 0 行保持空字符串（未被重译）
            assert completed.target_lines[0] == ""
            # 第 1 行被翻译
            assert completed.target_lines[1] == "译:第二行"

            # 验证 MockEngine 没有收到第 0 行
            translated_lines = []
            for e in patch_engine:
                for batch in e._translate_calls:
                    translated_lines.extend(batch)
            assert "第一行" not in translated_lines, "completed_indices 标记的空译文行不应被重译"
        finally:
            manager.close()

    def test_completed_empty_translation_survives_restart(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n第二行", encoding="utf-8")

        manager1 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task1 = manager1.add_task(str(source))
            slot = manager1._coordinator._tasks[task1.task_id]
            slot.completed_indices.add(0)
            assert manager1.save_task(task1.task_id)
        finally:
            manager1.close()

        manager2 = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task2 = manager2.add_task(str(source))
            slot = manager2._coordinator._tasks[task2.task_id]
            assert slot.completed_indices == {0}
            assert manager2._coordinator._compute_missing_indices_locked(slot) == [1]
        finally:
            manager2.close()

    def test_blank_source_lines_are_skipped(
        self, tmp_path, tmp_app_paths, tmp_config_manager, patch_engine
    ):
        """原文为空或纯空白的行：不进入待翻译集合。"""
        source = tmp_path / "novel.txt"
        source.write_text("第一行\n\n第三行", encoding="utf-8")

        manager = ConcurrentTranslationManager(tmp_config_manager, app_paths=tmp_app_paths)
        try:
            task = manager.add_task(str(source))
            time.sleep(0.1)

            slot = manager._coordinator._tasks[task.task_id]
            missing = manager._coordinator._compute_missing_indices_locked(slot)
            # 第 1 行（空行）不应在 missing 中
            assert 0 in missing
            assert 1 not in missing  # 空行跳过
            assert 2 in missing
        finally:
            manager.close()
