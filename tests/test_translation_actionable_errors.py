"""UX-3/UX-6: main-editor errors use safe classification and bounded retry."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.application.error_handling import ActionableError, ErrorCategory, RetryPolicy
from src.ui.translation_controller import TranslationController


def _controller_for_errors() -> TranslationController:
    controller = TranslationController.__new__(TranslationController)
    controller.is_translating = True
    controller.translate_btn = MagicMock()
    controller.continue_btn = MagicMock()
    controller.stop_btn = MagicMock()
    controller._streaming_preview_rows = set()
    controller._document = None
    controller._table_adapter = None
    controller.status_updater = MagicMock()
    controller._missing_check_rounds = 1
    controller._missing_retry_policy = RetryPolicy(max_attempts=2, base_delay_seconds=1.0)
    controller._start_missing_translation_check = MagicMock()
    controller.root = MagicMock()
    return controller


def test_main_translation_error_dialog_is_sanitized(monkeypatch):
    controller = _controller_for_errors()
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.ui.translation_controller.messagebox.showerror",
        lambda title, message: dialogs.append((title, message)),
    )

    controller._handle_translation_failed(
        "HTTP 401 unauthorized Authorization: Bearer secret-token-value-12345"
    )

    assert controller.is_translating is False
    assert len(dialogs) == 1
    assert "secret-token-value-12345" not in dialogs[0][1]
    assert "建议操作：" in dialogs[0][1]
    assert "诊断编号：" in dialogs[0][1]


def test_non_retryable_missing_error_stops_automatic_retry(monkeypatch):
    controller = _controller_for_errors()
    controller._show_actionable_error = MagicMock()
    actionable = ActionableError(
        ErrorCategory.AUTHENTICATION,
        "认证失败",
        "检查 API Key。",
        False,
        "abc123",
    )

    controller._schedule_missing_check_retry(actionable)

    controller.root.after.assert_not_called()
    controller._show_actionable_error.assert_called_once_with(actionable)
    assert controller._missing_check_rounds == 0


def test_retryable_missing_error_uses_jittered_backoff():
    controller = _controller_for_errors()
    controller._show_actionable_error = MagicMock()
    actionable = ActionableError(
        ErrorCategory.NETWORK,
        "网络连接失败",
        "检查网络后重试。",
        True,
        "abc123",
    )

    controller._schedule_missing_check_retry(actionable)

    controller.root.after.assert_called_once()
    delay_ms, callback = controller.root.after.call_args.args
    assert 800 <= delay_ms <= 1200
    assert callback is controller._start_missing_translation_check
    controller._show_actionable_error.assert_not_called()
