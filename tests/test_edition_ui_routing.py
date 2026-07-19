"""Regression tests for Text/Full edition capability routing at UI boundaries."""

import builtins
from types import SimpleNamespace
from unittest.mock import Mock

from src.domain.edition import EditionCapabilities
from src.ui.concurrent_window import ConcurrentWindow
from src.ui.file_importer import FileImporter
from src.ui.settings_window import SettingsWindow


def test_text_queue_never_constructs_manga_provider(monkeypatch):
    """The Text build must not import or instantiate the optional Manga provider."""
    win = ConcurrentWindow.__new__(ConcurrentWindow)
    win.edition_capabilities = EditionCapabilities.text()
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.endswith("manga_provider"):
            raise AssertionError("Manga provider import must not run in Text Edition")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert win._create_manga_provider() is None


def test_text_queue_image_command_stops_before_task_access(monkeypatch):
    """Text Edition rejects the local image command before touching the queue."""
    win = ConcurrentWindow.__new__(ConcurrentWindow)
    win.edition_capabilities = EditionCapabilities.text()
    win.win = SimpleNamespace()
    warning = Mock()
    monkeypatch.setattr("src.ui.concurrent_window.messagebox.showwarning", warning)

    win._translate_all_images()

    warning.assert_called_once()


def test_queue_defaults_to_detected_capability_when_not_injected(monkeypatch):
    """Legacy callers still use the shared runtime edition detector."""
    win = ConcurrentWindow.__new__(ConcurrentWindow)
    monkeypatch.setattr(
        "src.ui.concurrent_window.detect_edition_capabilities",
        EditionCapabilities.full,
    )

    assert win._manga_enabled() is True


def test_text_epub_import_does_not_prompt_for_manga():
    """Importing an EPUB in Text Edition does not invoke the Manga starter."""
    importer = FileImporter.__new__(FileImporter)
    importer.edition_capabilities = EditionCapabilities.text()
    importer.image_translation_starter = Mock(side_effect=AssertionError)

    importer._check_epub_images_for_text()

    importer.image_translation_starter.assert_not_called()


def test_text_settings_manga_probe_is_a_noop():
    """The settings availability check is capability-gated in Text Edition."""
    dialog = SettingsWindow.__new__(SettingsWindow)
    dialog.edition_capabilities = EditionCapabilities.text()
    dialog.manga_status_label = Mock()
    dialog._begin_test = Mock(side_effect=AssertionError("probe must not start"))

    dialog._check_manga_status()

    dialog.manga_status_label.config.assert_called_once()
    dialog._begin_test.assert_not_called()


def test_full_settings_manga_probe_still_starts(monkeypatch):
    """Full Edition keeps the existing asynchronous Manga availability probe."""
    dialog = SettingsWindow.__new__(SettingsWindow)
    dialog.edition_capabilities = EditionCapabilities.full()
    dialog.manga_status_label = Mock()
    dialog._begin_test = Mock(return_value=True)
    started = Mock()
    monkeypatch.setattr("src.ui.settings_window.threading.Thread", started)

    dialog._check_manga_status()

    dialog._begin_test.assert_called_once()
    started.assert_called_once()


def test_main_window_passes_capabilities_to_queue_window(monkeypatch):
    """The queue view receives the same capability contract as the main window."""
    created = []

    class FakeManager:
        def __init__(self, *_args, **_kwargs):
            self.args = _args
            self.kwargs = _kwargs

    class FakeWindow:
        def __init__(self, *_args, **kwargs):
            created.append(kwargs)
            self._closed = False

    monkeypatch.setattr("src.core.concurrent_manager.ConcurrentTranslationManager", FakeManager)
    monkeypatch.setattr("src.ui.concurrent_window.ConcurrentWindow", FakeWindow)

    from src.ui.main_window import MainWindow

    main = MainWindow.__new__(MainWindow)
    main.root = object()
    main.config_manager = object()
    main.app_paths = object()
    main._provider_limiter_registry = object()
    main.edition_capabilities = EditionCapabilities.text()
    main._queue_manager = None
    main._concurrent_window = None

    main.open_concurrent()

    assert created[0]["edition_capabilities"] is main.edition_capabilities
