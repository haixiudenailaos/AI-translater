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
from src.ui.translation_controller import TranslationController
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
        """P1-4：密钥读取延迟到首次需要时，且结果被缓存。

        旧测试 patch 全局 ``get_key``，P1-4 移除全局调用后改为注入
        ``CountingSecretStore`` 验证 retrieve 调用次数。
        """
        from src.domain.secret import StorageStatus

        class CountingSecretStore:
            def __init__(self):
                self.retrieve_count = 0

            def store(self, identifier, key):
                return StorageStatus.PERSISTED

            def retrieve(self, identifier):
                self.retrieve_count += 1
                return "sk-test"

            def delete(self, identifier):
                return True

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = SimpleNamespace(config_dir=Path(temp_dir))
            store = CountingSecretStore()
            manager = ConfigManager(app_paths=paths, secret_store=store)

            self.assertEqual(store.retrieve_count, 0)
            self.assertEqual(manager.get_api_config(load_secret=False)["api_key"], "")
            self.assertTrue(manager.is_api_configured())
            self.assertEqual(manager.get_api_config()["api_key"], "sk-test")
            self.assertEqual(store.retrieve_count, 1)


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

    def test_onboarding_host_is_removed_when_panel_hides(self):
        class Host:
            def __init__(self):
                self.manager = ""
                self.pack_options = None
                self.hidden = False

            def winfo_manager(self):
                return self.manager

            def pack(self, **kwargs):
                self.manager = "pack"
                self.pack_options = kwargs
                self.hidden = False

            def pack_forget(self):
                self.manager = ""
                self.hidden = True

        window = MainWindow.__new__(MainWindow)
        window._onboarding_host = Host()
        window._work_frame = object()

        window._set_onboarding_host_visible(True)
        self.assertEqual(window._onboarding_host.pack_options["before"], window._work_frame)

        window._set_onboarding_host_visible(False)
        self.assertTrue(window._onboarding_host.hidden)

    def test_toolbar_keeps_project_name_in_flexible_middle_column(self):
        class LayoutWidget:
            def __init__(self, parent=None, **options):
                self.parent = parent
                self.options = options
                self.pack_options = None
                self.grid_options = None
                self.column_options = {}

            def pack(self, **kwargs):
                self.pack_options = kwargs

            def grid(self, **kwargs):
                self.grid_options = kwargs

            def grid_columnconfigure(self, column, **kwargs):
                self.column_options[column] = kwargs

        frames = []
        labels = []
        buttons = []

        def make_frame(parent=None, **options):
            widget = LayoutWidget(parent, **options)
            frames.append(widget)
            return widget

        def make_label(parent=None, **options):
            widget = LayoutWidget(parent, **options)
            labels.append(widget)
            return widget

        def make_button(parent=None, **options):
            widget = LayoutWidget(parent, **options)
            buttons.append(widget)
            return widget

        window = MainWindow.__new__(MainWindow)
        window.file_importer = SimpleNamespace(
            import_file=lambda: None, import_clipboard=lambda: None
        )
        window.open_concurrent = lambda: None
        window.open_settings = lambda: None

        with (
            patch("src.ui.main_window.ttk.Frame", side_effect=make_frame),
            patch("src.ui.main_window.ttk.Label", side_effect=make_label),
            patch("src.ui.main_window.ttk.Button", side_effect=make_button),
            patch("src.ui.main_window.tk.Button", side_effect=make_button),
        ):
            window.create_toolbar(object())

        toolbar, _left, right = frames
        self.assertEqual(toolbar.column_options[1], {"weight": 1})
        self.assertEqual(window.project_label.options["width"], 1)
        self.assertEqual(window.project_label.grid_options["column"], 1)
        self.assertEqual(right.grid_options["column"], 2)
        self.assertEqual(window.settings_btn.pack_options["side"], tk.RIGHT)

    def test_control_panel_places_start_translation_at_bottom_left(self):
        class LayoutWidget:
            def __init__(self, parent=None, **options):
                self.parent = parent
                self.options = options
                self.pack_options = None

            def pack(self, **kwargs):
                self.pack_options = kwargs

        class Menu:
            def __init__(self, *_args, **_kwargs):
                self.entries = []

            def add_command(self, **options):
                self.entries.append(options)

            def add_separator(self):
                self.entries.append(None)

            def index(self, _index):
                return len(self.entries) - 1

        buttons = []
        calls = []

        def make_widget(parent=None, **options):
            return LayoutWidget(parent, **options)

        def make_button(parent=None, **options):
            widget = LayoutWidget(parent, **options)
            buttons.append(widget)
            return widget

        window = MainWindow.__new__(MainWindow)
        window._run_primary_action = lambda: calls.append("start")
        window._continue_translation = lambda: None

        with (
            patch("src.ui.main_window.ttk.Frame", side_effect=make_widget),
            patch("src.ui.main_window.ttk.Button", side_effect=make_button),
            patch("src.ui.main_window.ttk.Menubutton", side_effect=make_widget),
            patch("src.ui.main_window.ttk.Label", side_effect=make_widget),
            patch("src.ui.main_window.ttk.Progressbar", side_effect=make_widget),
            patch("src.ui.main_window.tk.Menu", Menu),
            patch("src.ui.main_window.tk.DoubleVar", return_value=object()),
        ):
            window.create_control_panel(object())

        self.assertIs(buttons[0], window.start_translation_btn)
        self.assertEqual(buttons[0].options["text"], "开始翻译")
        self.assertEqual(buttons[0].options["state"], tk.DISABLED)
        self.assertEqual(buttons[0].pack_options["side"], tk.LEFT)
        self.assertEqual(buttons[1].options["text"], "翻译未完成行")
        buttons[0].options["command"]()
        self.assertEqual(calls, ["start"])

    def test_shortcuts_bind_control_and_command_variants(self):
        class Root:
            def __init__(self):
                self.bindings = {}

            def bind(self, sequence, callback):
                self.bindings[sequence] = callback

        window = MainWindow.__new__(MainWindow)
        window.root = Root()
        window.file_importer = SimpleNamespace(import_file=lambda: None)
        window.translation_controller = SimpleNamespace(save_translation=lambda: None)
        window.focus_search = lambda: None
        window.undo = lambda: None
        window.redo = lambda: None
        window._run_primary_action = lambda: None
        window._continue_translation = lambda: None

        window.setup_bindings()

        for key in ("o", "s", "f", "z", "y"):
            self.assertIn(f"<Control-{key}>", window.root.bindings)
            self.assertIn(f"<Command-{key}>", window.root.bindings)
        self.assertIn("<Command-Shift-Z>", window.root.bindings)


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


