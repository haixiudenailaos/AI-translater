from types import SimpleNamespace

import main
from src.config.config_manager import ConfigManager
from src.ui.settings_window import SettingsWindow
from src.ui.window_geometry import WindowGeometryTracker


class _ConfigManager:
    def __init__(self, state=None):
        self.state = state or {}
        self.updates = []

    def get_window_state(self, window_name):
        return self.state.get(window_name, {})

    def update_window_state(self, window_name, state, *, persist=False):
        self.updates.append((window_name, state, persist))
        return True


class _Window:
    def __init__(self, screen_width=1920, screen_height=1080):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.geometry_value = None
        self.minsize_value = None
        self.state_value = "normal"
        self.bindings = {}
        self.idle_callbacks = []

    def update_idletasks(self):
        pass

    def winfo_screenwidth(self):
        return self.screen_width

    def winfo_screenheight(self):
        return self.screen_height

    def minsize(self, width, height):
        self.minsize_value = (width, height)

    def geometry(self, value):
        self.geometry_value = value

    def after_idle(self, callback):
        self.idle_callbacks.append(callback)

    def bind(self, sequence, callback, add=None):
        self.bindings[sequence] = (callback, add)

    def state(self, value=None):
        if value is not None:
            self.state_value = value
        return self.state_value


def test_restores_saved_settings_size_and_centers_it():
    window = _Window()
    config = _ConfigManager({"settings": {"width": 1200, "height": 800, "maximized": False}})
    tracker = WindowGeometryTracker(
        window,
        config,
        "settings",
        default_size=(600, 560),
        minimum_size=(600, 560),
        center=True,
    )

    tracker.restore()

    assert window.geometry_value == "1200x800+360+140"
    assert window.minsize_value == (600, 560)


def test_restore_clamps_saved_size_to_current_screen():
    window = _Window(1366, 768)
    config = _ConfigManager({"main": {"width": 3000, "height": 2000}})
    tracker = WindowGeometryTracker(
        window,
        config,
        "main",
        default_size=(1000, 700),
        minimum_size=(800, 600),
    )

    tracker.restore()

    assert window.geometry_value == "1302x704"
    assert window.minsize_value == (800, 600)


def test_invalid_saved_size_falls_back_to_default():
    window = _Window()
    config = _ConfigManager({"main": {"width": "wide", "height": -1}})
    tracker = WindowGeometryTracker(
        window,
        config,
        "main",
        default_size=(1000, 700),
        minimum_size=(800, 600),
    )

    tracker.restore()

    assert window.geometry_value == "1000x700"


def test_maximized_window_saves_last_normal_size():
    window = _Window()
    config = _ConfigManager({"main": {"width": 1100, "height": 760, "maximized": True}})
    tracker = WindowGeometryTracker(
        window,
        config,
        "main",
        default_size=(1000, 700),
        minimum_size=(800, 600),
    )
    tracker.restore()
    tracker.bind()
    window.idle_callbacks.pop()()

    callback, add = window.bindings["<Configure>"]
    callback(SimpleNamespace(widget=window, width=1920, height=1080))
    tracker.save(persist=True)

    assert add == "+"
    assert window.state_value == "zoomed"
    assert config.updates == [
        (
            "main",
            {"width": 1100, "height": 760, "maximized": True},
            True,
        )
    ]


def test_normal_resize_updates_saved_size():
    window = _Window()
    config = _ConfigManager()
    tracker = WindowGeometryTracker(
        window,
        config,
        "settings",
        default_size=(600, 560),
        minimum_size=(600, 560),
    )
    tracker.restore()
    tracker.bind()

    callback, _add = window.bindings["<Configure>"]
    callback(SimpleNamespace(widget=window, width=980, height=720))
    tracker.save()

    assert config.updates == [
        (
            "settings",
            {"width": 980, "height": 720, "maximized": False},
            False,
        )
    ]


def test_window_state_persists_across_config_manager_instances(tmp_app_paths, tmp_config_manager):
    assert tmp_config_manager.update_window_state(
        "main",
        {"width": 1280, "height": 820, "maximized": False},
        persist=True,
    )

    reloaded = ConfigManager(
        app_paths=tmp_app_paths,
        secret_store=tmp_config_manager._secret_store,
    )

    assert reloaded.get_window_state("main") == {
        "width": 1280,
        "height": 820,
        "maximized": False,
    }


def test_corrupt_persisted_window_state_is_ignored(tmp_config_manager):
    tmp_config_manager.app_config["window_state"] = {
        "settings": {"width": True, "height": 700, "maximized": True}
    }

    assert tmp_config_manager.get_window_state("settings") == {}


def test_application_close_records_main_window_before_saving_config():
    actions = []

    class _MainWindow:
        def confirm_save_before_close(self):
            return "proceed"

        def close(self, decision):
            actions.append(("close", decision))

    class _Root:
        def destroy(self):
            actions.append("destroy")

    app = main.TranslatorApp.__new__(main.TranslatorApp)
    app.root = _Root()
    app.main_window = _MainWindow()
    app.app_context = object()
    app._main_window_geometry_tracker = SimpleNamespace(
        save=lambda: actions.append("geometry") or True
    )
    app._save_config_with_retry = lambda: actions.append("config") or True

    app.on_closing()

    assert actions == ["geometry", "config", ("close", "proceed"), "destroy"]


def test_settings_destroy_persists_window_size_and_closes_ui_pump():
    actions = []
    window = object()
    dialog = SettingsWindow.__new__(SettingsWindow)
    dialog.window = window
    dialog._window_geometry_tracker = SimpleNamespace(
        save=lambda *, persist=False: actions.append(("geometry", persist)) or True
    )
    dialog.close_ui_pump = lambda: actions.append("pump")

    dialog._on_window_destroy(SimpleNamespace(widget=window))

    assert actions == [("geometry", True), "pump"]
