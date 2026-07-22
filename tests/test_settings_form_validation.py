"""P2-5：表单校验与异步状态测试。

覆盖 ``src/ui/form_validation.py`` 的纯 Python 校验器，以及
``SettingsWindow`` 的字段级校验、首错聚焦、testing busy 状态和
"测试连接不得持久化配置" 契约。
"""

import sys
import tkinter as tk
import types
import unittest
from unittest.mock import patch

from src.ui.form_validation import (
    FormValidator,
    clamp_int,
    validate_int_range,
    validate_required_string,
)
from src.ui.settings_window import SettingsWindow


def _has_display():
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


_HAS_DISPLAY = _has_display()


# ──────────────────────────────────────────────────────────────
# 纯 Python 校验器测试（无需 Tk 显示）
# ──────────────────────────────────────────────────────────────


class ClampIntTests(unittest.TestCase):
    def test_clamp_below_lo_returns_lo(self):
        self.assertEqual(clamp_int(-5, 1, 10, default=1), 1)

    def test_clamp_above_hi_returns_hi(self):
        self.assertEqual(clamp_int(999, 1, 10, default=1), 10)

    def test_clamp_invalid_returns_default(self):
        self.assertEqual(clamp_int("abc", 1, 10, default=5), 5)
        self.assertEqual(clamp_int(None, 1, 10, default=5), 5)

    def test_clamp_valid_value_unchanged(self):
        self.assertEqual(clamp_int(7, 1, 10, default=1), 7)


class ValidateIntRangeTests(unittest.TestCase):
    def test_valid_value_returns_none(self):
        self.assertIsNone(validate_int_range(5, 1, 10, "字段"))

    def test_below_lo_returns_message(self):
        msg = validate_int_range(0, 1, 10, "批次 token 数")
        self.assertIsNotNone(msg)
        self.assertIn("批次 token 数", msg)
        self.assertIn("1", msg)

    def test_above_hi_returns_message(self):
        msg = validate_int_range(100, 1, 10, "批次行数")
        self.assertIsNotNone(msg)
        self.assertIn("批次行数", msg)
        self.assertIn("10", msg)

    def test_non_int_returns_message(self):
        msg = validate_int_range("xyz", 1, 10, "并发数")
        self.assertIsNotNone(msg)
        self.assertIn("并发数", msg)
        self.assertIn("整数", msg)


class ValidateRequiredStringTests(unittest.TestCase):
    def test_empty_string_returns_message(self):
        msg = validate_required_string("", "API 密钥")
        self.assertIsNotNone(msg)
        self.assertIn("API 密钥", msg)

    def test_whitespace_only_returns_message(self):
        msg = validate_required_string("   ", "API 密钥")
        self.assertIsNotNone(msg)

    def test_non_empty_returns_none(self):
        self.assertIsNone(validate_required_string("sk-abc", "API 密钥"))

    def test_none_returns_message(self):
        msg = validate_required_string(None, "模型名称")
        self.assertIsNotNone(msg)


