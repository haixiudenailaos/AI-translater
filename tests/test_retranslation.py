from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.application.translation_document import TranslationDocument
from src.ui.translation_controller import TranslationController


class _Button:
    def config(self, **_kwargs):
        pass


def _controller() -> TranslationController:
    controller = TranslationController.__new__(TranslationController)
    controller.is_translating = False
    controller.root = object()
    controller.config_manager = SimpleNamespace(is_api_configured=lambda: True)
    controller.open_settings = lambda: None
    controller.get_table_data = lambda: (["one", "", "three"], ["old", "", "manual"])
    controller._confirm_preflight = lambda action: action == "retranslate"
    controller.start_btn = _Button()
    controller.translate_btn = controller.start_btn
    controller.retranslate_btn = _Button()
    controller.continue_btn = _Button()
    controller.stop_btn = _Button()
    controller._continue_missing_indices = []
    controller._continue_start_line = 0
    controller._current_run_id = None
    controller._current_mode = None
    controller.status_updater = lambda _message: None
    controller._new_run_id = lambda mode: setattr(controller, "_current_mode", mode) or "run"
    controller._translate_worker = lambda _content, _run_id: None
    return controller


def test_retranslate_all_maps_every_non_empty_source_without_clearing_old_targets():
    controller = _controller()
    started = []

    class _Thread:
        daemon = False

        def __init__(self, *, target, args):
            started.append((target, args))

        def start(self):
            started.append("started")

    with (
        patch("src.ui.translation_controller.messagebox.askyesno", return_value=True),
        patch("src.ui.translation_controller.threading.Thread", _Thread),
    ):
        controller.retranslate_all()

    assert controller._continue_missing_indices == [0, 2]
    assert started[0][1] == ("one\nthree", "run")
    assert started[1] == "started"
    assert controller._current_mode == "retranslate"


def test_successful_retranslation_can_replace_manually_edited_row():
    controller = TranslationController.__new__(TranslationController)
    controller._document = TranslationDocument()
    controller._document.replace(["source"], ["old"])
    controller._document.update_target(0, "manual", manually_edited=True)
    controller._current_mode = "retranslate"
    retranslated = []
    controller._on_retranslated = lambda indices: retranslated.append(indices)

    accepted = controller._apply_to_document({0: "new"})

    assert accepted == {0: "new"}
    assert controller._document.target_lines() == ["new"]
    assert controller._document.is_manually_edited(0) is False
    assert retranslated == [{0}]


def test_equal_retranslation_still_clears_manual_marker():
    controller = TranslationController.__new__(TranslationController)
    controller._document = TranslationDocument()
    controller._document.replace(["source"], [""])
    controller._document.update_target(0, "same", manually_edited=True)
    controller._current_mode = "retranslate"
    retranslated = []
    controller._on_retranslated = lambda indices: retranslated.append(indices)

    accepted = controller._apply_to_document({0: "same"})

    assert accepted == {}
    assert controller._document.is_manually_edited(0) is False
    assert retranslated == [{0}]
