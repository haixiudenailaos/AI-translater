#!/usr/bin/env python3
"""LC-03 / LC-10 / LC-14：主界面 controller 的超长上下文接线。

这些用例验证**接线**而不是策略对象：主界面“超长上下文翻译”入口必须真的把
LONG 策略送到引擎的 ``translate_long_context_mode``，普通入口仍走
``translate_fast_mode``；重复点击不启动重叠运行；小模型模式在入口即拒绝。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from src.domain.translation_policy import ContextMode, TranslationContextPolicy
from src.ui.translation_controller import TranslationController

LONG_PRESET = 131072


class _Widget:
    """记录 config() 调用的最小控件替身。"""

    def __init__(self):
        self.states: list[dict] = []

    def config(self, **kwargs):
        self.states.append(kwargs)

    def get(self):
        return ""


class FakeTranslator:
    """记录被调用的入口与策略。"""

    def __init__(self):
        self.fast_calls: list[str] = []
        self.long_calls: list[tuple[str, object]] = []

    def translate_fast_mode(self, content, progress, complete):
        self.fast_calls.append(content)

    def translate_long_context_mode(self, content, progress, complete, *, wallet=None):
        self.long_calls.append((content, wallet))

    def stop(self):
        pass


class FakeConfigManager:
    def __init__(self, *, small_model_mode: bool = False, tokens: int = LONG_PRESET):
        self._app = {
            "small_model_mode": small_model_mode,
            "long_context": {"schema_version": 1, "context_window_tokens": tokens},
        }

    def get_app_config(self):
        return dict(self._app)

    def is_api_configured(self):
        return True

    def get_long_context_window_tokens(self):
        return self._app["long_context"]["context_window_tokens"]

    def get_api_config(self, *, load_secret: bool = True):
        return {"model_name": "m", "context_window_tokens": 131072}


def make_controller(*, small_model_mode=False, source_lines=None):
    """构造一个只装配必要依赖的 controller（不起线程、不用 Tk）。"""
    controller = TranslationController.__new__(TranslationController)
    controller.root = SimpleNamespace(after=lambda *_a: None, after_cancel=lambda *_a: None)
    controller.config_manager = FakeConfigManager(small_model_mode=small_model_mode)
    controller.translator = FakeTranslator()
    controller.get_table_data = lambda: (
        list(source_lines if source_lines is not None else ["a", "b"]),
        ["", ""],
    )
    controller.status_updater = lambda _msg: None
    controller.is_translating = False
    controller._current_run_id = None
    controller._current_mode = None
    controller._pending_results = {}
    controller._pending_errors = {}
    controller._retired_run_ids = __import__("collections").deque(maxlen=64)
    controller._run_context_policies = {}
    controller._results_lock = threading.Lock()
    controller._streaming_preview_rows = set()
    controller._continue_start_line = 0
    controller._continue_missing_indices = []
    controller._export_job = None
    controller._export_poll_after_id = None
    controller._liveness_after_id = None
    controller._last_stream_activity_at = None
    controller._liveness_base_message = ""
    controller._document = None
    controller._table_adapter = None
    controller._on_run_terminal = lambda _r, _m: None
    controller._on_retranslated = lambda _i: None
    controller._on_save_success = lambda: None
    controller._preflight_callback = None
    controller._mailbox = SimpleNamespace(publish=lambda _e: None, discard_run=lambda _r: None)
    controller._event_pump = SimpleNamespace(start=lambda: None, close=lambda: None)
    controller.get_source_path = lambda: None
    controller.get_mapping_dir = lambda: None
    controller.schedule_save = lambda *a, **k: None
    controller.open_settings = lambda: None
    controller._continue_translation = lambda: None
    # 控件替身
    controller.start_btn = _Widget()
    controller.translate_btn = _Widget()
    controller.retranslate_btn = _Widget()
    controller.long_context_btn = _Widget()
    controller.continue_btn = _Widget()
    controller.stop_btn = _Widget()
    controller.mode_toggle = None
    return controller


def _no_threads(monkeypatch):
    """把工作线程替换成同步执行，让测试直接观察 engine 调用。"""
    started: list[threading.Thread] = []

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self.daemon = daemon

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    return started


class TestLongContextEntryWiring:
    def test_long_entry_sends_long_policy_to_engine(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller()

        controller.start_long_context_translation()

        assert controller.translator.long_calls, "必须调用超长入口"
        content, policy = controller.translator.long_calls[0]
        assert content == "a\nb"
        assert policy is not None
        assert policy.mode is ContextMode.LONG
        assert policy.context_window_tokens == LONG_PRESET
        assert controller.translator.fast_calls == [], "超长模式不得走普通入口"

    def test_standard_entry_still_uses_fast_mode(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller()

        controller.start_translation()

        assert controller.translator.fast_calls == ["a\nb"]
        assert controller.translator.long_calls == []

    def test_only_unfinished_lines_are_sent(self, monkeypatch):
        """两个入口都只处理未完成行，保留已有译文与人工修改。"""
        _no_threads(monkeypatch)
        controller = TranslatorControllerStub = make_controller(source_lines=["a", "b", "c"])
        controller.get_table_data = lambda: (["a", "b", "c"], ["译a", "", "译c"])

        controller.start_long_context_translation()

        content, _policy = controller.translator.long_calls[0]
        assert content == "b"

    def test_duplicate_click_does_not_start_overlapping_run(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller()
        controller.translator.translate_long_context_mode = (
            lambda *a, **k: None  # 不回调终态，运行保持"进行中"
        )

        controller.start_long_context_translation()
        first_run_id = controller._current_run_id
        assert controller.is_translating is True

        messages: list[str] = []
        controller.status_updater = messages.append
        controller.start_long_context_translation()

        assert controller._current_run_id == first_run_id
        assert messages == ["翻译任务正在运行或停止中，请等待当前任务结束"]

    def test_active_run_blocks_long_entry(self):
        controller = make_controller()
        controller.is_translating = True
        messages: list[str] = []
        controller.status_updater = messages.append

        controller.start_long_context_translation()

        assert messages == ["翻译任务正在运行或停止中，请等待当前任务结束"]
        assert controller.translator.long_calls == []

    def test_controller_does_not_mutate_global_config(self, monkeypatch):
        """超长模式是一次运行的明确选项，不通过临时改配置再恢复实现。"""
        _no_threads(monkeypatch)
        controller = make_controller()
        before = controller.config_manager.get_app_config()

        controller.start_long_context_translation()

        assert controller.config_manager.get_app_config() == before


class TestSmallModelModeExclusion:
    def test_entry_refuses_when_small_model_mode_enabled(self, monkeypatch):
        """LC-14：小模型模式下入口状态与服务边界一致。"""
        _no_threads(monkeypatch)
        controller = make_controller(small_model_mode=True)
        shown: list[tuple] = []
        monkeypatch.setattr(
            "src.ui.translation_controller.messagebox.showwarning",
            lambda *a, **k: shown.append((a, k)),
        )

        controller.start_long_context_translation()

        assert controller.translator.long_calls == []
        assert controller.translator.fast_calls == []
        assert shown, "必须给出可见原因"
        assert "小模型" in str(shown[0][0])

    def test_button_state_follows_small_model_mode(self):
        controller = make_controller(small_model_mode=True)
        controller.long_context_btn = _Widget()

        controller._set_control_states("normal", "normal", "disabled")

        assert controller.long_context_btn.states[-1]["state"] == "disabled"

    def test_button_enabled_when_mode_is_off(self):
        controller = make_controller(small_model_mode=False)
        controller.long_context_btn = _Widget()

        controller._set_control_states("normal", "normal", "disabled")

        assert controller.long_context_btn.states[-1]["state"] == "normal"

    def test_button_disabled_while_running(self):
        controller = make_controller(small_model_mode=False)
        controller.long_context_btn = _Widget()

        controller._set_control_states("disabled", "disabled", "normal")

        assert controller.long_context_btn.states[-1]["state"] == "disabled"


class TestPolicySnapshotIsPerRun:
    def test_policy_is_frozen_at_enqueue_and_consumed_once(self, monkeypatch):
        """设置改动不影响在途运行；策略条目用完即移除。"""
        _no_threads(monkeypatch)
        controller = make_controller()

        controller.start_long_context_translation()

        # 工作线程取用后不应残留条目。
        assert controller._run_context_policies == {}

    def test_strategy_is_removed_when_run_is_retired(self):
        controller = make_controller()
        controller._run_context_policies["run1"] = TranslationContextPolicy(
            ContextMode.LONG, LONG_PRESET
        )

        controller._retire_run("run1")

        assert "run1" not in controller._run_context_policies

    def test_take_returns_none_for_unknown_run(self):
        controller = make_controller()
        assert controller._take_run_context_policy("nope") is None

    def test_take_returns_policy_once(self):
        controller = make_controller()
        policy = TranslationContextPolicy(ContextMode.LONG, LONG_PRESET)
        controller._run_context_policies["r"] = policy

        assert controller._take_run_context_policy("r") is policy
        assert controller._take_run_context_policy("r") is None


class TestEmptyAndNoPendingDocuments:
    def test_empty_document_warns_without_starting(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller(source_lines=[])
        shown: list[tuple] = []
        monkeypatch.setattr(
            "src.ui.translation_controller.messagebox.showwarning",
            lambda *a, **k: shown.append((a, k)),
        )

        controller.start_long_context_translation()

        assert controller.translator.long_calls == []
        assert shown

    def test_all_lines_complete_informs_without_starting(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller()
        controller.get_table_data = lambda: (["a"], ["译a"])
        shown: list[tuple] = []
        monkeypatch.setattr(
            "src.ui.translation_controller.messagebox.showinfo",
            lambda *a, **k: shown.append((a, k)),
        )
        monkeypatch.setattr(
            "src.ui.translation_controller.messagebox.showwarning",
            lambda *a, **k: shown.append((a, k)),
        )

        controller.start_long_context_translation()

        assert controller.translator.long_calls == []
        assert shown


class TestPreflightIsUsed:
    def test_long_entry_uses_long_context_action_name(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller()
        actions: list[str] = []

        def preflight(action):
            actions.append(action)
            return True

        controller._preflight_callback = preflight
        controller.start_long_context_translation()

        assert actions == ["long_context"]

    def test_preflight_rejection_does_not_start(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller()
        controller._preflight_callback = lambda _action: False

        controller.start_long_context_translation()

        assert controller.translator.long_calls == []
        assert controller.is_translating is False

    def test_standard_entry_keeps_full_action_name(self, monkeypatch):
        _no_threads(monkeypatch)
        controller = make_controller()
        actions: list[str] = []
        controller._preflight_callback = lambda action: (actions.append(action), True)[1]

        controller.start_translation()

        assert actions == ["full"]


@pytest.mark.parametrize("tokens", [32768, 131072, 1_048_576])
def test_policy_uses_configured_user_budget(monkeypatch, tokens):
    """每次运行读取当时的用户预算，不做应用侧裁剪。"""
    _no_threads(monkeypatch)
    controller = make_controller()
    controller.config_manager = FakeConfigManager(tokens=tokens)

    controller.start_long_context_translation()

    _content, policy = controller.translator.long_calls[0]
    assert policy.context_window_tokens == tokens
