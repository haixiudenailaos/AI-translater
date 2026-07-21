#!/usr/bin/env python3
"""
新手指导流程模块

本模块实现一套轻量内联引导条，帮助首次使用者快速了解配置、导入、
翻译、质检和导出五个核心环节。模块只负责说明、导航、记录进度并调用
已有命令，不重复实现配置、导入或翻译逻辑。

三个核心对象：
- ``OnboardingStep``：静态步骤文案与目标键。
- ``OnboardingPanel``：只负责渲染，不读取配置、不判断流程。
- ``OnboardingController``：管理状态、事件推进、目标定位和持久化。
"""

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any, Callable, Dict, List, Mapping

from ..utils.logger import get_logger

logger = get_logger(__name__)


# ── 步骤定义 ────────────────────────────────────────


@dataclass(frozen=True)
class OnboardingStep:
    """单个引导步骤的静态文案与可选的目标控件键。"""

    step_id: str
    title: str
    body: str
    primary_label: str
    target_key: str | None = None


#: 引导步骤的有序列表，顺序即用户的前进方向。
STEPS: List[OnboardingStep] = [
    OnboardingStep(
        step_id="welcome",
        title="第一次使用？用 1 分钟了解工作流",
        body="按“配置 API、导入内容、补译、质检、导出”的顺序走一遍。"
        "你可以随时退出，并从“帮助”菜单重新打开。",
        primary_label="开始指导",
    ),
    OnboardingStep(
        step_id="api",
        title="第 1 步：配置文本翻译 API",
        body="选择服务商并填写 API 密钥和模型。保存成功后，本步骤会自动完成。",
        primary_label="打开设置",
    ),
    OnboardingStep(
        step_id="import",
        title="第 2 步：导入内容",
        body="导入 EPUB 或 TXT，也可以直接粘贴剪贴板文本。导入后原文会显示在对照表中。",
        primary_label="导入文件",
    ),
    OnboardingStep(
        step_id="translate",
        title="第 3 步：翻译未完成行",
        body="主按钮只处理原文非空且译文为空的行。开始前请确认目标语言、模型和待翻译数量。",
        primary_label="定位主按钮",
        target_key="translate",
    ),
    OnboardingStep(
        step_id="review_export",
        title="第 4 步：质检并导出",
        body="翻译后可筛选未翻译行或质检问题。确认内容完整后，再导出 EPUB 或对照文件。",
        primary_label="定位质检筛选",
        target_key="review",
    ),
]


#: 步骤 ID 到主操作动作键的映射（仅对使用动作而非定位的步骤）。
_ACTION_FOR_STEP: Dict[str, str] = {
    "api": "open_settings",
    "import": "import_file",
}


# ── 内联面板 ────────────────────────────────────────


