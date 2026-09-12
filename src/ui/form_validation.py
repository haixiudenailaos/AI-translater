"""P2-5：表单字段级校验工具。

为 ``SettingsWindow`` 提供可复用的字段校验器：

- ``clamp_int``：把越界值收敛到 ``[lo, hi]``。
- ``validate_int_range`` / ``validate_required_string``：单字段校验，返回
  ``None`` 或可读错误消息。
- ``FormValidator``：聚合字段配置，``validate_all`` 返回
  ``(errors, first_failed_widget)``，供调用方聚焦首错。

设计目标：

1. 即时反馈：``<FocusOut>`` 时单字段校验并修正，避免保存时才弹通用错误。
2. 首错聚焦：跨字段校验失败时把焦点设到第一个出错字段。
3. 不依赖 Tk：核心校验纯 Python，便于单元测试；widget 引用由调用方注入。
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Any, List, Tuple, cast

TkVar = tk.IntVar | tk.DoubleVar | tk.StringVar | tk.BooleanVar


@dataclass
class FieldSpec:
    """单个字段的最小校验配置。

    ``widget`` 可为 ``None``（如纯逻辑字段），此时不会进入首错聚焦候选。
    """

    name: str
    label: str
    var: TkVar
    kind: str  # "int" | "string"
    lo: int | None = None
    hi: int | None = None
    required: bool = False
    widget: object | None = None


@dataclass
class ValidationResult:
    """``FormValidator.validate_all`` 的返回值。"""

    ok: bool
    errors: List[Tuple[str, str]]  # [(field_name, message), ...]
    first_failed_widget: object | None = None

    @property
    def first_message(self) -> str | None:
        if not self.errors:
            return None
        return self.errors[0][1]


def clamp_int(value: object, lo: int, hi: int, default: int) -> int:
    """把任意输入收敛到 ``[lo, hi]``，无法解析时返回 ``default``。"""
    try:
        v = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def validate_int_range(value: object, lo: int, hi: int, label: str) -> str | None:
    """整数范围校验。返回 ``None`` 表示通过，否则返回错误消息。"""
    try:
        v = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"{label}必须是整数"
    if v < lo:
        return f"{label}不能小于 {lo}"
    if v > hi:
        return f"{label}不能大于 {hi}"
    return None


def validate_required_string(value: object, label: str) -> str | None:
    """必填字符串校验。"""
    if value is None:
        return f"{label}不能为空"
    text = str(value).strip()
    if not text:
        return f"{label}不能为空"
    return None


def validate_positive_int_text(value: object, label: str) -> str | None:
    """校验"无上限正整数"文本字段（如超长上下文预算）。

    与 :func:`validate_int_range` 的差别：

    1. **不设人为最大值**——用户可输入 1,048,576 甚至更大，不用
       ``sys.maxsize`` 之类的哨兵值表达"不限"。
    2. **不做静默截断**——``int(1.5)`` 会接受 1.5 并截断为 1，
       ``int("１２")`` 会接受全角数字，这里都拒绝。
    3. 返回字段级错误消息，调用方聚焦字段并保留待编辑内容。
    """
    if value is None:
        return f"{label}不能为空"
    text = str(value).strip()
    if not text:
        return f"{label}不能为空"
    if not all(char in "0123456789" for char in text):
        return f"{label}必须是正整数（不能为空、0、负数、小数或非数字内容）"
    if int(text) <= 0:  # pragma: no cover - 全 0 字符串由上一分支覆盖
        return f"{label}必须大于 0"
    return None


def parse_positive_int_text(value: object) -> int | None:
    """把已通过 :func:`validate_positive_int_text` 的文本转成整数。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or not all(char in "0123456789" for char in text):
        return None
    parsed = int(text)
    return parsed if parsed > 0 else None


class FormValidator:
    """聚合多个 ``FieldSpec``，提供整表校验。

    用法::

        validator = FormValidator()
        validator.register_int("batch_lines", "批次翻译行数", var, 1, 200, spin_widget)
        result = validator.validate_all()
        if not result.ok:
            messagebox.showwarning("配置无效", result.first_message)
            if result.first_failed_widget is not None:
                result.first_failed_widget.focus_set()
    """

    def __init__(self) -> None:
        self._fields: List[FieldSpec] = []

    def register_int(
        self,
        name: str,
        label: str,
        var: TkVar,
        lo: int,
        hi: int,
        widget: object | None = None,
    ) -> FieldSpec:
        spec = FieldSpec(
            name=name,
            label=label,
            var=var,
            kind="int",
            lo=lo,
            hi=hi,
            required=True,
            widget=widget,
        )
        self._fields.append(spec)
        return spec

    def register_required_string(
        self,
        name: str,
        label: str,
        var: TkVar,
        widget: object | None = None,
    ) -> FieldSpec:
        spec = FieldSpec(
            name=name,
            label=label,
            var=var,
            kind="string",
            required=True,
            widget=widget,
        )
        self._fields.append(spec)
        return spec

    def validate_field(self, spec: FieldSpec) -> str | None:
        """单字段校验。"""
        try:
            value = spec.var.get()
        except (tk.TclError, AttributeError):
            return f"{spec.label}无效"
        if spec.kind == "int":
            assert spec.lo is not None and spec.hi is not None
            return validate_int_range(value, spec.lo, spec.hi, spec.label)
        if spec.required:
            return validate_required_string(value, spec.label)
        return None

    def validate_all(self) -> ValidationResult:
        errors: List[Tuple[str, str]] = []
        first_failed_widget: object | None = None
        for spec in self._fields:
            msg = self.validate_field(spec)
            if msg is not None:
                errors.append((spec.name, msg))
                if first_failed_widget is None and spec.widget is not None:
                    first_failed_widget = spec.widget
        return ValidationResult(
            ok=not errors,
            errors=errors,
            first_failed_widget=first_failed_widget,
        )

    def clamp_field(self, spec: FieldSpec) -> None:
        """把字段值收敛到 ``[lo, hi]``，原地写回 ``var``。"""
        if spec.kind != "int" or spec.lo is None or spec.hi is None:
            return
        try:
            current = spec.var.get()
        except (tk.TclError, AttributeError):
            return
        clamped = clamp_int(current, spec.lo, spec.hi, default=spec.lo)
        # StringVar is used by editable spinboxes and text fields; writing only
        # to IntVar leaves invalid text visible and defeats the clamp contract.
        try:
            if isinstance(spec.var, tk.IntVar):
                spec.var.set(clamped)
            else:
                cast(Any, spec.var).set(str(clamped))
        except tk.TclError:
            pass

    def attach_focus_out_clamp(self, spec: FieldSpec) -> None:
        """为字段绑定 ``<FocusOut>`` 自动 clamp + 即时校验。

        适用于 ``ttk.Spinbox``：用户离开字段时若输入非法文本，立即收敛到合法值，
        避免保存时才暴露错误。
        """
        widget = spec.widget
        if widget is None:
            return

        def _on_focus_out(_event: tk.Event) -> None:
            self.clamp_field(spec)

        binder = getattr(widget, "bind", None)
        if not callable(binder):
            return
        try:
            binder("<FocusOut>", _on_focus_out, add="+")
        except tk.TclError:
            pass


__all__ = [
    "FieldSpec",
    "ValidationResult",
    "FormValidator",
    "clamp_int",
    "parse_positive_int_text",
    "validate_int_range",
    "validate_positive_int_text",
    "validate_required_string",
]