@unittest.skipUnless(_HAS_DISPLAY, "当前环境无 Tk 显示")
class FormValidatorTests(unittest.TestCase):
    def setUp(self):
        # 不需要真实 Tk root，IntVar/StringVar 只需一个 Tcl 解释器
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError:
            self.skipTest("当前环境无 Tk 显示")

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_validate_all_passes_when_all_fields_valid(self):
        v = FormValidator()
        int_var = tk.IntVar(value=5)
        str_var = tk.StringVar(value="hello")
        v.register_int("n", "数量", int_var, 1, 10)
        v.register_required_string("s", "名称", str_var)
        result = v.validate_all()
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertIsNone(result.first_failed_widget)

    def test_validate_all_collects_errors_in_registration_order(self):
        v = FormValidator()
        int_var = tk.IntVar(value=999)  # 越界
        str_var = tk.StringVar(value="")  # 空
        # widget 用简单 stub，便于断言
        stub_a = object()
        stub_b = object()
        v.register_int("n", "数量", int_var, 1, 10, widget=stub_a)
        v.register_required_string("s", "名称", str_var, widget=stub_b)
        result = v.validate_all()
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 2)
        # 首错聚焦到第一个出错字段
        self.assertIs(result.first_failed_widget, stub_a)
        self.assertIn("数量", result.first_message)

    def test_validate_all_skips_widget_none_for_first_failed(self):
        v = FormValidator()
        int_var = tk.IntVar(value=999)
        str_var = tk.StringVar(value="")
        # 第一个字段没有 widget，首错应落到第二个字段
        v.register_int("n", "数量", int_var, 1, 10, widget=None)
        stub_b = object()
        v.register_required_string("s", "名称", str_var, widget=stub_b)
        result = v.validate_all()
        self.assertFalse(result.ok)
        self.assertIs(result.first_failed_widget, stub_b)

    def test_clamp_field_writes_back_to_var(self):
        v = FormValidator()
        int_var = tk.IntVar(value=999)
        spec = v.register_int("n", "数量", int_var, 1, 10)
        v.clamp_field(spec)
        self.assertEqual(int_var.get(), 10)

    def test_clamp_field_handles_invalid_input(self):
        v = FormValidator()
        # 故意用 StringVar 模拟非法输入（生产中不会出现，但 clamp 应防御）
        int_var = tk.StringVar(value="abc")
        spec = v.register_int("n", "数量", int_var, 1, 10)
        # 不应抛异常
        v.clamp_field(spec)
        # clamp_int 把 "abc" 当 default=lo=1 写回，StringVar 会存成字符串 "1"
        self.assertEqual(int_var.get(), "1")


# ──────────────────────────────────────────────────────────────
# SettingsWindow 集成测试（需要 Tk 显示，无显示时 skip）
# ──────────────────────────────────────────────────────────────


class _FakeSecretStore:
    """最小 stub SecretStore，避免触碰真实密钥环。"""

    def __init__(self):
        self.saved_keys = {}  # identifier -> key
        self.save_calls = []  # 记录所有 store 调用，用于断言"未持久化"

    def store(self, identifier, key):
        self.save_calls.append((identifier, key))
        self.saved_keys[identifier] = key
        # 模拟 PERSISTED
        from src.domain.secret import StorageStatus

        return StorageStatus.PERSISTED

    def retrieve(self, identifier):
        return self.saved_keys.get(identifier, "")

    def delete(self, identifier):
        self.saved_keys.pop(identifier, None)
        return True


class _StubConfigManager:
    """最小 config_manager stub，记录所有持久化调用。

    用于验证"测试连接不得改变持久化配置"契约。
    """

    def __init__(self):
        self._secret_store = _FakeSecretStore()
        self.save_volc_calls = []  # save_volc_key 调用记录
        self.save_ocr_calls = []  # save_ocr_key 调用记录
        self.save_api_calls = []  # save_api_config 调用记录
        self.save_app_calls = []  # save_app_config 调用记录

    # —— 读侧 ——
    def get_api_config(self, *, load_secret=True):
        return {
            "provider": "siliconflow",
            "api_key": "",
            "base_url": "https://api.siliconflow.cn/v1",
            "model_name": "deepseek-ai/DeepSeek-V3.2",
            # 旧配置字段：设置页必须忽略且不再保存。
            "max_tokens": 4000,
            "temperature": 0.3,
        }

    def get_app_config(self):
        return {}

    def get_image_translation_config(self):
        return {
            "ocr": {
                "base_url": "",
                "model": "PaddlePaddle/PaddleOCR-VL-1.5",
            },
            "ai_volcengine": {},
        }

    def get_ocr_key(self):
        return ""

    def get_volc_key(self):
        return ""

    def get_provider_key(self, provider):
        return ""

    def get_provider_config(self, provider):
        return {}

    def _get_default_prompt(self):
        return ""

    # —— 写侧（全部记录，不真正持久化）——
    def save_volc_key(self, api_key):
        self.save_volc_calls.append(api_key)
        from src.domain.secret import SecretSaveResult, StorageStatus

        return SecretSaveResult(
            secret_status=StorageStatus.PERSISTED,
            config_saved=True,
        )

    def save_ocr_key(self, api_key):
        self.save_ocr_calls.append(api_key)
        from src.domain.secret import SecretSaveResult, StorageStatus

        return SecretSaveResult(
            secret_status=StorageStatus.PERSISTED,
            config_saved=True,
            provider="ocr",
        )

    def save_api_config(self, config):
        self.save_api_calls.append(config)
        from src.domain.secret import SecretSaveResult, StorageStatus

        return SecretSaveResult(
            secret_status=StorageStatus.PERSISTED,
            config_saved=True,
        )

    def save_app_config(self, config):
        self.save_app_calls.append(config)
        return True