class OnboardingPanel:
    """新手指导内联面板。

    只负责渲染当前步骤的文案与按钮，所有流程判断由
    :class:`OnboardingController` 完成。面板放在工具栏与工作区之间，
    出现时通过 ``pack()`` 展开，隐藏时通过 ``pack_forget()`` 收起。
    """

    def __init__(
        self,
        parent,
        *,
        on_back: Callable[[], None],
        on_next: Callable[[], None],
        on_postpone: Callable[[], None],
        on_dismiss: Callable[[], None],
        on_visibility_changed: Callable[[bool], None] | None = None,
    ):
        self._on_back = on_back
        self._on_next = on_next
        self._on_postpone = on_postpone
        self._on_dismiss = on_dismiss
        self._on_visibility_changed = on_visibility_changed

        self._visible = False
        self._closed = False

        self._frame = ttk.Frame(parent, padding=(10, 6))

        # 头部：步骤计数 + 标题
        header = ttk.Frame(self._frame)
        header.pack(fill=tk.X)
        self._counter_label = ttk.Label(header, text="", font=("TkDefaultFont", 9, "bold"))
        self._counter_label.pack(side=tk.LEFT)
        self._title_label = ttk.Label(header, text="", font=("TkDefaultFont", 11, "bold"))
        self._title_label.pack(side=tk.LEFT, padx=(8, 0))

        # 正文
        self._body_label = ttk.Label(self._frame, text="", wraplength=700, justify=tk.LEFT)
        self._body_label.pack(fill=tk.X, pady=(4, 6))

        # 状态提示（如“API 已配置，可以继续。”）
        self._hint_label = ttk.Label(self._frame, text="", foreground="#0F766E")

        # 按钮栏
        btn_bar = ttk.Frame(self._frame)
        btn_bar.pack(fill=tk.X)

        # 左侧：稍后 / 跳过指导
        self._postpone_btn = ttk.Button(btn_bar, text="稍后", command=self._on_postpone)
        self._postpone_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._dismiss_btn = ttk.Button(btn_bar, text="跳过指导", command=self._on_dismiss)
        self._dismiss_btn.pack(side=tk.LEFT)

        # 右侧按钮（pack 顺序决定视觉次序：先 pack 的在最右）
        self._back_btn = ttk.Button(btn_bar, text="上一步", command=self._on_back)
        self._secondary_btn = ttk.Button(btn_bar, text="", command=self._on_secondary)
        self._advance_btn = ttk.Button(btn_bar, text="", command=self._on_advance)
        self._primary_btn = ttk.Button(btn_bar, text="", command=self._on_primary)

        # 命令缓存
        self._primary_command: Callable[[], None] | None = None
        self._secondary_command: Callable[[], None] | None = None
        self._advance_command: Callable[[], None] | None = None

        # 跟随窗口宽度调整正文换行
        self._frame.bind("<Configure>", self._on_configure)

        try:
            self._root = parent.winfo_toplevel()
        except Exception:
            self._root = parent

    # ── 渲染 ──────────────────────────────────────

    def show_step(
        self,
        step: OnboardingStep,
        *,
        index: int,
        total: int,
        can_go_back: bool,
        primary_command: Callable[[], None] | None,
        secondary_label: str | None = None,
        secondary_command: Callable[[], None] | None = None,
        advance_label: str | None = None,
        advance_command: Callable[[], None] | None = None,
        status_hint: str | None = None,
    ) -> None:
        """渲染指定步骤。``index`` 为 1 起的步骤序号。"""
        if self._closed:
            return

        self._counter_label.config(text=f"新手指导  {index}/{total}")
        self._title_label.config(text=step.title)
        self._body_label.config(text=step.body)

        if status_hint:
            self._hint_label.config(text=status_hint)
            if not self._hint_label.winfo_ismapped():
                self._hint_label.pack(fill=tk.X, pady=(0, 4))
        else:
            self._hint_label.config(text="")
            self._hint_label.pack_forget()

        # 主按钮
        self._primary_command = primary_command
        self._primary_btn.config(text=step.primary_label, command=self._on_primary)

        # 次按钮
        self._secondary_command = secondary_command
        if secondary_label:
            self._secondary_btn.config(text=secondary_label)

        # 前进/完成按钮
        self._advance_command = advance_command
        if advance_label:
            self._advance_btn.config(text=advance_label)

        # 重新 pack 右侧按钮，保证视觉次序：[上一步][次按钮][前进按钮][主按钮]
        for btn in (
            self._back_btn,
            self._secondary_btn,
            self._advance_btn,
            self._primary_btn,
        ):
            btn.pack_forget()
        self._primary_btn.pack(side=tk.RIGHT, padx=(4, 0))
        if advance_label:
            self._advance_btn.pack(side=tk.RIGHT, padx=(4, 0))
        if secondary_label:
            self._secondary_btn.pack(side=tk.RIGHT, padx=(4, 0))
        if can_go_back:
            self._back_btn.pack(side=tk.RIGHT, padx=(4, 0))

        self._maybe_focus_primary()

    def show(self) -> None:
        if self._closed:
            return
        if not self._visible:
            self._notify_visibility_changed(True)
            self._frame.pack(fill=tk.X, pady=(0, 8))
            self._visible = True
        self._bind_shortcuts()

    def hide(self) -> None:
        if self._visible:
            self._frame.pack_forget()
            self._visible = False
        self._notify_visibility_changed(False)
        self._unbind_shortcuts()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._notify_visibility_changed(False)
        self._unbind_shortcuts()
        try:
            self._frame.destroy()
        except Exception as exc:
            logger.debug("销毁新手指导面板失败: %s", exc)

    def _notify_visibility_changed(self, visible: bool) -> None:
        callback = self._on_visibility_changed
        if callback is None:
            return
        try:
            callback(visible)
        except Exception as exc:
            logger.debug("更新新手指导布局失败: %s", exc)

    # ── 内部回调 ──────────────────────────────────

    def _on_primary(self) -> None:
        if self._primary_command is not None:
            self._primary_command()

    def _on_secondary(self) -> None:
        if self._secondary_command is not None:
            self._secondary_command()

    def _on_advance(self) -> None:
        if self._advance_command is not None:
            self._advance_command()

    def _maybe_focus_primary(self) -> None:
        try:
            focused = self._frame.focus_get()
        except Exception:
            focused = None
        # 不抢占正在编辑的文本焦点
        if isinstance(focused, tk.Entry | ttk.Entry | tk.Text):
            return
        try:
            self._primary_btn.focus_set()
        except Exception:
            pass

    def _on_configure(self, _event) -> None:
        try:
            width = self._frame.winfo_width()
            if width > 40:
                self._body_label.config(wraplength=width - 24)
        except Exception:
            pass

    def _bind_shortcuts(self) -> None:
        try:
            self._root.bind("<Alt-Left>", lambda _e: self._on_back())
            self._root.bind("<Alt-Right>", lambda _e: self._on_next())
            self._root.bind("<Escape>", lambda _e: self._on_postpone())
        except Exception as exc:
            logger.debug("绑定新手指导快捷键失败: %s", exc)

    def _unbind_shortcuts(self) -> None:
        try:
            self._root.unbind("<Alt-Left>")
            self._root.unbind("<Alt-Right>")
            self._root.unbind("<Escape>")
        except Exception:
            pass