class TranslationControlTests(unittest.TestCase):
    def test_imported_pending_content_enables_start_translation(self):
        window = MainWindow.__new__(MainWindow)
        window._table_loading = False
        window._document = TranslationDocument()
        window._document.replace(["source"], [""])
        window._api_configured = True
        window._review_filter_var = SimpleNamespace(get=lambda: "全部")
        window.file_importer = SimpleNamespace(current_mapping_dir=None)
        window.translate_btn = _Widget()
        window.start_translation_btn = _Widget()

        window.refresh_action_state()

        self.assertEqual(window.start_translation_btn.options["state"], tk.NORMAL)
        self.assertEqual(window.translate_btn.options["text"], "翻译未完成行")

    def test_translation_controller_keeps_start_button_in_sync(self):
        controller = TranslationController.__new__(TranslationController)
        controller.start_btn = _Widget()
        controller.translate_btn = _Widget()
        controller.continue_btn = _Widget()
        controller.stop_btn = _Widget()

        controller._set_control_states(tk.DISABLED, tk.DISABLED, tk.NORMAL)

        self.assertEqual(controller.start_btn.options["state"], tk.DISABLED)
        self.assertEqual(controller.translate_btn.options["state"], tk.DISABLED)
        self.assertEqual(controller.continue_btn.options["state"], tk.DISABLED)
        self.assertEqual(controller.stop_btn.options["state"], tk.NORMAL)


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
        # P2-4：行值缓存（_load_table_chunk 写入，apply_review_filter 读取）
        window._row_values_cache = {}
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
