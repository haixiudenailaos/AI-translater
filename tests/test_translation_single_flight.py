from __future__ import annotations

from src.ui.translation_controller import TranslationController


def _running_controller() -> tuple[TranslationController, list[str]]:
    controller = TranslationController.__new__(TranslationController)
    messages: list[str] = []
    controller.is_translating = True
    controller.status_updater = messages.append
    return controller, messages


def test_full_translation_command_is_rejected_while_run_is_active():
    controller, messages = _running_controller()
    controller.get_table_data = lambda: (_ for _ in ()).throw(AssertionError("must not read data"))

    controller.start_translation()

    assert messages == ["翻译任务正在运行或停止中，请等待当前任务结束"]


def test_continue_translation_command_is_rejected_while_run_is_active():
    controller, messages = _running_controller()
    controller.get_table_data = lambda: (_ for _ in ()).throw(AssertionError("must not read data"))

    controller.continue_translation()

    assert messages == ["翻译任务正在运行或停止中，请等待当前任务结束"]


def test_selected_translation_command_is_rejected_while_run_is_active():
    controller, messages = _running_controller()
    controller.translation_table = object()

    controller.translate_selected_rows()

    assert messages == ["翻译任务正在运行或停止中，请等待当前任务结束"]


def test_session_replacement_cancels_and_retires_active_run():
    """P0-2：导入新会话前停止旧请求并立刻恢复可用状态。"""
    import threading
    from collections import deque
    from types import SimpleNamespace

    class _Widget:
        def __init__(self):
            self.states = []

        def config(self, **kwargs):
            self.states.append(kwargs)

    class _Mailbox:
        def __init__(self):
            self.discarded = []

        def discard_run(self, run_id):
            self.discarded.append(run_id)

    controller = TranslationController.__new__(TranslationController)
    controller.is_translating = True
    controller.translator = SimpleNamespace(call_if_initialized=lambda method: method)
    controller._current_run_id = "old-run"
    controller._current_mode = "full"
    controller._mailbox = _Mailbox()
    controller._pending_results = {"old-run": object()}
    controller._pending_errors = {"old-run": "late error"}
    controller._results_lock = threading.Lock()
    controller._retired_run_ids = deque(maxlen=64)
    controller._continue_missing_indices = [1]
    controller._missing_translation_indices = [1]
    controller._selected_translation_data = [{"item": "row"}]
    controller._streaming_preview_rows = set()
    controller._document = None
    controller._table_adapter = None
    controller.translate_btn = _Widget()
    controller.continue_btn = _Widget()
    controller.stop_btn = _Widget()
    messages = []
    controller.status_updater = messages.append
    save_calls = []
    controller.schedule_save = lambda **kwargs: save_calls.append(kwargs)

    assert controller.cancel_for_session_replacement() is True
    assert controller.is_translating is False
    assert controller._current_run_id is None
    assert controller._mailbox.discarded == ["old-run"]
    assert "old-run" not in controller._pending_results
    assert controller._is_retired_run("old-run")
    assert save_calls == [{"delay_ms": 0}]
    assert messages[-1] == "已停止当前翻译，正在切换文档"
