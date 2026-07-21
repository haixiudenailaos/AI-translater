#!/usr/bin/env python3
"""
新手指导状态机单元测试

优先测试纯状态转换，不创建真实 Tk 窗口。使用假的 panel、config manager、
target 和 action 验证开始、前进、后退、稍后、跳过、完成以及业务事件联动。
"""

import copy
import unittest

from src.ui.onboarding import STEPS, OnboardingController, OnboardingPanel

# ── 测试替身 ────────────────────────────────────────


class _FakeRoot:
    """模拟 Tk root，记录 after_idle 调度的回调。"""

    def __init__(self):
        self.idle_callbacks = []

    def after_idle(self, callback):
        self.idle_callbacks.append(callback)

    def flush_idle(self):
        while self.idle_callbacks:
            callback = self.idle_callbacks.pop(0)
            callback()


class _FakePanel:
    """记录面板调用，不创建真实控件。"""

    def __init__(self):
        self.shown = False
        self.hidden_count = 0
        self.closed = False
        self.steps_rendered = []
        self.last_render = None

    def show_step(self, step, **kwargs):
        self.last_render = {"step": step, **kwargs}
        self.steps_rendered.append(self.last_render)

    def show(self):
        self.shown = True

    def hide(self):
        self.hidden_count += 1
        self.shown = False

    def close(self):
        self.closed = True


class _FakePanelFrame:
    def __init__(self):
        self.pack_calls = []
        self.hidden = False
        self.destroyed = False

    def pack(self, **kwargs):
        self.pack_calls.append(kwargs)

    def pack_forget(self):
        self.hidden = True

    def destroy(self):
        self.destroyed = True


class _FakeConfigManager:
    """模拟配置管理器，可控制保存成功/失败。"""

    def __init__(self, onboarding=None, *, save_fails=False):
        self._onboarding = onboarding or {}
        self.save_fails = save_fails
        self.saved_configs = []

    def get_app_config(self):
        return {"onboarding": copy.deepcopy(self._onboarding)}

    def save_app_config(self, config):
        if self.save_fails:
            return False
        self._onboarding = copy.deepcopy(config.get("onboarding", {}))
        self.saved_configs.append(copy.deepcopy(config))
        return True


def _make_controller(
    *,
    onboarding=None,
    save_fails=False,
    targets=None,
    actions=None,
):
    root = _FakeRoot()
    panel = _FakePanel()
    config_manager = _FakeConfigManager(onboarding, save_fails=save_fails)
    statuses = []
    controller = OnboardingController(
        root=root,
        panel=panel,
        config_manager=config_manager,
        targets=targets or {},
        actions=actions or {},
        status_updater=statuses.append,
    )
    return controller, panel, config_manager, root, statuses


# ── 配置归一化 ──────────────────────────────────────


class ConfigNormalizationTests(unittest.TestCase):
    def test_default_config_generates_valid_not_started_state(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller()
        self.assertEqual(controller._state["status"], "not_started")
        self.assertEqual(controller._state["current_step"], "welcome")
        self.assertEqual(controller._state["completed_steps"], [])
        self.assertTrue(controller._state["auto_show"])

    def test_missing_subfields_merge_with_defaults(self):
        # 仅提供 status，其余字段缺失
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress"}
        )
        self.assertEqual(controller._state["status"], "in_progress")
        self.assertEqual(controller._state["current_step"], "welcome")
        self.assertEqual(controller._state["completed_steps"], [])
        self.assertTrue(controller._state["auto_show"])
        self.assertEqual(controller._state["schema_version"], 1)

    def test_invalid_status_falls_back_to_not_started(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "bogus", "current_step": "api"}
        )
        self.assertEqual(controller._state["status"], "not_started")

    def test_unknown_current_step_falls_back_to_first_incomplete(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={
                "status": "in_progress",
                "current_step": "does_not_exist",
                "completed_steps": ["welcome", "api"],
            }
        )
        # welcome 与 api 已完成，第一个未完成步骤为 import
        self.assertEqual(controller._state["current_step"], "import")

    def test_unknown_current_step_falls_back_to_welcome_when_all_complete(self):
        all_ids = [s.step_id for s in STEPS]
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={
                "status": "in_progress",
                "current_step": "ghost",
                "completed_steps": all_ids,
            }
        )
        self.assertEqual(controller._state["current_step"], "welcome")

    def test_completed_steps_deduplicated(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={
                "status": "in_progress",
                "current_step": "import",
                "completed_steps": ["welcome", "welcome", "api"],
            }
        )
        self.assertEqual(controller._state["completed_steps"], ["welcome", "api"])