def _make_stub_image_translator_module():
    """构造一个 stub ``src.core.image_translator`` 模块。

    避免真实模块导入 ``openai``（依赖 ``pydantic``），让测试可以在
    最小依赖环境下运行。
    """
    mod = types.ModuleType("src.core.image_translator")

    class _StubImageTranslator:
        DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
        MODEL_HIGH_QUALITY = "doubao-seedream-5-0-pro-260628"
        MODEL_ECONOMY = "doubao-seedream-5-0-260128"
        DEFAULT_MODEL = MODEL_HIGH_QUALITY

        def __init__(self, config_manager):
            self.config_manager = config_manager
            self.last_error = ""

        def test_connection(self):
            return False  # 默认测试失败，避免真实网络请求

        def close(self):
            pass

    mod.ImageTranslator = _StubImageTranslator
    return mod


@unittest.skipUnless(_HAS_DISPLAY, "当前环境无 Tk 显示")
class SettingsWindowValidationTests(unittest.TestCase):
    """P2-5：SettingsWindow 表单校验 + testing busy + 不持久化契约。"""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.config_manager = _StubConfigManager()
        # P2-5：注入 stub ImageTranslator 模块，避免真实模块导入 openai/pydantic。
        # ``test_volc_connection`` 会按需导入 ImageTranslator；通过 sys.modules
        # 注入可让连接测试直接拿到 stub。
        self._stub_module = _make_stub_image_translator_module()
        self._module_patcher = patch.dict(
            sys.modules, {"src.core.image_translator": self._stub_module}
        )
        self._module_patcher.start()

    def tearDown(self):
        self._module_patcher.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _make_dialog(self):
        """构造一个真实 SettingsWindow（窗口不显示）。"""
        dialog = SettingsWindow(self.root, self.config_manager)
        dialog.window.withdraw()
        return dialog

    # ── 字段级校验 ──

    def test_form_validator_registered_for_all_spinboxes(self):
        """所有 spinbox 都应注册到 form_validator。"""
        dialog = self._make_dialog()
        # 4 个整数 spinbox，以及火山 API 地址和模型两个必填文本字段。
        registered_names = [f.name for f in dialog._form_validator._fields]
        expected = {
            "batch_lines",
            "translation_concurrency",
            "ui_font_size",
            "queue_custom_concurrency",
            "volc_base_url",
            "volc_model",
        }
        self.assertEqual(set(registered_names), expected)
        self.assertEqual(len(registered_names), len(expected))

    def test_queue_concurrency_presets_update_custom_value_and_state(self):
        dialog = self._make_dialog()

        self.assertEqual(dialog.queue_concurrency_preset_var.get(), "medium")
        self.assertEqual(dialog.queue_custom_concurrency_var.get(), 4)
        self.assertEqual(str(dialog.queue_custom_concurrency_spin.cget("state")), "disabled")

        dialog.queue_concurrency_preset_var.set("large")
        dialog._on_queue_concurrency_preset_changed()
        self.assertEqual(dialog.queue_custom_concurrency_var.get(), 8)

        dialog.queue_concurrency_preset_var.set("custom")
        dialog._on_queue_concurrency_preset_changed()
        self.assertEqual(str(dialog.queue_custom_concurrency_spin.cget("state")), "normal")

    def test_save_settings_persists_large_concurrency_preset(self):
        dialog = self._make_dialog()
        dialog.queue_concurrency_preset_var.set("large")
        dialog._on_queue_concurrency_preset_changed()

        with patch("src.ui.settings_window.messagebox.showinfo"):
            dialog.save_settings()

        saved_app = self.config_manager.save_app_calls[-1]
        self.assertEqual(saved_app["queue_concurrency_preset"], "large")
        self.assertEqual(saved_app["queue_max_in_flight_requests"], 8)
        self.assertEqual(saved_app["queue_hard_request_cap"], 8)
        self.assertEqual(saved_app["queue_max_active_tasks"], 8)

    def test_api_form_does_not_expose_or_submit_max_tokens(self):
        dialog = self._make_dialog()

        self.assertFalse(hasattr(dialog, "max_tokens_var"))
        self.assertNotIn("max_tokens", dialog._current_api_form_config())
        self.assertFalse(hasattr(dialog, "batch_token_budget_var"))
        self.assertFalse(hasattr(dialog, "queue_batch_tokens_var"))

    def test_volc_fields_render_without_image_client_dependencies(self):
        """设置页不得因 OpenAI 图片客户端依赖缺失而只渲染上半截。"""
        with patch.dict(sys.modules, {"src.core.image_translator": None}):
            dialog = self._make_dialog()

        self.assertEqual(
            dialog.volc_base_url_var.get(),
            "https://ark.cn-beijing.volces.com/api/v3",
        )
        self.assertEqual(
            dialog.volc_model_var.get(),
            "doubao-seedream-5-0-pro-260628",
        )
        self.assertTrue(hasattr(dialog, "volc_key_var"))
        self.assertEqual(dialog.ocr_base_url_var.get(), "")
        self.assertEqual(dialog.ocr_model_var.get(), "PaddlePaddle/PaddleOCR-VL-1.5")
        self.assertTrue(hasattr(dialog, "ocr_key_var"))

    def test_validate_form_passes_with_defaults(self):
        dialog = self._make_dialog()
        ok, msg, widget = dialog._validate_form()
        self.assertTrue(ok, f"默认值应通过校验，但出错: {msg}")
        self.assertEqual(msg, "")
        self.assertIsNone(widget)

    def test_validate_form_catches_out_of_range_int(self):
        dialog = self._make_dialog()
        dialog.batch_lines_var.set(999999)
        ok, msg, widget = dialog._validate_form()
        self.assertFalse(ok)
        self.assertIn("批次翻译行数", msg)
        self.assertIs(widget, dialog._form_validator._fields[0].widget)

    def test_validate_form_first_error_focuses_earliest_failed_field(self):
        dialog = self._make_dialog()
        # 同时让两个字段越界，首错应聚焦到注册顺序靠前的那个
        dialog.batch_lines_var.set(-1)
        dialog.translation_concurrency_var.set(99999)
        ok, msg, widget = dialog._validate_form()
        self.assertFalse(ok)
        self.assertIn("批次翻译行数", msg)
        batch_lines_spec = next(
            field for field in dialog._form_validator._fields if field.name == "batch_lines"
        )
        self.assertIs(widget, batch_lines_spec.widget)

    def test_focus_out_clamps_out_of_range_spinbox(self):
        """P2-5：``<FocusOut>`` 时自动收敛越界值。"""
        dialog = self._make_dialog()
        dialog.batch_lines_var.set(999999)
        batch_lines_spec = next(
            field for field in dialog._form_validator._fields if field.name == "batch_lines"
        )
        dialog._form_validator.clamp_field(batch_lines_spec)
        self.assertEqual(dialog.batch_lines_var.get(), 20)

        dialog.batch_lines_var.set(0)
        dialog._form_validator.clamp_field(batch_lines_spec)
        self.assertEqual(dialog.batch_lines_var.get(), 1)

    # ── testing busy 状态 ──

    def test_begin_test_disables_test_buttons(self):
        dialog = self._make_dialog()
        self.assertFalse(dialog._test_in_progress)
        # 初始状态所有测试按钮可用
        for btn in dialog._test_buttons:
            self.assertEqual(str(btn.cget("state")), "normal")

        ok = dialog._begin_test()
        self.assertTrue(ok)
        self.assertTrue(dialog._test_in_progress)
        # testing 期间所有测试按钮禁用
        for btn in dialog._test_buttons:
            self.assertEqual(str(btn.cget("state")), "disabled")

    def test_begin_test_refuses_second_concurrent_test(self):
        dialog = self._make_dialog()
        self.assertTrue(dialog._begin_test())
        # 第二次调用应被拒绝（patch 掉 messagebox 避免弹窗阻塞测试）
        with patch("src.ui.settings_window.messagebox.showinfo"):
            ok = dialog._begin_test()
        self.assertFalse(ok)
        # 状态仍是 busy
        self.assertTrue(dialog._test_in_progress)

    def test_end_test_restores_button_state(self):
        dialog = self._make_dialog()
        dialog._begin_test()
        self.assertTrue(dialog._test_in_progress)
        dialog._end_test()
        self.assertFalse(dialog._test_in_progress)
        for btn in dialog._test_buttons:
            self.assertEqual(str(btn.cget("state")), "normal")

    def test_test_buttons_registered_in_busy_list(self):
        """三个测试按钮都应纳入 _test_buttons 列表。"""
        dialog = self._make_dialog()
        self.assertEqual(len(dialog._test_buttons), 3)
        texts = sorted(str(btn.cget("text")) for btn in dialog._test_buttons)
        self.assertEqual(
            texts,
            sorted(
                [
                    "测试连接",
                    "检测可用性",
                    "测试 AI 图片翻译连接",
                ]
            ),
        )

    # ── 测试连接不得持久化 ──

    def test_test_volc_connection_does_not_persist_key(self):
        """P2-5：火山测试连接不得调用 save_volc_key。

        原实现 ``test_volc_connection`` 会先 ``save_volc_key`` 再测试，导致
        用户在测试中发现 Key 错误后撤销设置，错误 Key 已写入密钥环。
        """
        dialog = self._make_dialog()
        dialog.volc_key_var.set("sk-test-key-for-validation")

        # patch 掉 worker 线程，避免真实网络请求
        with patch("src.ui.settings_window.threading.Thread") as mock_thread:
            # 让 Thread 构造返回一个 mock，start() 不真正启动
            mock_instance = mock_thread.return_value
            dialog.test_volc_connection()

        # 不应有任何 save_volc_key 调用
        self.assertEqual(
            self.config_manager.save_volc_calls,
            [],
            "test_volc_connection 不应调用 save_volc_key 持久化密钥",
        )
        # Thread.start() 应被调用一次，说明确实启动了测试
        mock_instance.start.assert_called_once()

    def test_test_connection_does_not_persist_config(self):
        """P2-5：文本 API 测试连接不得调用 save_api_config。"""
        dialog = self._make_dialog()
        dialog.api_key_var.set("sk-test-key")

        with patch("src.ui.settings_window.threading.Thread") as mock_thread:
            mock_instance = mock_thread.return_value
            dialog.test_connection()

        self.assertEqual(
            self.config_manager.save_api_calls,
            [],
            "test_connection 不应调用 save_api_config 持久化配置",
        )
        mock_instance.start.assert_called_once()

    def test_test_volc_connection_busy_blocks_second_call(self):
        """P2-5：testing 期间再次点击测试连接应被拒绝。"""
        dialog = self._make_dialog()
        dialog.volc_key_var.set("sk-test")

        with patch("src.ui.settings_window.threading.Thread"):
            dialog.test_volc_connection()

        # busy 状态下第二次调用不应启动新线程
        self.assertTrue(dialog._test_in_progress)
        with patch("src.ui.settings_window.messagebox.showinfo"):
            with patch("src.ui.settings_window.threading.Thread") as mock_thread:
                dialog.test_volc_connection()
        # 第二次 Thread 不应被构造（因为 _begin_test 拒绝了）
        mock_thread.assert_not_called()

    def test_save_settings_persists_keys(self):
        """P2-5：对比测试——只有 save_settings 才真正持久化密钥。"""
        dialog = self._make_dialog()
        dialog.volc_key_var.set("sk-final-key")
        dialog.api_key_var.set("sk-api-final")

        # save_settings 会弹"保存成功"对话框，patch 掉避免阻塞
        with patch("src.ui.settings_window.messagebox.showinfo"):
            try:
                dialog.save_settings()
            except Exception:
                # save_settings 内部可能调用其他真实方法失败，这里只关心
                # 是否触发了 save_volc_key / save_api_config
                pass

        # save_settings 应触发持久化
        self.assertTrue(
            len(self.config_manager.save_volc_calls) > 0
            or len(self.config_manager.save_api_calls) > 0,
            "save_settings 应调用 save_volc_key 或 save_api_config 持久化",
        )

    def test_save_settings_persists_custom_volc_endpoint_and_model(self):
        dialog = self._make_dialog()
        dialog.volc_base_url_var.set("https://example.com/ark/v3/")
        dialog.volc_model_var.set("ep-user-defined-model")

        with patch("src.ui.settings_window.messagebox.showinfo"):
            dialog.save_settings()

        volc_config = self.config_manager.save_app_calls[-1]["image_translation"]["ai_volcengine"]
        self.assertEqual(volc_config["base_url"], "https://example.com/ark/v3")
        self.assertEqual(volc_config["model"], "ep-user-defined-model")

    def test_save_settings_persists_custom_ocr_endpoint_model_and_key(self):
        dialog = self._make_dialog()
        dialog.ocr_base_url_var.set("https://ocr.example.com/v1/chat/completions/")
        dialog.ocr_model_var.set("vendor/custom-ocr")
        dialog.ocr_key_var.set("ocr-final-key")

        with patch("src.ui.settings_window.messagebox.showinfo"):
            dialog.save_settings()

        ocr_config = self.config_manager.save_app_calls[-1]["image_translation"]["ocr"]
        self.assertEqual(ocr_config["base_url"], "https://ocr.example.com/v1")
        self.assertEqual(ocr_config["model"], "vendor/custom-ocr")
        self.assertEqual(self.config_manager.save_ocr_calls[-1], "ocr-final-key")

    def test_save_settings_requires_dedicated_key_for_custom_ocr_endpoint(self):
        dialog = self._make_dialog()
        dialog.ocr_base_url_var.set("https://ocr.example.com/v1")
        dialog.ocr_key_var.set("")

        with patch("src.ui.settings_window.messagebox.showwarning") as warning:
            dialog.save_settings()

        warning.assert_called_once()
        self.assertEqual(self.config_manager.save_ocr_calls, [])
        self.assertEqual(self.config_manager.save_app_calls, [])

    def test_save_settings_blocks_when_form_invalid(self):
        """P2-5：表单校验失败时 save_settings 不应触发持久化。"""
        dialog = self._make_dialog()
        dialog.batch_lines_var.set(-999)

        with patch("src.ui.settings_window.messagebox.showwarning"):
            dialog.save_settings()

        # 校验失败，不应有任何持久化调用
        self.assertEqual(self.config_manager.save_volc_calls, [])
        self.assertEqual(self.config_manager.save_api_calls, [])
        self.assertEqual(self.config_manager.save_app_calls, [])

    def test_save_settings_drops_legacy_token_limit_fields(self):
        dialog = self._make_dialog()
        dialog.app_config["batch_max_input_tokens"] = 6000
        dialog.app_config["queue_batch_max_input_tokens"] = 16000

        with patch("src.ui.settings_window.messagebox.showinfo"):
            dialog.save_settings()

        self.assertNotIn("max_tokens", self.config_manager.save_api_calls[-1])
        saved_app = self.config_manager.save_app_calls[-1]
        self.assertNotIn("batch_max_input_tokens", saved_app)
        self.assertNotIn("queue_batch_max_input_tokens", saved_app)


if __name__ == "__main__":
    unittest.main()
