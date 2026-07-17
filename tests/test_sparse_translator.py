#!/usr/bin/env python3
"""
稀疏行翻译服务单元测试（UXF-001 / UXF-002）

验证 src/application/sparse_translator.py：
- 只翻译待翻译行（不重复翻译已完成行）
- 通过稳定索引映射结果
- 跳过手工编辑行（UXF-001）
- 失败行保持原值并记录
- 取消时不覆盖已有译文
- translate_failed 只重试失败行
"""

import pytest

from src.application.sparse_translator import SparseLineTranslator
from src.domain.errors import TranslationCancelled
from src.domain.project import TranslationProject
from src.domain.translation import (
    OperationStatus,
    TranslationOptions,
    TranslationProgress,
    TranslationResult,
)


class FakeProvider:
    """测试用 TranslationProvider 替身"""

    def __init__(
        self, results: list[TranslationResult] | None = None, fail_with: Exception | None = None
    ):
        self._results = results or []
        self._fail_with = fail_with
        self._call_count = 0
        self.received_lines: list[list[str]] = []

    def translate_batch(self, lines, options, on_progress=None):
        self.received_lines.append(list(lines))
        if self._fail_with:
            raise self._fail_with
        if self._call_count < len(self._results):
            result = self._results[self._call_count]
            self._call_count += 1
        else:
            result = TranslationResult(
                status=OperationStatus.SUCCEEDED,
                lines=tuple(["OK"] * len(lines)),
            )
        # 模拟进度回调
        if on_progress is not None and result.lines:
            from src.domain.translation import TranslationProgress

            on_progress(
                TranslationProgress(
                    completed=len(result.lines),
                    total=len(lines),
                    batch_start=0,
                    preview_lines=result.lines,
                )
            )
        return result

    def cancel(self):
        pass

    def close(self):
        pass


def _make_project(lines=None, translations=None, manual=None, failed=None):
    orig = lines if lines is not None else ["A", "B", "C", "D"]
    trans = translations if translations is not None else ["", "", "", ""]
    project = TranslationProject(
        project_id="test",
        source_path="/tmp/test.txt",
        source_fingerprint="fp",
        file_type="txt",
        mapping_dir="",
        original_lines=list(orig),
        translated_lines=list(trans),
    )
    if manual:
        for i in manual:
            project.manually_edited_indices.add(i)
            project.translated_lines[i] = f"manual_{i}"
    if failed:
        for i in failed:
            project.failed_indices.add(i)
    return project


def _options(batch_size=20):
    return TranslationOptions(
        target_language="中文",
        model_name="test-model",
        batch_size=batch_size,
    )


# ── translate_pending ───────────────────────


class TestTranslatePending:
    def test_translates_all_pending(self):
        """翻译所有待翻译行"""
        project = _make_project()
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        result = service.translate_pending(project, _options())

        assert result.status == OperationStatus.SUCCEEDED
        assert len(result.lines) == 4
        assert all(t == "OK" for t in result.lines)
        assert project.translated_count == 4

    def test_skips_already_translated(self):
        """不重复翻译已翻译行"""
        project = _make_project(translations=["甲", "", "", "丁"])
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        service.translate_pending(project, _options())

        # 只翻译了2行（索引1和2）
        assert len(provider.received_lines) == 1
        assert len(provider.received_lines[0]) == 2
        assert provider.received_lines[0] == ["B", "C"]
        # 已翻译行不变
        assert project.translated_lines[0] == "甲"
        assert project.translated_lines[3] == "丁"

    def test_skips_manual_edited(self):
        """跳过手工编辑行（UXF-001）"""
        project = _make_project(manual={1})
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        service.translate_pending(project, _options())

        # 手工行不被翻译
        assert project.translated_lines[1] == "manual_1"
        # 其他行被翻译
        assert project.translated_lines[0] == "OK"
        assert project.translated_lines[2] == "OK"
        assert project.translated_lines[3] == "OK"

    def test_no_pending_returns_success(self):
        """没有待翻译行时返回成功"""
        project = _make_project(translations=["甲", "乙", "丙", "丁"])
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        result = service.translate_pending(project, _options())
        assert result.status == OperationStatus.SUCCEEDED
        assert provider.received_lines == []  # 没有调用


# ── translate_lines（稀疏行） ───────────────


