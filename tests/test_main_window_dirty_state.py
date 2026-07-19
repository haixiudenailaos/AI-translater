from pathlib import Path
from types import SimpleNamespace

from src.application.autosave import CLEAN, DIRTY, SaveResult
from src.application.document_session import DocumentSession, SessionKind
from src.application.translation_document import TranslationDocument
from src.ui.main_window import MainWindow
from src.ui.translation_controller import TranslationController


class _ConfigManager:
    def get_app_config(self):
        return {"auto_save": True}


class _Autosave:
    def __init__(self, state=CLEAN):
        self.state = state
        self.paths = []
        self.dirty_calls = []

    def set_save_paths(self, target_path, mapping_dir):
        self.paths.append((target_path, mapping_dir))

    def mark_dirty(self, **kwargs):
        self.dirty_calls.append(kwargs)


def _make_window(document, *, autosave_state=CLEAN):
    session = DocumentSession(
        session_id="test-session",
        kind=SessionKind.TXT,
        source_path=Path("source.txt"),
        target_path=Path("target.txt"),
        mapping_dir=None,
        document=document,
    )
    window = MainWindow.__new__(MainWindow)
    window._document = document
    window._table_loading = False
    window._disable_auto_save = False
    window._unsaved_edits = False
    window.config_manager = _ConfigManager()
    window.file_importer = SimpleNamespace(
        current_target_path=session.target_path,
        current_mapping_dir=session.mapping_dir,
        session=session,
    )
    window._autosave = _Autosave(state=autosave_state)
    window._set_save_status = lambda _status: None
    window.update_status = lambda _status: None
    return window, session


def test_schedule_save_ignores_clean_document():
    document = TranslationDocument()
    document.replace(["source"], ["translated"])
    window, session = _make_window(document)

    window._schedule_save_to_target()

    assert window._unsaved_edits is False
    assert session.dirty is False
    assert window._autosave.paths == []
    assert window._autosave.dirty_calls == []


def test_schedule_save_marks_session_only_after_model_change():
    document = TranslationDocument()
    document.replace(["source"], [""])
    window, session = _make_window(document)
    document.update_target(0, "translated")

    window._schedule_save_to_target()

    assert window._unsaved_edits is True
    assert session.dirty is True
    assert window._autosave.paths == [(Path("target.txt"), None)]
    assert window._autosave.dirty_calls == [
        {"source": "edit", "debounce_ms": 1000}
    ]


def test_clean_save_result_clears_document_and_session_dirty_state():
    document = TranslationDocument()
    document.replace(["source"], [""])
    document.update_target(0, "translated")
    window, session = _make_window(document)
    window._unsaved_edits = True
    session.mark_dirty()

    window._on_save_result(
        SaveResult(
            generation=document.version,
            succeeded=True,
            elapsed_seconds=0.01,
            bytes_written=10,
        )
    )

    assert document.dirty_indices == frozenset()
    assert window._unsaved_edits is False
    assert session.dirty is False


def test_stale_save_result_keeps_document_dirty():
    document = TranslationDocument()
    document.replace(["source"], [""])
    document.update_target(0, "newer translation")
    window, session = _make_window(document, autosave_state=DIRTY)
    window._unsaved_edits = True
    session.mark_dirty()

    window._on_save_result(
        SaveResult(
            generation=document.version - 1,
            succeeded=True,
            elapsed_seconds=0.01,
            bytes_written=5,
        )
    )

    assert document.dirty_indices == frozenset({0})
    assert window._unsaved_edits is True
    assert session.dirty is True


class _Progress:
    def set(self, _value):
        pass


class _TableAdapter:
    def __init__(self):
        self.updates = []

    def apply_target_updates(self, updates):
        self.updates.append(updates)


def test_rejected_machine_result_does_not_schedule_save():
    document = TranslationDocument()
    document.replace(["source"], ["human translation"])
    document.update_target(0, "human translation", manually_edited=True)
    document.clear_dirty()

    controller = TranslationController.__new__(TranslationController)
    controller._document = document
    controller._table_adapter = _TableAdapter()
    controller._continue_missing_indices = []
    controller._continue_start_line = 0
    controller.progress_var = _Progress()
    # P0-2：_render_translation_progress 在 BATCH_COMPLETED 路径会更新
    # _streaming_preview_rows，测试桩需初始化该集合。
    controller._streaming_preview_rows = set()
    save_calls = []
    controller.schedule_save = lambda **kwargs: save_calls.append(kwargs)

    controller._render_translation_progress(
        100,
        {
            "batch_start": 0,
            "streaming": False,
            "translated_lines": ["late machine translation"],
        },
    )

    assert document.target_lines() == ["human translation"]
    assert document.dirty_indices == frozenset()
    assert controller._table_adapter.updates == [{}]
    assert save_calls == []
