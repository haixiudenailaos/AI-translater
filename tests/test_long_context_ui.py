#!/usr/bin/env python3
"""LC-13 / LC-14：超长上下文的表单校验与入口状态。

这些用例聚焦**纯逻辑**（不创建 Tk 窗口），因此可在无显示环境的 CI 中运行：

- 预算字段接受任意正整数（含 1,048,576），拒绝空值/0/负数/小数/布尔值/
  全角数字，且不做静默截断。
- 预检/预览与运行时使用同一套预算解析，展示的模式与有效预算一致（LC-13）。
- 小模型模式与超长模式互斥的入口状态（LC-14）。
"""

import pytest

from src.application.context_budget import (
    describe_budget,
    resolve_context_budget,
    resolve_long_budget,
    resolve_model_context_tokens,
    resolve_model_max_output_tokens,
)
from src.domain.translation_policy import ContextMode, TranslationContextPolicy
from src.ui.form_validation import (
    parse_positive_int_text,
    validate_int_range,
    validate_positive_int_text,
)


class TestBudgetFieldValidation:
    @pytest.mark.parametrize("value", ["32768", "65536", "131072", "1048576", "1"])
    def test_accepts_any_positive_integer(self, value):
        assert validate_positive_int_text(value, "单次上下文大小") is None
        assert parse_positive_int_text(value) == int(value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "0",
            "-1",
            "-131072",
            "1.5",
            "12.5",
            "abc",
            "1e6",
            "０１２",  # 全角数字：int() 会静默误读为 12
            "1,048,576",  # 千分位分隔符不是裸整数
            None,
        ],
    )
    def test_rejects_invalid_input_with_field_message(self, value):
        message = validate_positive_int_text(value, "单次上下文大小")
        assert message is not None
        assert "单次上下文大小" in message
        assert parse_positive_int_text(value) is None

    def test_surrounding_whitespace_is_trimmed_not_rejected(self):
        """前后空白是复制粘贴的常见产物，去掉后合法即接受。"""
        assert validate_positive_int_text("  131072  ", "单次上下文大小") is None
        assert parse_positive_int_text("  131072  ") == 131072

    def test_rejects_booleans(self):
        # bool 是 int 子类：True 不能变成预算 1。
        assert parse_positive_int_text(True) is None
        assert parse_positive_int_text(False) is None

    def test_no_artificial_maximum(self):
        """不得用 sys.maxsize 之类的哨兵值表达"不限"。"""
        huge = str(10**12)
        assert validate_positive_int_text(huge, "单次上下文大小") is None
        assert parse_positive_int_text(huge) == 10**12

    def test_not_clamped_by_legacy_limits(self):
        """旧控件会把值裁剪到 6,000/20,000；新字段不得如此。"""
        value = "1048576"
        assert validate_positive_int_text(value, "单次上下文大小") is None
        # 对照：旧的通用范围校验确实会拒绝该值。
        assert validate_int_range(value, 1, 20000, "批次行数") is not None


class TestPreflightMatchesRuntime:
    """LC-13：UI 预检与真实分批使用同一策略和估算。"""

    def test_long_policy_resolves_same_budget_as_engine(self):
        from src.core.translator import TranslatorEngine

        class Config:
            def __init__(self):
                self._app_config = {"target_language": "中文"}
                self._api_config = {"context_window_tokens": 131072, "model_name": "m"}

            def get_app_config(self):
                return dict(self._app_config)

            def get_api_config(self, *, load_secret=True):
                return dict(self._api_config)

            def get_glossary_prompt(self):
                return ""

        policy = TranslationContextPolicy(ContextMode.LONG, 262144)
        engine = TranslatorEngine(Config())
        python_budget = engine.resolve_run_budget(policy)
        ui_budget = resolve_long_budget(
            requested_context_tokens=262144, model_context_tokens=131072
        )
        assert python_budget == ui_budget
        # 展示文案与实际有效预算一致。
        narrative = describe_budget(ui_budget, model_context_tokens=131072)
        assert f"{ui_budget.effective_context_tokens:,}" in narrative.text

    def test_standard_policy_comparison_uses_same_entry(self):
        policy = TranslationContextPolicy()
        budget = resolve_context_budget(
            policy, model_context_tokens=32768, configured_input_budget=6000
        )
        assert budget.mode is ContextMode.STANDARD
        assert budget.batch_budget_tokens == 6000

    def test_model_capacity_read_from_declared_config_only(self):
        # 只读用户声明值；未声明时回退默认，不按模型名推断。
        assert resolve_model_context_tokens({"context_window_tokens": "262144"}) == 262144
        assert resolve_model_context_tokens({"model_name": "huge-model-1m"}) == 32768