class OnboardingPanelVisibilityTests(unittest.TestCase):
    def test_show_and_hide_notify_host_visibility(self):
        visibility = []
        panel = OnboardingPanel.__new__(OnboardingPanel)
        panel._closed = False
        panel._visible = False
        panel._frame = _FakePanelFrame()
        panel._on_visibility_changed = visibility.append
        panel._bind_shortcuts = lambda: None
        panel._unbind_shortcuts = lambda: None

        panel.show()
        panel.hide()

        self.assertEqual(visibility, [True, False])
        self.assertTrue(panel._frame.hidden)


# ── 自动展示规则 ────────────────────────────────────


class AutoStartTests(unittest.TestCase):
    def test_auto_starts_when_api_not_configured_and_no_recent_files(self):
        controller, panel, _cfg, _root, _statuses = _make_controller()
        controller.maybe_start(api_configured=False, has_recent_files=False)
        self.assertTrue(panel.shown)
        self.assertTrue(controller._active)

    def test_does_not_auto_start_when_api_configured(self):
        controller, panel, _cfg, _root, _statuses = _make_controller()
        controller.maybe_start(api_configured=True, has_recent_files=False)
        self.assertFalse(panel.shown)
        self.assertFalse(controller._active)

    def test_does_not_auto_start_when_recent_files_present(self):
        controller, panel, _cfg, _root, _statuses = _make_controller()
        controller.maybe_start(api_configured=False, has_recent_files=True)
        self.assertFalse(panel.shown)

    def test_does_not_auto_start_when_completed(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "completed", "current_step": "review_export"}
        )
        controller.maybe_start(api_configured=False, has_recent_files=False)
        self.assertFalse(panel.shown)

    def test_does_not_auto_start_when_dismissed(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "dismissed", "current_step": "api"}
        )
        controller.maybe_start(api_configured=False, has_recent_files=False)
        self.assertFalse(panel.shown)

    def test_resumes_when_in_progress(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "translate"}
        )
        controller.maybe_start(api_configured=True, has_recent_files=True)
        self.assertTrue(panel.shown)
        # 应从 translate 恢复，而非回到 welcome
        self.assertEqual(controller._state["current_step"], "translate")

    def test_postpone_blocks_auto_show_in_same_session(self):
        controller, panel, _cfg, _root, _statuses = _make_controller()
        controller.start()
        controller.postpone()
        # 同一会话再次 maybe_start 不应重新弹出
        controller.maybe_start(api_configured=False, has_recent_files=False)
        self.assertFalse(panel.shown)

    def test_auto_show_false_blocks_auto_start(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "not_started", "auto_show": False}
        )
        controller.maybe_start(api_configured=False, has_recent_files=False)
        self.assertFalse(panel.shown)


# ── 流程控制 ────────────────────────────────────────


