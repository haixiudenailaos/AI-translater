from __future__ import annotations

import hashlib

from src.application.preflight import PreflightSeverity, build_preflight_report
from src.application.quality_review import QualityIssueType, inspect_quality
from src.application.usage import UsageStatistics
from src.core.translator import TranslatorEngine
from src.domain.project import TranslationProject
from src.domain.translation import TranslationOptions
from src.ui.main_window import MainWindow


def _project(source: list[str], target: list[str]) -> TranslationProject:
    return TranslationProject(
        project_id="test-project",
        source_path="book.txt",
        source_fingerprint="fingerprint",
        file_type="txt",
        mapping_dir="",
        original_lines=source,
        translated_lines=target,
    )


def test_preflight_allows_valid_small_translation_without_issues():
    report = build_preflight_report(
        _project(["A short source line."], [""]),
        TranslationOptions(target_language="中文", model_name="test-model"),
        provider="test-provider",
    )

    assert report.can_start
    assert report.issues == ()
    assert report.pending_lines == 1


def test_preflight_blocks_missing_model_and_target_language():
    report = build_preflight_report(
        _project(["source"], [""]),
        TranslationOptions(target_language="", model_name=""),
    )

    assert not report.can_start
    assert {issue.code for issue in report.issues} == {"missing_model", "missing_target_language"}
    assert all(issue.severity is PreflightSeverity.ERROR for issue in report.issues)


def test_preflight_reports_long_line_as_confirmation_warning():
    report = build_preflight_report(
        _project(["x" * 21], [""]),
        TranslationOptions(target_language="中文", model_name="test-model"),
        long_line_threshold=20,
    )

    assert report.can_start
    assert report.issues[0].code == "long_source_lines"
    assert report.issues[0].severity is PreflightSeverity.WARNING


def test_quality_review_detects_deterministic_line_level_issues():
    report = inspect_quality(
        _project(
            [
                "Alice has 12 apples.",
                "Bob says hello.",
                "Cara says hello.",
                "Magic Sword appears.",
            ],
            [
                "Alice 有 10 个苹果。[LINE_1]",
                "相同的译文内容。",
                "相同的译文内容。",
                "魔法武器出现。",
            ],
        ),
        glossary_terms=(("Magic Sword", "魔剑"),),
    )

    issue_types = {issue.issue_type for issue in report.issues}
    assert QualityIssueType.NUMBER_MISMATCH in issue_types
    assert QualityIssueType.INTERNAL_MARKER in issue_types
    assert QualityIssueType.REPEATED_TRANSLATION in issue_types
    assert QualityIssueType.GLOSSARY_MISS in issue_types


def test_usage_statistics_merges_only_monotonic_metric_deltas():
    usage = UsageStatistics()
    first = {
        "successful_requests": 3,
        "retries": 1,
        "cache_hits": 2,
        "input_tokens_estimated": 120,
        "output_tokens_estimated": 80,
    }
    second = {
        "successful_requests": 5,
        "retries": 1,
        "cache_hits": 4,
        "input_tokens_estimated": 200,
        "output_tokens_estimated": 130,
    }

    usage.record_metrics_delta(first)
    usage.record_metrics_delta(second, first)
    usage.record_metrics_delta(first, second)

    assert usage.requests == 5
    assert usage.retries == 1
    assert usage.cache_hits == 4
    assert usage.input_tokens == 200
    assert usage.output_tokens == 130


def test_queue_run_context_uses_the_same_schema_versioned_cache_key():
    class Config:
        def get_app_config(self):
            return {
                "target_language": "中文",
                "translation_prompt": "translate faithfully",
                "prompt_schema_version": 2,
            }

        def get_api_config(self):
            return {"provider": "test", "model_name": "test-model", "temperature": 0.3}

        def get_glossary_prompt(self):
            return ""

    context = TranslatorEngine(Config()).build_run_context()
    expected = hashlib.sha256(b"schema:2\ntranslate faithfully").hexdigest()[:16]

    assert context.prompt_schema_version == 2
    assert context.prompt_version == expected


def _preflight_window(project: TranslationProject, app_config: dict[str, object]) -> MainWindow:
    class Config:
        def get_app_config(self):
            return app_config

        def get_api_config(self, *, load_secret=False):
            return {
                "provider": "test",
                "model_name": "test-model",
                "temperature": 0.3,
            }

    window = MainWindow.__new__(MainWindow)
    window._build_runtime_project = lambda: project
    window._glossary_pairs = lambda: ()
    window.config_manager = Config()
    window.root = object()
    window.update_status = lambda _message: None
    return window


def test_main_window_preflight_runs_silently_when_no_risks(monkeypatch):
    window = _preflight_window(
        _project(["short source"], [""]),
        {"target_language": "中文", "batch_lines": 20, "preflight_confirm_warnings": True},
    )
    dialogs: list[str] = []
    monkeypatch.setattr(
        "src.ui.main_window.messagebox.showerror", lambda *args, **kwargs: dialogs.append("error")
    )
    monkeypatch.setattr(
        "src.ui.main_window.messagebox.askyesno", lambda *args, **kwargs: dialogs.append("warning")
    )

    assert window._run_preflight("full")
    assert dialogs == []


def test_retranslation_preflight_estimates_every_non_empty_source(monkeypatch):
    project = _project(["first", "second"], ["译文一", "译文二"])
    project.completed_indices.update({0, 1})
    project.manually_edited_indices.add(1)
    window = _preflight_window(
        project,
        {"target_language": "中文", "batch_lines": 20, "preflight_confirm_warnings": True},
    )
    monkeypatch.setattr(
        "src.ui.main_window.messagebox.showerror",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected error")),
    )

    assert window._run_preflight("retranslate")
    assert window._last_preflight_report.pending_lines == 2
    assert window._last_preflight_report.translated_lines == 0


def test_main_window_preflight_stops_when_user_rejects_warning(monkeypatch):
    window = _preflight_window(
        _project(["x" * 2_001], [""]),
        {"target_language": "中文", "batch_lines": 20, "preflight_confirm_warnings": True},
    )
    monkeypatch.setattr("src.ui.main_window.messagebox.askyesno", lambda *args, **kwargs: False)

    assert not window._run_preflight("full")


def test_main_window_records_usage_from_cumulative_api_metrics():
    class Translator:
        def __init__(self):
            self.snapshot = {
                "successful_requests": 2,
                "retries": 1,
                "cache_hits": 3,
                "input_tokens_estimated": 100,
                "output_tokens_estimated": 40,
            }

        def call_if_initialized(self, method: str):
            assert method == "get_usage_snapshot"
            return self.snapshot

    window = MainWindow.__new__(MainWindow)
    window.translator = Translator()
    window._usage_statistics = UsageStatistics()
    window._last_usage_snapshot = {}

    window._record_translation_usage(None, "full")
    window.translator.snapshot = {
        "successful_requests": 3,
        "retries": 2,
        "cache_hits": 4,
        "input_tokens_estimated": 160,
        "output_tokens_estimated": 90,
    }
    window._record_translation_usage(None, "full")

    assert window._usage_statistics.to_dict() == {
        "requests": 3,
        "retries": 2,
        "cache_hits": 4,
        "cache_hit_rate": 4 / 7,
        "input_tokens": 160,
        "output_tokens": 90,
        "estimated_cost": 0.0,
    }