class TestSmallModelModeMutualExclusion:
    """LC-14：小模型模式与超长模式互斥。"""

    def test_engine_rejects_long_mode_when_small_model_enabled(self):
        from src.core.translation_result import TranslationRequestError
        from src.core.translator import TranslatorEngine

        class Config:
            def __init__(self):
                self._app_config = {
                    "target_language": "中文",
                    "small_model_mode": True,
                }
                self._api_config = {"context_window_tokens": 131072, "model_name": "m"}

            def get_app_config(self):
                return dict(self._app_config)

            def get_api_config(self, *, load_secret=True):
                return dict(self._api_config)

            def get_glossary_prompt(self):
                return ""

        class NoopApi:
            def configure_serial_transport(self, serial):
                pass

            def reset_cancel(self):
                pass

            def cancel_requests(self):
                pass

            def translate_stream(self, prompt, callback, system_prompt=None):
                raise AssertionError("互斥时不得发出请求")

        engine = TranslatorEngine(Config())
        engine.api = NoopApi()
        with pytest.raises(TranslationRequestError) as excinfo:
            engine.translate_long_context_mode(
                "hello",
                lambda *_a: None,
                lambda _r: None,
                wallet=TranslationContextPolicy(ContextMode.LONG, 131072),
            )
        assert "小模型" in str(excinfo.value)

    def test_preflight_note_matches_runtime_budget(self):
        """LC-13：预检展示的有效预算与运行时解析一致。"""
        from src.application.context_budget import describe_budget, resolve_long_budget

        api_config = {"context_window_tokens": 32768}
        requested = 1_048_576

        # 预检侧（main_window._long_context_preflight_note 使用的同一调用）
        preflight_budget = resolve_long_budget(
            requested_context_tokens=requested,
            model_context_tokens=resolve_model_context_tokens(api_config),
            model_max_output_tokens=resolve_model_max_output_tokens(api_config),
        )
        note = describe_budget(preflight_budget, model_context_tokens=32768).text

        # 运行时侧（引擎解析）
        from src.core.translator import TranslatorEngine

        class Config:
            def __init__(self):
                self._app = {"target_language": "中文"}
                self._api = dict(api_config)

            def get_app_config(self):
                return dict(self._app)

            def get_api_config(self, *, load_secret=True):
                return dict(self._api)

            def get_glossary_prompt(self):
                return ""

        engine = TranslatorEngine(Config())
        runtime_budget = engine.resolve_run_budget(
            TranslationContextPolicy(ContextMode.LONG, requested)
        )

        assert runtime_budget == preflight_budget
        assert f"{runtime_budget.effective_context_tokens:,}" in note
        assert f"{requested:,}" in note

    def test_preflight_does_not_add_unconditional_confirmation(self):
        """LC-13：无无条件额外确认弹窗——预检只在有警告时才询问。"""
        import inspect

        from src.ui.main_window import MainWindow

        source = inspect.getsource(MainWindow._long_context_preflight_note)
        # 该方法只解析预算并返回文案，不弹出确认对话框。
        assert "askyesno" not in source
        assert "askokcancel" not in source

    def test_small_model_mode_still_allows_standard_translation(self):
        from src.core.translation_result import TranslationStatus
        from src.core.translator import TranslatorEngine

        class Config:
            def __init__(self):
                self._app_config = {
                    "target_language": "中文",
                    "small_model_mode": True,
                    "queue_max_in_flight_requests": 1,
                    "queue_hard_request_cap": 1,
                }
                self._api_config = {"context_window_tokens": 32768, "model_name": "m"}

            def get_app_config(self):
                return dict(self._app_config)

            def get_api_config(self, *, load_secret=True):
                return dict(self._api_config)

            def get_glossary_prompt(self):
                return ""

        class Api:
            def configure_serial_transport(self, serial):
                pass

            def reset_cancel(self):
                pass

            def cancel_requests(self):
                pass

            def translate_stream(self, prompt, callback, system_prompt=None):
                result = prompt.replace("[LINE_001]", "[LINE_001]译:")
                if callback:
                    callback(result)
                return result

        engine = TranslatorEngine(Config())
        engine.api = Api()
        completed = []
        engine.translate_fast_mode("a", lambda *_a: None, lambda r: completed.append(r))
        assert completed[0].status is TranslationStatus.SUCCEEDED
