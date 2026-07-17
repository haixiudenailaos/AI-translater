import tempfile
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.application.translation_document import TranslationDocument
from src.config.config_manager import ConfigManager
from src.ui.lazy_service import LazyService
from src.ui.main_window import MainWindow
from src.ui.translation_table_adapter import TranslationTableAdapter


class LazyServiceTests(unittest.TestCase):
    def test_constructs_once_on_first_use(self):
        constructed = []

        class Service:
            def __init__(self, value):
                constructed.append(value)
                self.value = value

            def read(self):
                return self.value

            def close(self):
                return "closed"

        with patch(
            "src.ui.lazy_service.import_module",
            lambda _name: SimpleNamespace(Service=Service),
        ):
            service = LazyService("fake_module", "Service", 42)

            self.assertFalse(service.is_initialized)
            self.assertIsNone(service.call_if_initialized("close"))
            self.assertEqual(constructed, [])

            with ThreadPoolExecutor(max_workers=8) as executor:
                values = list(executor.map(lambda _index: service.read(), range(16)))

            self.assertEqual(values, [42] * 16)
            self.assertTrue(service.is_initialized)
            self.assertEqual(constructed, [42])
            self.assertEqual(service.call_if_initialized("close"), "closed")


class ConfigStartupTests(unittest.TestCase):
    def test_credential_lookup_is_deferred_and_cached(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("src.config.config_manager.get_key", return_value="sk-test") as get_key_mock,
        ):
            paths = SimpleNamespace(config_dir=Path(temp_dir))
            manager = ConfigManager(app_paths=paths)

            self.assertEqual(get_key_mock.call_count, 0)
            self.assertEqual(manager.get_api_config(load_secret=False)["api_key"], "")
            self.assertTrue(manager.is_api_configured())
            self.assertEqual(manager.get_api_config()["api_key"], "sk-test")
            self.assertEqual(get_key_mock.call_count, 1)


class MainWindowLayoutTests(unittest.TestCase):
    def test_footer_is_reserved_before_expandable_work_area(self):
        class PackedFrame:
            def __init__(self):
                self.pack_options = None

            def pack(self, **kwargs):
                self.pack_options = kwargs

        main_frame = PackedFrame()
        footer_frame = PackedFrame()
        window = MainWindow.__new__(MainWindow)
        window.root = object()
        calls = []
        window.create_menu = lambda: calls.append(("menu", None))
        window.create_toolbar = lambda parent: calls.append(("toolbar", parent))
        window.create_control_panel = lambda parent: calls.append(("controls", parent))
        window.create_status_bar = lambda parent: calls.append(("status", parent))
        window.create_onboarding_host = lambda parent: calls.append(("onboarding", parent))
        window.create_work_area = lambda parent: calls.append(("work", parent))

        with patch(
            "src.ui.main_window.ttk.Frame",
            side_effect=(main_frame, footer_frame),
        ):
            window.setup_ui()

        self.assertEqual(footer_frame.pack_options, {"side": tk.BOTTOM, "fill": tk.X})
        self.assertEqual(
            [name for name, _parent in calls],
            ["menu", "toolbar", "controls", "status", "onboarding", "work"],
        )
        self.assertIs(calls[2][1], footer_frame)
        self.assertIs(calls[3][1], footer_frame)


class _Root:
    def __init__(self):
        self.callbacks = []
        self.cancelled = []

    def after(self, _delay, callback):
        callback_id = f"after-{len(self.callbacks)}"
        self.callbacks.append((callback_id, callback))
        return callback_id

    def after_cancel(self, callback_id):
        self.cancelled.append(callback_id)

    def run_pending(self):
        while self.callbacks:
            _callback_id, callback = self.callbacks.pop(0)
            callback()


class _Widget:
    def __init__(self):
        self.options = {}
        self.hidden = False

    def config(self, **kwargs):
        self.options.update(kwargs)

    def pack_forget(self):
        self.hidden = True


class _Tree:
    def __init__(self):
        self.items = []
        self.tags = {}

    def get_children(self):
        return tuple(self.items)

    def delete(self, *item_ids):
        removed = set(item_ids)
        self.items = [item for item in self.items if item not in removed]

    def insert(self, _parent, _position, **_kwargs):
        item_id = f"item-{len(self.items)}"
        self.items.append(item_id)
        return item_id

    def tag_configure(self, tag, **kwargs):
        self.tags[tag] = kwargs


class ChunkedTableLoadTests(unittest.TestCase):
    def test_large_load_does_not_mutate_inputs(self):
        window = MainWindow.__new__(MainWindow)
        window.root = _Root()
        window.translation_table = _Tree()
        window.translate_btn = _Widget()
        window.continue_btn = _Widget()
        window.project_label = _Widget()
        window.empty_state = _Widget()
        window.file_importer = SimpleNamespace(current_source_path=None)
        window._table_load_after_id = None
        window._table_load_generation = 0
        window._table_loading = False
        window._undo_stack = []
        window._redo_stack = []
        window._manually_edited_items = set()
        window._hidden_items = set()
        window._all_items = []
        # PERF §7：文档模型和表格适配器（load_data_to_table 依赖）
        window._document = TranslationDocument()
        window._table_adapter = TranslationTableAdapter(window.translation_table)
        statuses = []
        window.update_status = statuses.append
        window._set_save_status = lambda _status: None
        window.refresh_action_state = lambda: None

        source_lines = [f"line {index}" for index in range(600)] + [""]
        target_lines = [""] * 602
        source_snapshot = list(source_lines)
        target_snapshot = list(target_lines)

        window.load_data_to_table(source_lines, target_lines)

        self.assertEqual(len(window._all_items), 250)
        self.assertEqual(len(window.root.callbacks), 1)
        self.assertTrue(window._table_loading)
        self.assertEqual(source_lines, source_snapshot)
        self.assertEqual(target_lines, target_snapshot)

        window.root.run_pending()

        self.assertEqual(len(window._all_items), 600)
        self.assertFalse(window._table_loading)
        self.assertTrue(window.empty_state.hidden)
        self.assertEqual(window.continue_btn.options["state"], "normal")
        self.assertTrue(any("250/600" in status for status in statuses))


if __name__ == "__main__":
    unittest.main()