class FlowControlTests(unittest.TestCase):
    def test_force_starts_from_help_menu_for_completed_user(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "completed", "current_step": "review_export"}
        )
        controller.start(force=True)
        self.assertTrue(panel.shown)
        self.assertEqual(controller._state["status"], "in_progress")
        self.assertEqual(controller._state["current_step"], "welcome")
        self.assertEqual(controller._state["completed_steps"], [])

    def test_force_starts_for_dismissed_user(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "dismissed", "current_step": "api"}
        )
        controller.start(force=True)
        self.assertEqual(controller._state["current_step"], "welcome")
        self.assertEqual(controller._state["status"], "in_progress")

    def test_force_resumes_for_in_progress_user(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "import"}
        )
        controller.start(force=True)
        # in_progress 时从 current_step 恢复
        self.assertEqual(controller._state["current_step"], "import")

    def test_next_advances_and_marks_completed(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller()
        controller.start()
        controller.next()  # welcome -> api
        self.assertEqual(controller._state["current_step"], "api")
        self.assertIn("welcome", controller._state["completed_steps"])

    def test_next_at_last_step_finishes(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={
                "status": "in_progress",
                "current_step": "review_export",
            }
        )
        controller.start()
        controller.next()
        self.assertEqual(controller._state["status"], "completed")
        all_ids = [s.step_id for s in STEPS]
        self.assertEqual(set(controller._state["completed_steps"]), set(all_ids))

    def test_back_does_not_overshoot_first_step(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller()
        controller.start()
        controller.back()  # already at welcome
        self.assertEqual(controller._state["current_step"], "welcome")

    def test_back_navigates_to_previous_step(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "import"}
        )
        controller.start()
        controller.back()
        self.assertEqual(controller._state["current_step"], "api")

    def test_postpone_keeps_position_and_in_progress(self):
        controller, panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "api"}
        )
        controller.start()
        controller.postpone()
        self.assertEqual(controller._state["status"], "in_progress")
        self.assertEqual(controller._state["current_step"], "api")
        self.assertFalse(controller._active)
        self.assertTrue(controller._postponed_this_session)
        self.assertEqual(panel.hidden_count, 1)

    def test_dismiss_writes_dismissed(self):
        controller, panel, cfg, _root, _statuses = _make_controller()
        controller.start()
        controller.dismiss()
        self.assertEqual(controller._state["status"], "dismissed")
        self.assertFalse(controller._active)
        # 已持久化
        self.assertEqual(cfg._onboarding["status"], "dismissed")

    def test_finish_writes_completed(self):
        controller, panel, cfg, _root, _statuses = _make_controller()
        controller.start()
        controller.finish()
        self.assertEqual(controller._state["status"], "completed")
        self.assertEqual(cfg._onboarding["status"], "completed")
        all_ids = [s.step_id for s in STEPS]
        self.assertEqual(set(cfg._onboarding["completed_steps"]), set(all_ids))

    def test_save_failure_does_not_block(self):
        controller, panel, _cfg, _root, statuses = _make_controller(save_fails=True)
        controller.start()
        # 即使保存失败也不抛异常，并在状态栏提示
        self.assertIn("新手指导进度保存失败", statuses)
        self.assertTrue(controller._active)

    def test_close_is_idempotent(self):
        controller, panel, _cfg, _root, _statuses = _make_controller()
        controller.start()
        controller.close()
        controller.close()  # 重复调用不应抛异常
        self.assertTrue(panel.closed)


# ── 业务事件联动 ────────────────────────────────────