class TestTranslateLines:
    def test_translate_specific_indices(self):
        """翻译指定稀疏索引"""
        project = _make_project()
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        service.translate_lines(project, [0, 2], _options())

        assert project.translated_lines[0] == "OK"
        assert project.translated_lines[1] == ""  # 未翻译
        assert project.translated_lines[2] == "OK"
        assert project.translated_lines[3] == ""  # 未翻译

    def test_does_not_touch_behind_translated(self):
        """UXF-002：文档中间有空译文、后方有已译行，续翻不修改后方译文"""
        project = _make_project(translations=["甲", "", "", "丁"])
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        # 只翻译索引1（中间空行）
        service.translate_lines(project, [1], _options())

        assert project.translated_lines[0] == "甲"  # 不变
        assert project.translated_lines[1] == "OK"  # 新翻译
        assert project.translated_lines[2] == ""  # 未触及
        assert project.translated_lines[3] == "丁"  # 不变

    def test_empty_indices_returns_success(self):
        project = _make_project()
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        result = service.translate_lines(project, [], _options())
        assert result.status == OperationStatus.SUCCEEDED
        assert provider.received_lines == []

    def test_skip_out_of_range(self):
        """越界索引被跳过"""
        project = _make_project()
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        service.translate_lines(project, [0, 100], _options())
        assert project.translated_lines[0] == "OK"

    def test_skip_empty_original(self):
        """空原文行被跳过"""
        project = _make_project(lines=["A", "", "C"])
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        service.translate_lines(project, [0, 1, 2], _options())
        # 索引1是空原文，不翻译
        assert project.translated_lines[0] == "OK"
        assert project.translated_lines[1] == ""
        assert project.translated_lines[2] == "OK"


# ── 失败处理 ───────────────────────────────


class TestFailureHandling:
    def test_partial_failure_records_failed(self):
        """部分失败记录失败行"""
        project = _make_project()
        provider = FakeProvider(
            results=[
                TranslationResult(
                    status=OperationStatus.PARTIAL,
                    lines=("OK", ""),
                    failed_indices=(1,),
                    error_message="timeout",
                ),
            ]
        )
        service = SparseLineTranslator(provider)

        result = service.translate_pending(project, _options(batch_size=2))

        assert result.status == OperationStatus.PARTIAL
        # failed_indices 应映射到全局索引
        assert 1 in result.failed_indices
        assert project.translated_lines[0] == "OK"
        assert 1 in project.failed_indices

    def test_batch_exception_continues(self):
        """批次异常后继续后续批次"""
        project = _make_project(lines=["A", "B", "C", "D"])
        provider = FakeProvider(fail_with=RuntimeError("network"))
        service = SparseLineTranslator(provider)

        result = service.translate_pending(project, _options(batch_size=2))

        assert result.status == OperationStatus.FAILED
        assert len(result.failed_indices) == 4
        assert project.status.value in ("error", "partial")

    def test_cancelled_raises(self):
        """取消时抛出 TranslationCancelled"""
        project = _make_project()
        provider = FakeProvider(fail_with=TranslationCancelled())
        service = SparseLineTranslator(provider)

        with pytest.raises(TranslationCancelled):
            service.translate_pending(project, _options())


# ── translate_failed ───────────────────────


class TestTranslateFailed:
    def test_only_retries_failed(self):
        """只重试失败行，不重复成功行"""
        project = _make_project(
            translations=["甲", "乙", "丙", "丁"],
            failed=[1],
        )
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        service.translate_failed(project, _options())

        # 只翻译了索引1
        assert len(provider.received_lines) == 1
        assert provider.received_lines[0] == ["B"]
        # 成功行不变
        assert project.translated_lines[0] == "甲"
        # 失败行已修复
        assert project.translated_lines[1] == "OK"
        assert 1 not in project.failed_indices

    def test_no_failed_returns_success(self):
        project = _make_project(translations=["甲", "乙", "丙", "丁"])
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        result = service.translate_failed(project, _options())
        assert result.status == OperationStatus.SUCCEEDED
        assert provider.received_lines == []


# ── 进度回调 ───────────────────────────────


class TestProgressCallback:
    def test_progress_callback_invoked(self):
        project = _make_project()
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        events: list[TranslationProgress] = []
        service.translate_pending(
            project,
            _options(),
            on_progress=lambda e: events.append(e),
        )

        assert len(events) > 0
        assert events[0].total == 4  # 4个待翻译行

    def test_progress_callback_none_works(self):
        """on_progress=None 正常工作"""
        project = _make_project()
        provider = FakeProvider()
        service = SparseLineTranslator(provider)

        result = service.translate_pending(project, _options(), on_progress=None)
        assert result.status == OperationStatus.SUCCEEDED