# ── 控制器 ──────────────────────────────────────────


class OnboardingController:
    """新手指导状态机与流程控制器。

    管理引导状态、业务事件推进、目标控件定位和持久化。控制器不创建
    Tk 控件，所有渲染委托给 :class:`OnboardingPanel`。
    """

    def __init__(
        self,
        *,
        root,
        panel: OnboardingPanel,
        config_manager,
        targets: Mapping[str, Callable[[], Any]],
        actions: Mapping[str, Callable[[], None]],
        status_updater: Callable[[str], None],
    ):
        self._root = root
        self._panel = panel
        self._config_manager = config_manager
        self._targets = dict(targets)
        self._actions = dict(actions)
        self._status_updater = status_updater

        self._steps = list(STEPS)
        self._step_ids: List[str] = [s.step_id for s in self._steps]

        self._state: Dict[str, Any] = self._load_state()
        self._active = False
        # 仅存在于内存中的会话标记：用户点击“稍后”后，本会话不再自动弹出
        self._postponed_this_session = False

    # ── 状态加载与持久化 ────────────────────────

    def _load_state(self) -> Dict[str, Any]:
        try:
            onboarding = self._config_manager.get_app_config().get("onboarding", {})
        except Exception as exc:
            logger.warning("读取新手指导配置失败，使用默认值: %s", exc)
            onboarding = {}

        if not isinstance(onboarding, dict):
            onboarding = {}

        def _get(key, default):
            value = onboarding.get(key, default)
            return value if isinstance(value, type(default)) else default

        state = {
            "schema_version": _get("schema_version", 1),
            "status": _get("status", "not_started"),
            "current_step": _get("current_step", "welcome"),
            "completed_steps": [
                s for s in onboarding.get("completed_steps", []) if isinstance(s, str)
            ],
            "auto_show": _get("auto_show", True),
        }

        # 状态值校正
        if state["status"] not in (
            "not_started",
            "in_progress",
            "completed",
            "dismissed",
        ):
            state["status"] = "not_started"
        # 去重 completed_steps
        seen = set()
        deduped = []
        for step_id in state["completed_steps"]:
            if step_id not in seen:
                seen.add(step_id)
                deduped.append(step_id)
        state["completed_steps"] = deduped
        # 未知 current_step 回退到第一个未完成步骤，否则 welcome
        if state["current_step"] not in self._step_ids:
            state["current_step"] = (
                self._first_incomplete_step(state["completed_steps"]) or "welcome"
            )

        return state

    def _first_incomplete_step(self, completed_steps=None) -> str | None:
        if completed_steps is None:
            completed_steps = self._state["completed_steps"]
        for step_id in self._step_ids:
            if step_id not in completed_steps:
                return step_id
        return None

    def _save(self) -> None:
        """持久化引导状态。

        只在开始、前进、后退、稍后、跳过、完成以及事件标记完成时调用。
        保存失败不阻断主界面，仅在状态栏提示。
        """
        try:
            config = self._config_manager.get_app_config()
            config["onboarding"] = {
                "schema_version": self._state["schema_version"],
                "status": self._state["status"],
                "current_step": self._state["current_step"],
                "completed_steps": list(self._state["completed_steps"]),
                "auto_show": self._state["auto_show"],
            }
            ok = self._config_manager.save_app_config(config)
            if not ok:
                self._status_updater("新手指导进度保存失败")
        except Exception as exc:
            logger.warning("新手指导进度保存失败: %s", exc)
            self._status_updater("新手指导进度保存失败")

    # ── 自动展示 ──────────────────────────────────

    def maybe_start(self, *, api_configured: bool, has_recent_files: bool) -> None:
        """启动时判断是否自动展示引导。

        - ``not_started``：需同时满足 auto_show、未配置 API 且无最近文件。
        - ``in_progress``：auto_show 且本会话未点击“稍后”时恢复。
        - ``completed``/``dismissed``：不自动展示。
        """
        if self._postponed_this_session:
            return
        if not self._state.get("auto_show", True):
            return

        status = self._state["status"]
        if status == "not_started":
            if not api_configured and not has_recent_files:
                self.start()
        elif status == "in_progress":
            self.start()

    def start(self, *, force: bool = False) -> None:
        """显示引导面板。

        ``force=True`` 时忽略自动展示条件，用于“帮助 > 新手指导”入口。
        对已完成或已跳过的用户，重新从 welcome 开始；对进行中的用户，
        从 ``current_step`` 恢复。
        """
        if force:
            if self._state["status"] in ("completed", "dismissed", "not_started"):
                self._state["status"] = "in_progress"
                self._state["current_step"] = "welcome"
                self._state["completed_steps"] = []
            # in_progress：从 current_step 恢复
        else:
            if self._state["status"] == "not_started":
                self._state["status"] = "in_progress"
                self._state["current_step"] = "welcome"

        self._postponed_this_session = False
        self._active = True
        self._save()
        self._panel.show()
        self._render_current_step()

    # ── 流程控制 ──────────────────────────────────

    def next(self) -> None:
        step = self._current_step_obj()
        if step is None:
            return
        index = self._step_ids.index(step.step_id)
        self._mark_completed(step.step_id)
        if index >= len(self._step_ids) - 1:
            self.finish()
            return
        self._state["current_step"] = self._step_ids[index + 1]
        self._state["status"] = "in_progress"
        self._save()
        self._render_current_step()

    def back(self) -> None:
        step = self._current_step_obj()
        if step is None:
            return
        index = self._step_ids.index(step.step_id)
        if index <= 0:
            return
        self._state["current_step"] = self._step_ids[index - 1]
        self._save()
        self._render_current_step()

    def postpone(self) -> None:
        """稍后：隐藏面板，保留当前位置与 in_progress 状态。

        设置仅存在于内存中的 ``_postponed_this_session`` 标记，避免当前
        会话再次自动弹出。
        """
        self._postponed_this_session = True
        if self._state["status"] == "not_started":
            self._state["status"] = "in_progress"
        self._save()
        self._active = False
        self._panel.hide()

    def dismiss(self) -> None:
        """跳过指导：写入 dismissed，以后不再自动展示。"""
        self._state["status"] = "dismissed"
        self._save()
        self._active = False
        self._panel.hide()

    def finish(self) -> None:
        """完成指导：标记全部步骤完成，写入 completed。"""
        self._state["status"] = "completed"
        self._state["current_step"] = self._step_ids[-1]
        for step_id in self._step_ids:
            self._mark_completed(step_id)
        self._save()
        self._active = False
        self._panel.hide()

    def close(self) -> None:
        """主窗口关闭时调用，取消面板注册的快捷键与回调。可重复调用。"""
        self._active = False
        try:
            self._panel.close()
        except Exception as exc:
            logger.debug("关闭新手指导面板失败: %s", exc)

    # ── 业务事件联动 ──────────────────────────────

    def notify(self, event: str, **payload: Any) -> None:
        """接收业务事件，更新已完成步骤并按需自动前进。

        事件发生在引导未显示时，也可更新 ``completed_steps``，但不会自动
        弹出面板。自动前进通过 ``root.after_idle`` 调度。
        """
        changed = False
        advance: tuple | None = None

        if event == "api_status_changed":
            if payload.get("configured", False):
                if self._mark_completed("api"):
                    changed = True
                advance = ("api", "import")
        elif event == "content_loaded":
            if payload.get("count", 0) > 0:
                if self._mark_completed("import"):
                    changed = True
                advance = ("import", "translate")
        elif event == "translation_started":
            if self._mark_completed("translate"):
                changed = True
            # 不自动跳到结束，用户仍应看见质检说明
        elif event == "quality_check_run":
            if self._mark_completed("review_export"):
                changed = True
            # 不自动把总状态改为 completed
        else:
            return

        if changed:
            self._save()
        if advance is not None:
            self._maybe_auto_advance(*advance)

    def _maybe_auto_advance(self, current_step_id: str, next_step_id: str) -> None:
        if not self._active:
            return
        if self._state["current_step"] != current_step_id:
            return

        def _advance() -> None:
            if not self._active:
                return
            if self._state["current_step"] != current_step_id:
                return
            self._state["current_step"] = next_step_id
            self._state["status"] = "in_progress"
            self._save()
            self._render_current_step()

        self._schedule(_advance)

    def _schedule(self, callback: Callable[[], None]) -> None:
        after_idle = getattr(self._root, "after_idle", None)
        if callable(after_idle):
            try:
                after_idle(callback)
                return
            except Exception as exc:
                logger.debug("after_idle 调度失败，直接执行: %s", exc)
        callback()

    # ── 渲染辅助 ──────────────────────────────────

    def _current_step_obj(self) -> OnboardingStep | None:
        step_id = self._state.get("current_step", "welcome")
        for step in self._steps:
            if step.step_id == step_id:
                return step
        return self._steps[0]

    def _mark_completed(self, step_id: str) -> bool:
        if step_id not in self._step_ids:
            return False
        if step_id in self._state["completed_steps"]:
            return False
        self._state["completed_steps"].append(step_id)
        return True

    def _render_current_step(self) -> None:
        step = self._current_step_obj()
        if step is None:
            return
        index = self._step_ids.index(step.step_id) + 1
        total = len(self._step_ids)
        can_go_back = index > 1

        primary_command = self._primary_command_for(step)

        secondary_label: str | None = None
        secondary_command: Callable[[], None] | None = None
        advance_label: str | None = None
        advance_command: Callable[[], None] | None = None
        status_hint: str | None = None

        if step.step_id == "api":
            if "api" in self._state["completed_steps"]:
                status_hint = "API 已配置，可以继续。"
            secondary_label = "以后配置"
            secondary_command = self.next
        elif step.step_id == "import":
            secondary_label = "粘贴文本"
            secondary_command = self._actions.get("paste_text")
        elif step.step_id == "translate":
            advance_label = "下一步"
            advance_command = self.next
        elif step.step_id == "review_export":
            advance_label = "完成指导"
            advance_command = self.finish

        self._panel.show_step(
            step,
            index=index,
            total=total,
            can_go_back=can_go_back,
            primary_command=primary_command,
            secondary_label=secondary_label,
            secondary_command=secondary_command,
            advance_label=advance_label,
            advance_command=advance_command,
            status_hint=status_hint,
        )

    def _primary_command_for(self, step: OnboardingStep) -> Callable[[], None] | None:
        if step.step_id == "welcome":
            return self.next
        if step.target_key is not None:
            return lambda: self._locate(step.target_key)
        action_key = _ACTION_FOR_STEP.get(step.step_id)
        if action_key is not None:
            return self._actions.get(action_key)
        return None

    def _locate(self, target_key: str) -> None:
        """定位目标控件：只设置键盘焦点，不触发按钮命令。"""
        factory = self._targets.get(target_key)
        if factory is None:
            logger.debug("未注册引导目标 [%s]", target_key)
            return
        try:
            widget = factory()
        except Exception as exc:
            logger.debug("获取引导目标控件失败 [%s]: %s", target_key, exc)
            return
        if widget is None:
            logger.debug("引导目标控件不存在 [%s]", target_key)
            return
        try:
            if hasattr(widget, "winfo_exists") and not widget.winfo_exists():
                logger.debug("引导目标控件已销毁 [%s]", target_key)
                return
            widget.focus_set()
        except Exception as exc:
            logger.debug("聚焦引导目标控件失败 [%s]: %s", target_key, exc)