class EventNotificationTests(unittest.TestCase):
    def test_api_configured_completes_api_and_advances(self):
        controller, _panel, _cfg, root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "api"}
        )
        controller.start()
        controller.notify("api_status_changed", configured=True)
        root.flush_idle()
        self.assertIn("api", controller._state["completed_steps"])
        self.assertEqual(controller._state["current_step"], "import")

    def test_api_not_configured_does_not_complete(self):
        controller, _panel, _cfg, root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "api"}
        )
        controller.start()
        controller.notify("api_status_changed", configured=False)
        root.flush_idle()
        self.assertNotIn("api", controller._state["completed_steps"])
        self.assertEqual(controller._state["current_step"], "api")

    def test_content_loaded_completes_import_and_advances(self):
        controller, _panel, _cfg, root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "import"}
        )
        controller.start()
        controller.notify("content_loaded", count=42)
        root.flush_idle()
        self.assertIn("import", controller._state["completed_steps"])
        self.assertEqual(controller._state["current_step"], "translate")

    def test_empty_content_does_not_complete_import(self):
        controller, _panel, _cfg, root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "import"}
        )
        controller.start()
        controller.notify("content_loaded", count=0)
        root.flush_idle()
        self.assertNotIn("import", controller._state["completed_steps"])
        self.assertEqual(controller._state["current_step"], "import")

    def test_translation_started_completes_translate_without_jumping_to_end(self):
        controller, _panel, _cfg, root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "translate"}
        )
        controller.start()
        controller.notify("translation_started")
        root.flush_idle()
        self.assertIn("translate", controller._state["completed_steps"])
        # 仍停留在 translate，不自动跳到结束
        self.assertEqual(controller._state["current_step"], "translate")
        self.assertNotEqual(controller._state["status"], "completed")

    def test_quality_check_run_does_not_complete_guide(self):
        controller, _panel, _cfg, root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "review_export"}
        )
        controller.start()
        controller.notify("quality_check_run", issue_count=3)
        root.flush_idle()
        self.assertIn("review_export", controller._state["completed_steps"])
        self.assertNotEqual(controller._state["status"], "completed")

    def test_event_updates_completed_steps_when_panel_hidden(self):
        controller, panel, cfg, root, _statuses = _make_controller()
        # 引导未显示时收到 API 配置成功事件
        controller.notify("api_status_changed", configured=True)
        root.flush_idle()
        self.assertIn("api", controller._state["completed_steps"])
        # 不自动弹出面板
        self.assertFalse(panel.shown)
        # 进度已持久化，手动打开时可见
        self.assertIn("api", cfg._onboarding["completed_steps"])

    def test_auto_advance_only_when_on_matching_step(self):
        controller, _panel, _cfg, root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "welcome"}
        )
        controller.start()
        # 当前在 welcome，收到 api 事件应标记完成但不从 welcome 跳到 import
        controller.notify("api_status_changed", configured=True)
        root.flush_idle()
        self.assertIn("api", controller._state["completed_steps"])
        self.assertEqual(controller._state["current_step"], "welcome")


# ── 目标定位 ────────────────────────────────────────


class _FakeWidget:
    def __init__(self, exists=True):
        self._exists = exists
        self.focused = False

    def winfo_exists(self):
        return self._exists

    def focus_set(self):
        self.focused = True


class TargetLocationTests(unittest.TestCase):
    def test_locate_focuses_target_without_triggering_command(self):
        widget = _FakeWidget()
        translate_calls = []
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "translate"},
            targets={"translate": lambda: widget},
            actions={"start_translation": lambda: translate_calls.append(1)},
        )
        controller.start()
        primary = controller._primary_command_for(controller._current_step_obj())
        self.assertIsNotNone(primary)
        primary()
        self.assertTrue(widget.focused)
        # 不应触发翻译命令
        self.assertEqual(translate_calls, [])

    def test_locate_handles_destroyed_widget(self):
        widget = _FakeWidget(exists=False)
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "translate"},
            targets={"translate": lambda: widget},
        )
        controller.start()
        primary = controller._primary_command_for(controller._current_step_obj())
        # 控件已销毁，定位应被忽略而不抛异常
        primary()
        self.assertFalse(widget.focused)

    def test_locate_handles_missing_target(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "translate"},
            targets={},
        )
        controller.start()
        primary = controller._primary_command_for(controller._current_step_obj())
        primary()  # 未注册目标，应安全忽略


# ── 主操作动作 ──────────────────────────────────────


class PrimaryActionTests(unittest.TestCase):
    def test_api_primary_invokes_open_settings(self):
        calls = []
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "api"},
            actions={"open_settings": lambda: calls.append("opened")},
        )
        controller.start()
        primary = controller._primary_command_for(controller._current_step_obj())
        primary()
        self.assertEqual(calls, ["opened"])

    def test_import_primary_invokes_import_file(self):
        calls = []
        controller, _panel, _cfg, _root, _statuses = _make_controller(
            onboarding={"status": "in_progress", "current_step": "import"},
            actions={"import_file": lambda: calls.append("imported")},
        )
        controller.start()
        primary = controller._primary_command_for(controller._current_step_obj())
        primary()
        self.assertEqual(calls, ["imported"])

    def test_welcome_primary_advances(self):
        controller, _panel, _cfg, _root, _statuses = _make_controller()
        controller.start()
        primary = controller._primary_command_for(controller._current_step_obj())
        primary()
        self.assertEqual(controller._state["current_step"], "api")


if __name__ == "__main__":
    unittest.main()
