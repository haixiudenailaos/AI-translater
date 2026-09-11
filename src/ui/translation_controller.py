#!/usr/bin/env python3
"""
翻译控制器模块
从MainWindow中提取的翻译流程控制逻辑
"""

import threading
import time
import tkinter as tk
import uuid
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, List, Tuple

from ..application.error_handling import (
    ActionableError,
    RetryPolicy,
    classify_error,
    log_classified_error,
)
from ..application.export_job import ExportJob, ExportProgress, ExportResult, TextExportJob
from ..application.translation_document import TranslationDocument
from ..application.translation_events import (
    TranslationEventKind,
    TranslationProgressEvent,
)
from ..config.translation_profile import SMALL_MODEL_MODE_CONFIG_KEY
from ..core.translation_result import BatchTranslationResult, TranslationStatus
from ..domain.translation_policy import ContextMode, TranslationContextPolicy
from ..utils.logger import get_logger
from .export_helpers import (
    build_default_epub_filename,
    load_image_text_translations,
    load_image_translation_result,
)
from .tk_event_pump import TkTranslationEventPump
from .translation_event_mailbox import TranslationEventMailbox
from .translation_table_adapter import TranslationTableAdapter

logger = get_logger(__name__)

# BUG-004：自动查漏单行/单批最多补译次数
MAX_MISSING_CHECK_ROUNDS = 2

# PERF：事件泵默认刷新间隔（20 次/秒，满足"不超过 25 次/秒"的验收标准）
DEFAULT_EVENT_PUMP_INTERVAL_MS = 50

# 长请求存活提示：DeepSeek 等模型常在首 token 前静默十余秒。状态栏在这段
# 时间不变，用户无法区分"正在等模型"和"程序卡死"。超过此阈值后显示已等待
# 秒数；收到流式数据后计时归零。
_LIVENESS_TICK_MS = 1000
_LIVENESS_SILENCE_SECONDS = 8

# 小模型模式强制逐行，与超长批次语义互斥（实现指南 §3.2）。UI 与服务边界
# 使用同一条消息，避免两处给出不同解释。
_SMALL_MODEL_LONG_CONTEXT_CONFLICT_MESSAGE = (
    "小模型模式与超长上下文翻译不能同时使用：小模型模式强制逐行翻译，"
    "与超长批次语义冲突。请先关闭小模型模式（设置 → 翻译设置），"
    "或改用普通翻译。"
)


class TranslationController:
    """翻译控制器：管理翻译流程、进度更新、状态管理

    PERF：后台工作线程不再直接调用 ``root.after()`` 或修改 Tk 控件，
    而是发布 ``TranslationProgressEvent`` 到事件邮箱。Tk 主线程的事件泵
    排空邮箱并驱动渲染。详见
    TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md §6。
    """

    def __init__(
        self,
        root: tk.Tk,
        config_manager,
        translator,
        file_handler,
        epub_processor,
        translation_table: ttk.Treeview,
        progress_var: tk.DoubleVar,
        progress_bar: ttk.Progressbar,
        translate_btn,
        continue_btn,
        stop_btn,
        status_updater: Callable[[str], None],
        get_table_data: Callable[[], Tuple[List[str], List[str]]],
        schedule_save: Callable,
        open_settings: Callable,
        get_source_path: Callable,
        get_mapping_dir: Callable,
        document: TranslationDocument | None = None,
        table_adapter: TranslationTableAdapter | None = None,
        get_target_path: Callable[[], "Path | None"] | None = None,
        update_target_path: Callable[[Path], None] | None = None,
        on_save_success: Callable[[], None] | None = None,
        preflight_callback: Callable[[str], bool] | None = None,
        on_run_terminal: Callable[[BatchTranslationResult, str], None] | None = None,
        start_btn=None,
        retranslate_btn=None,
        on_retranslated: Callable[[set[int]], None] | None = None,
        mode_toggle=None,
        backup_dir: Path | str | None = None,
        long_context_btn=None,
    ):
        self.root = root
        self.config_manager = config_manager
        self.translator = translator
        self.file_handler = file_handler
        self.epub_processor = epub_processor
        self.translation_table = translation_table
        # STORAGE-6：仅在组合根注入数据目录后启用文件级译文备份。旧的
        # 测试/直接构造路径保持不创建备份文件。
        self._backup_dir = Path(backup_dir).resolve() if backup_dir is not None else None
        self.progress_var = progress_var
        self.progress_bar = progress_bar
        self.start_btn = start_btn
        self.translate_btn = translate_btn
        self.retranslate_btn = retranslate_btn
        self.long_context_btn = long_context_btn
        self.mode_toggle = mode_toggle
        self.continue_btn = continue_btn
        self.stop_btn = stop_btn
        self.status_updater = status_updater
        self.get_table_data = get_table_data
        self.schedule_save = schedule_save
        self.open_settings = open_settings
        self.get_source_path = get_source_path
        self.get_mapping_dir = get_mapping_dir
        # PERF §7：文档模型和表格适配器。
        # 渲染热路径通过适配器批量写入，避免 get_children() 和逐行 item()。
        self._document = document
        self._table_adapter = table_adapter
        # P0-3：保存语义修复所需回调。
        # - get_target_path：读取当前 session 的 target_path；None 表示无目标。
        # - update_target_path：Save As 成功后将新目标写回 session。
        # - on_save_success：成功保存后清除 MainWindow 的 dirty 状态。
        self._get_target_path = get_target_path or (lambda: None)
        self._update_target_path = update_target_path or (lambda _path: None)
        self._on_save_success = on_save_success or (lambda: None)
        self._preflight_callback = preflight_callback
        self._on_run_terminal = on_run_terminal or (lambda _result, _mode: None)
        self._on_retranslated = on_retranslated or (lambda _indices: None)

        # Translation state
        self.is_translating = False
        self._continue_start_line = 0
        self._continue_missing_indices = []  # R2-BUG-014：续翻缺失行原始索引
        self._selected_translation_data = []
        self._missing_translation_indices = []
        self._is_missing_check = False
        # BUG-004：自动查漏轮次计数与失败索引记录
        self._missing_check_rounds = 0
        self._last_missing_failed_indices = []
        self._missing_retry_policy = RetryPolicy(max_attempts=MAX_MISSING_CHECK_ROUNDS)

        # Continuation mode flags
        self._continuing_mode = False
        self._continuing_first_insert = False
        self._translation_buffer = []

        # 右键菜单
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="翻译选中行", command=self.translate_selected_rows)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="取消", command=lambda: self.context_menu.unpost())

        # PERF：事件邮箱 + Tk 主线程事件泵，取代旧的 TkUpdateCoalescer。
        # 工作线程只调用 mailbox.publish()，事件泵在主线程排空并渲染。
        self._mailbox = TranslationEventMailbox()
        self._event_pump = TkTranslationEventPump(
            self.root,
            self._mailbox,
            self._render_event,
            interval_ms=DEFAULT_EVENT_PUMP_INTERVAL_MS,
        )
        self._event_pump.start()
        # 当前运行 ID：用于丢弃旧任务的迟到事件。
        self._current_run_id: str | None = None
        # 当前翻译模式："full" | "retranslate" | "selected" | "missing" | None
        self._current_mode: str | None = None
        # 工作线程暂存的终结结果，主线程通过 run_id 取回。
        self._pending_results: dict[str, BatchTranslationResult] = {}
        self._pending_errors: dict[str, str] = {}
        self._results_lock = threading.Lock()
        # Retired run IDs prevent late worker callbacks from leaking pending
        # results after a document replacement or window close.
        self._retired_run_ids: deque[str] = deque(maxlen=64)
        # P0-2：流式预览仅写入 Treeview，不写入 TranslationDocument。
        # 此集合记录当前已有未提交预览的行索引，供终态取消/失败时回退。
        # BATCH_COMPLETED 提交后会从此集合移除对应行。
        self._streaming_preview_rows: set[int] = set()
        self._export_job: ExportJob | TextExportJob | None = None
        self._export_poll_after_id: str | None = None
        self._export_kind: str | None = None
        # 每个 run_id 冻结的上下文策略（None = 普通模式）。工作线程按 run_id
        # 取用后立即移除；三个 worker 因此可以共用同一 (content, run_id) 签名。
        self._run_context_policies: dict[str, TranslationContextPolicy | None] = {}
        self._export_success_callback: Callable[[], None] | None = None
        # 长请求存活提示：记录最近一次流式活动时间和 after 回调。
        self._last_stream_activity_at: float | None = None
        self._liveness_after_id: str | None = None
        self._liveness_base_message: str = ""

    def _set_control_states(self, translate_state, continue_state, stop_state) -> None:
        """Keep all text-translation actions synchronized.

        超长上下文按钮与翻译按钮同步禁用/启用：运行期间两者都不能启动重叠运行。
        小模型模式下超长按钮保持禁用（互斥），由 ``refresh_action_state`` 在
        启用路径上再次校验。
        """
        seen: set[int] = set()
        for button in (
            getattr(self, "start_btn", None),
            getattr(self, "translate_btn", None),
            getattr(self, "retranslate_btn", None),
        ):
            if button is not None and id(button) not in seen:
                button.config(state=translate_state)
                seen.add(id(button))
        long_btn = getattr(self, "long_context_btn", None)
        if long_btn is not None:
            long_state = translate_state
            if translate_state != tk.DISABLED and self._small_model_mode_enabled():
                long_state = tk.DISABLED
            long_btn.config(state=long_state)
        self.continue_btn.config(state=continue_state)
        self.stop_btn.config(state=stop_state)
        mode_toggle = getattr(self, "mode_toggle", None)
        if mode_toggle is not None:
            mode_toggle.config(state=translate_state)

    def close(self):
        """关闭事件泵并使当前 run_id 失效。

        窗口关闭时必须调用，避免残留 after 回调和迟到事件污染。
        """
        if self._current_run_id is not None:
            self._retire_run(self._current_run_id)
            self._current_run_id = None
        # P0-2：关闭时清空预览行集合，避免残留状态影响下一次窗口生命周期。
        self._streaming_preview_rows.clear()
        if self._export_job is not None:
            self._export_job.cancel()
        if self._export_poll_after_id is not None:
            try:
                self.root.after_cancel(self._export_poll_after_id)
            except (tk.TclError, ValueError):
                pass
            self._export_poll_after_id = None
        self._stop_liveness_ticker()
        self._event_pump.close()

    def invalidate_session(self):
        """P0-4：文档会话切换时调用，使当前 run_id 失效。

        导入新文件、粘贴新内容或替换文档时调用此方法，
        确保旧翻译任务的迟到事件不会写入新文档的行索引。

        P0-2：同时清空未提交的流式预览行集合——预览只属于旧会话，
        新会话的 Treeview 已重置，不应继续追踪旧预览。
        """
        if self._current_run_id is not None:
            self._retire_run(self._current_run_id)
            self._current_run_id = None
            self._current_mode = None
        self._streaming_preview_rows.clear()
        self._stop_liveness_ticker()

    def cancel_for_session_replacement(self) -> bool:
        """Cancel the active run and make the controller immediately reusable.

        The worker may emit a terminal callback later, but its run ID is
        retired before the session is replaced so it cannot alter the new
        document or leave ``is_translating`` stuck.
        """
        if not self.is_translating:
            return True

        try:
            self.translator.call_if_initialized("stop")
        except Exception as exc:
            logger.warning("切换文档时停止翻译失败: %s", exc)
            self.status_updater("无法停止当前翻译，请稍后重试")
            return False

        run_id = self._current_run_id
        if run_id is not None:
            self._retire_run(run_id)
        self._current_run_id = None
        self._current_mode = None
        self.is_translating = False
        self._continue_missing_indices = []
        self._missing_translation_indices = []
        self._selected_translation_data = []
        self._revert_streaming_preview()
        self._stop_liveness_ticker()
        self._set_control_states(tk.NORMAL, tk.NORMAL, tk.DISABLED)
        self.status_updater("已停止当前翻译，正在切换文档")
        self.schedule_save(delay_ms=0)
        return True

    def _new_run_id(self, mode: str) -> str:
        """生成新 run_id，丢弃旧任务的事件。

        在主线程开始新翻译时调用。旧任务的迟到事件会被事件泵的
        run_id 检查丢弃。
        """
        run_id = uuid.uuid4().hex[:12]
        if self._current_run_id is not None:
            self._retire_run(self._current_run_id)
        self._current_run_id = run_id
        self._current_mode = mode
        return run_id

    def start_translation(self, *, context_policy: TranslationContextPolicy | None = None):
        """开始翻译（完全重构：分批翻译机制）

        ``context_policy`` 是本次运行的显式上下文模式。``None`` 表示普通模式，
        保持既有默认策略；传入 LONG 策略即超长上下文翻译。策略作为不可变快照
        传到工作线程，不通过临时修改全局配置再恢复来实现。
        """
        if not self._can_start_translation():
            return
        # UXF-001：默认只处理缺失译文，绝不静默清空已有译文。
        # 覆盖已有结果应由明确的“重新翻译”操作完成。
        source_lines, target_lines = self.get_table_data()
        if not source_lines or not any(line.strip() for line in source_lines):
            messagebox.showwarning("翻译警告", "请先输入要翻译的文本")
            return

        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return
        action = "long_context" if (context_policy and context_policy.is_long) else "full"
        if not self._confirm_preflight(action):
            return

        # 更新界面状态
        self.is_translating = True
        self._set_control_states(tk.DISABLED, tk.DISABLED, tk.NORMAL)

        pending_indices = [
            index
            for index, source_line in enumerate(source_lines)
            if source_line.strip()
            and (index >= len(target_lines) or not target_lines[index].strip())
        ]
        if not pending_indices:
            self.is_translating = False
            self._set_control_states(tk.NORMAL, tk.NORMAL, tk.DISABLED)
            messagebox.showinfo("提示", "所有非空内容均已有译文。")
            return

        # 通过缺失行索引映射写回，保留已有译文和人工校对结果。
        self._continue_start_line = 0
        self._continue_missing_indices = pending_indices

        # P0-4：在主线程生成 run_id 并通过参数传入工作线程，
        # 避免工作线程在回调时读取可变的 self._current_run_id。
        run_id = self._new_run_id("full")
        mode_label = "超长上下文翻译" if action == "long_context" else "翻译"
        self._start_liveness_ticker(f"正在{mode_label}...")

        # 在新线程中执行翻译
        source_content = "\n".join(source_lines[index] for index in pending_indices)
        self._run_context_policies[run_id] = context_policy
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(source_content, run_id),
        )
        translation_thread.daemon = True
        translation_thread.start()

    def start_long_context_translation(self) -> None:
        """主界面“超长上下文翻译”入口：翻译当前文档全部未完成行。

        复用同一 controller 与同一运行锁，因此重复点击不会启动重叠运行；
        过滤后的表格不是真相源（``get_table_data`` 读文档模型）。
        小模型模式在此处预检拦截，程序调用入口也有明确配置错误。
        """
        if not self._can_start_translation():
            return
        policy = self._resolve_long_context_policy()
        if policy is None:
            return
        self.start_translation(context_policy=policy)

    def _resolve_long_context_policy(self) -> TranslationContextPolicy | None:
        """读取用户预算并构造超长模式策略；失败时提示并返回 ``None``。"""
        if self._small_model_mode_enabled():
            messagebox.showwarning(
                "超长上下文翻译不可用",
                _SMALL_MODEL_LONG_CONTEXT_CONFLICT_MESSAGE,
                parent=self.root,
            )
            self.status_updater("超长上下文翻译需要先关闭小模型模式")
            return None
        try:
            tokens = self.config_manager.get_long_context_window_tokens()
        except Exception as exc:  # noqa: BLE001 - 配置异常需可见而非静默
            logger.exception("读取超长上下文预算失败")
            messagebox.showerror("配置错误", f"无法读取超长上下文设置：{exc}", parent=self.root)
            return None
        return TranslationContextPolicy(ContextMode.LONG, tokens)

    def _small_model_mode_enabled(self) -> bool:
        try:
            app_config = self.config_manager.get_app_config()
        except Exception:  # noqa: BLE001 - 读不到配置时不阻断普通翻译
            return False
        return bool(app_config.get(SMALL_MODEL_MODE_CONFIG_KEY, False))

    def _take_run_context_policy(self, run_id: str) -> TranslationContextPolicy | None:
        """取出并移除该 run 冻结的上下文策略（工作线程第一个动作）。

        ``getattr`` 回退让 ``__new__`` 构造的最小测试替身也能调用本方法。
        """
        with self._results_lock:
            policies = getattr(self, "_run_context_policies", None)
            if policies is None:
                return None
            return policies.pop(run_id, None)

    def retranslate_all(self):
        """Retranslate every non-empty source row after explicit confirmation.

        Existing targets remain visible until each replacement is successfully
        returned. This prevents a cancelled or failed run from erasing usable
        translations.
        """
        if not self._can_start_translation():
            return

        source_lines, target_lines = self.get_table_data()
        source_indices = [index for index, line in enumerate(source_lines) if line.strip()]
        if not source_indices:
            messagebox.showwarning("翻译警告", "请先输入要翻译的文本")
            return

        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return

        translated_count = sum(
            1
            for index in source_indices
            if index < len(target_lines) and target_lines[index].strip()
        )
        confirmed = messagebox.askyesno(
            "确认重新翻译",
            f"将重新翻译全部 {len(source_indices)} 行，并覆盖其中 {translated_count} 行现有译文。\n\n"
            "只有成功返回的新译文才会覆盖旧内容。是否继续？",
            icon="warning",
            parent=self.root,
        )
        if not confirmed or not self._confirm_preflight("retranslate"):
            return

        self.is_translating = True
        self._set_control_states(tk.DISABLED, tk.DISABLED, tk.NORMAL)
        self._continue_start_line = 0
        self._continue_missing_indices = source_indices

        run_id = self._new_run_id("retranslate")
        self._start_liveness_ticker(f"正在重新翻译全部 {len(source_indices)} 行...")
        source_content = "\n".join(source_lines[index] for index in source_indices)
        translation_thread = threading.Thread(
            target=self._translate_worker, args=(source_content, run_id)
        )
        translation_thread.daemon = True
        translation_thread.start()

    def continue_translation(self):
        """继续翻译（R2-BUG-014：只处理真正缺失的行，不覆盖已有译文）

        核心逻辑：
        1. 检查所有行，找出需要翻译的行（原文不为空但译文为空）
        2. 只提取缺失行的原文，记录其原始索引
        3. 回调通过索引映射写回，不覆盖已有译文
        """
        if not self._can_start_translation():
            return
        # 获取原文和译文
        source_lines, target_lines = self.get_table_data()

        # 检查所有行，找出需要翻译的行（原文不为空但译文为空）
        need_translation_indices = []
        for i in range(len(source_lines)):
            source_text = source_lines[i].strip() if i < len(source_lines) else ""
            target_text = target_lines[i].strip() if i < len(target_lines) else ""

            # 如果原文不为空但译文为空，则需要翻译
            if source_text and not target_text:
                need_translation_indices.append(i)

        # 如果没有需要翻译的行，说明全部翻译完成
        if not need_translation_indices:
            messagebox.showinfo("提示", "所有内容已翻译完成。")
            return

        # R2-BUG-014：只提取缺失行的原文，而非从第一个缺口到末尾的全部原文
        missing_source_lines = [source_lines[i] for i in need_translation_indices]
        remaining_content = "\n".join(missing_source_lines).strip()

        if not remaining_content:
            messagebox.showinfo("提示", "当前无可继续的原文内容，已全部翻译或原文为空。")
            return

        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return
        if not self._confirm_preflight("continue"):
            return

        # 更新界面状态（不清空译文）
        self.is_translating = True
        self._set_control_states(tk.DISABLED, tk.DISABLED, tk.NORMAL)

        # R2-BUG-014：记录缺失行原始索引，用于回调按索引写回（不再使用连续偏移）
        self._continue_missing_indices = need_translation_indices
        self._continue_start_line = 0

        # P0-4：在主线程生成 run_id 并通过参数传入工作线程
        run_id = self._new_run_id("full")
        self._start_liveness_ticker("正在继续翻译...")

        # 在新线程中执行翻译，只翻译缺失行
        translation_thread = threading.Thread(
            target=self._translate_worker, args=(remaining_content, run_id)
        )
        translation_thread.daemon = True
        translation_thread.start()

    def stop_translation(self):
        """停止翻译（修复：保留已翻译内容，不删除未翻译原文）。

        核心逻辑：
        1. 立即停止翻译并取消API请求
        2. 保留已翻译的内容和对应的原文
        3. 仅清除未翻译部分的译文（将译文框设为空）
        4. 确保实时保存机制正确更新txt译文文件和EPUB映射文件
        """
        if self.is_translating:
            # 立即停止翻译并取消API请求
            self.translator.stop()

            # ✅ 修复3：不删除未翻译的行，只清空未翻译部分的译文
            # 不做任何删除操作，保留所有原文和已翻译的译文

            # P0-2：立即回退未提交的流式预览，避免用户停止后仍看到半成品。
            # 已通过 BATCH_COMPLETED 提交的行不受影响，保留为最终译文。
            self._revert_streaming_preview()

            # 更新UI状态
            self._set_control_states(tk.DISABLED, tk.DISABLED, tk.DISABLED)
            self._stop_liveness_ticker()
            self.status_updater("正在停止翻译，已完成内容会保留")

            # 重置进度条为当前实际进度
            # PERF §7.5 D2：从文档模型读取译文状态，不反向遍历 Treeview。
            # Treeview 仅作视图，业务状态唯一真相来源为 TranslationDocument。
            if self._document:
                targets = self._document.target_lines()
                if targets:
                    total = len(targets)
                    translated = sum(1 for t in targets if t.strip())
                    actual_progress = (translated / total) * 100 if total > 0 else 0
                    self.progress_var.set(actual_progress)

            # ✅ 关键：立即触发保存（保存当前已翻译部分）
            self.schedule_save(delay_ms=0)  # 立即保存，不延迟

    def show_context_menu(self, event):
        """显示右键菜单

        新增功能：当用户右键点击表格时，显示上下文菜单
        支持多选行进行翻译
        """
        # 获取点击位置的行
        item = self.translation_table.identify_row(event.y)

        if item:
            # 如果点击的行不在选中列表中，则选中该行
            selection = self.translation_table.selection()
            if item not in selection:
                self.translation_table.selection_set(item)

            # 显示菜单
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()

    def translate_selected_rows(self):
        """翻译选中的行

        新墟功能：
        1. 获取用户选中的所有行
        2. 提取这些行的原文
        3. 单独翻译这些行
        4. 将翻译结果写回对应的译文栏
        """
        if not self._can_start_translation():
            return

        # 获取选中的行
        selection = self.translation_table.selection()
        if not selection:
            messagebox.showwarning("翻译警告", "请先选中要翻译的行")
            return

        # 检查API配置
        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return
        if not self._confirm_preflight("selected"):
            return

        # 提取选中行的原文和位置信息
        selected_data = []
        for item in selection:
            values = self.translation_table.item(item)["values"]
            if values:
                line_num = values[0]  # 行号
                source_text = values[1]  # 原文
                selected_data.append(
                    {"item": item, "line_num": line_num, "source_text": source_text}
                )

        if not selected_data:
            messagebox.showwarning("翻译警告", "选中的行没有内容")
            return

        # 更新界面状态
        self.is_translating = True
        self._set_control_states(tk.DISABLED, tk.DISABLED, tk.NORMAL)

        # 保存选中的数据（用于渲染时定位）
        self._selected_translation_data = selected_data

        # 提取原文内容
        source_texts = [item["source_text"] for item in selected_data]
        combined_source = "\n".join(source_texts)

        # P0-4：在主线程生成 run_id 并通过参数传入工作线程
        run_id = self._new_run_id("selected")
        self._start_liveness_ticker("正在翻译选中行...")

        # 在新线程中执行翻译
        translation_thread = threading.Thread(
            target=self._translate_selected_worker, args=(combined_source, run_id)
        )
        translation_thread.daemon = True
        translation_thread.start()

    def _translate_worker(self, content, run_id):
        """全文翻译工作线程。

        P0-4：``run_id`` 在主线程生成并通过参数传入，工作线程不再读取
        可变的 ``self._current_run_id``。旧任务停止后新任务启动时，
        旧线程的迟到事件仍携带旧 run_id，会被事件泵丢弃。

        上下文策略通过 **run_id → 策略** 的映射按运行读取，而不是加一个
        仅本 worker 才有的参数：三个 worker 必须共用 ``(content, run_id)``
        同一回调模式（P0-3）。策略在主线程入队时冻结，之后设置改动不影响
        在途运行。
        """
        try:
            context_policy = self._take_run_context_policy(run_id)
            if context_policy is not None and context_policy.is_long:
                self.translator.translate_long_context_mode(
                    content,
                    lambda progress, data: self._publish_progress_event(progress, data, run_id),
                    lambda result: self._publish_terminal_event(result, run_id),
                    wallet=context_policy,
                )
            else:
                self.translator.translate_fast_mode(
                    content,
                    lambda progress, data: self._publish_progress_event(progress, data, run_id),
                    lambda result: self._publish_terminal_event(result, run_id),
                )
        except Exception as exc:
            # BUG-002：在离开 except 块前绑定消息，避免 NameError
            error_message = str(exc)
            logger.exception("后台翻译失败")
            self._publish_run_failed(error_message, run_id)

    def _translate_selected_worker(self, content, run_id):
        """选中行翻译工作线程。只调用 ``mailbox.publish()``。"""
        try:
            self.translator.translate_fast_mode(
                content,
                lambda progress, data: self._publish_progress_event(progress, data, run_id),
                lambda result: self._publish_terminal_event(result, run_id),
            )
        except Exception as exc:
            error_message = str(exc)
            logger.exception("选中行翻译失败")
            self._publish_run_failed(error_message, run_id)

    # ── 适配层：translator 旧 callback API → 结构化事件 ────────
    # 工作线程调用以下方法，将 ``(progress, data)`` / ``BatchTranslationResult``
    # 转为不可变 ``TranslationProgressEvent`` 并发布到邮箱。线程安全：
    # 只读 ``self._current_run_id``、只调用 ``mailbox.publish()``，
    # 不访问任何 Tk 控件。

    def _publish_progress_event(self, progress, batch_data, run_id):
        """将旧 ``(progress, data)`` 回调转为结构化事件并发布。

        P0-4：``run_id`` 由调用方（工作线程闭包）传入，不再读取
        可变的 ``self._current_run_id``。

        - ``streaming=True`` → ``STREAM`` 事件（邮箱按批次合并最新快照）
        - ``streaming=False`` → ``BATCH_COMPLETED`` 事件（保序、不丢弃）
        """
        if run_id is None or not batch_data or self._is_retired_run(run_id):
            return

        is_streaming = bool(batch_data.get("streaming", False))
        batch_start = int(batch_data.get("batch_start", 0))
        expected_lines = int(batch_data.get("expected_lines", 0))

        if is_streaming:
            stream_lines, stream_start_line = self._get_stream_snapshot(batch_data)
            translated_lines: tuple[str, ...] = ()
            completed_lines = int(batch_data.get("completed_lines", len(stream_lines)))
            kind = TranslationEventKind.STREAM
        else:
            translated_lines = tuple(batch_data.get("translated_lines", []))
            stream_lines = ()
            stream_start_line = 0
            completed_lines = len(translated_lines)
            kind = TranslationEventKind.BATCH_COMPLETED

        event = TranslationProgressEvent(
            run_id=run_id,
            kind=kind,
            batch_start=batch_start,
            expected_lines=expected_lines,
            completed_lines=completed_lines,
            overall_progress=min(float(progress), 100.0) if progress >= 0 else 0.0,
            stream_start_line=stream_start_line,
            stream_lines=tuple(stream_lines),
            translated_lines=translated_lines,
            display_batch_start=batch_data.get("display_batch_start"),
            message=batch_data.get("message"),
        )
        self._mailbox.publish(event)

    def _publish_terminal_event(self, result: BatchTranslationResult, run_id: str):
        """将翻译结果转为终结事件并发布。

        P0-4：``run_id`` 由调用方传入，不再读取可变的 ``self._current_run_id``。
        结果暂存到 ``_pending_results``，主线程通过 ``run_id`` 取回。
        """
        if run_id is None:
            return
        with self._results_lock:
            if run_id in getattr(self, "_retired_run_ids", ()):
                return
            self._pending_results[run_id] = result

        if result.is_cancelled:
            kind = TranslationEventKind.RUN_CANCELLED
            overall = 0.0
        elif result.is_failed:
            kind = TranslationEventKind.RUN_FAILED
            overall = 0.0
        else:
            kind = TranslationEventKind.RUN_COMPLETED
            overall = 100.0

        event = TranslationProgressEvent(
            run_id=run_id,
            kind=kind,
            batch_start=0,
            expected_lines=0,
            completed_lines=0,
            overall_progress=overall,
            message=result.error_message,
        )
        self._mailbox.publish(event)

    def _publish_run_failed(self, error_message: str, run_id: str):
        """工作线程异常时发布 RUN_FAILED 事件。

        P0-4：``run_id`` 由调用方传入，不再读取可变的 ``self._current_run_id``。
        """
        if run_id is None:
            return
        with self._results_lock:
            if run_id in getattr(self, "_retired_run_ids", ()):
                return
        event = TranslationProgressEvent(
            run_id=run_id,
            kind=TranslationEventKind.RUN_FAILED,
            batch_start=0,
            expected_lines=0,
            completed_lines=0,
            overall_progress=0.0,
            message=error_message,
        )
        self._mailbox.publish(event)

    def _render_translation_progress(self, progress, batch_data):
        """渲染翻译进度（在主线程执行）。

        PERF §7.4/§7.5 步骤4：模型优先更新顺序。
        1. 校验索引并构建 {row_index: value} 更新
        2. 更新 TranslationDocument（唯一真相来源）
        3. TranslationTableAdapter 批量写入变化的行
        4. 标记自动保存 dirty
        """
        if progress >= 0:
            self.progress_var.set(progress)
            # PERF-002：移除 update_idletasks()，让 Tk 事件循环自然刷新

        if not batch_data or not isinstance(batch_data, dict):
            return

        batch_start = batch_data.get("batch_start", 0)
        is_streaming = batch_data.get("streaming", False)

        # PERF §7.7：翻译热路径不调用 get_children()，
        # 改用文档模型的行数做边界校验。
        row_count = (
            self._document.row_count
            if self._document
            else len(self.translation_table.get_children())
        )
        if row_count == 0:
            return

        # R2-BUG-014：续翻模式通过缺失行索引映射回原位置，不覆盖已有译文
        continue_missing_indices = getattr(self, "_continue_missing_indices", None)
        continue_offset = getattr(self, "_continue_start_line", 0)

        def resolve_row_index(rel_idx):
            """将批次内相对索引映射为表格绝对行号"""
            if continue_missing_indices:
                if rel_idx < len(continue_missing_indices):
                    return continue_missing_indices[rel_idx]
                return None
            return continue_offset + rel_idx

        if is_streaming:
            # PERF-001：新协议只携带新增完整行。保留旧字段回退，
            # 使 UI 和核心层可以独立升级。
            expected_lines = batch_data.get("expected_lines", 1)
            stream_lines, stream_start_line = self._get_stream_snapshot(batch_data)

            # 实时显示：不超过预期行数
            updates: dict[int, str] = {}
            last_row_index = -1
            for i, line in enumerate(stream_lines):
                relative_index = stream_start_line + i
                if relative_index >= expected_lines:
                    break
                row_index = resolve_row_index(batch_start + relative_index)
                if row_index is not None and row_index < row_count:
                    updates[row_index] = line.strip()
                    last_row_index = row_index

            # P0-2：流式预览只写入 Treeview，不写入 TranslationDocument。
            # 业务模型只在 BATCH_COMPLETED 时提交，避免预览值与最终值相同时
            # update_target() 返回未变化而导致 dirty/save 被跳过。
            # 取消/失败终态由 _revert_streaming_preview 回退未提交预览。
            if self._table_adapter:
                self._table_adapter.apply_streaming_preview(updates)
                # PERF-002：只滚动到最后一行，不逐行滚动
                if (
                    last_row_index in updates
                    and updates.get(last_row_index) is not None
                    and self._should_follow_stream(batch_data)
                ):
                    self._table_adapter.see(last_row_index)
            else:
                # 回退路径：无适配器时只滚动定位
                if last_row_index in updates and self._should_follow_stream(batch_data):
                    item = self.translation_table.get_children()[last_row_index]
                    self.translation_table.see(item)
            # 记录未提交预览的行，供终态取消/失败时回退。
            # 排除人工编辑保护过的行（apply_streaming_preview 不会真正覆盖它们，
            # 但 Treeview 已显示预览值；为简化语义，仍追踪这些行，回退时按文档值还原）。
            self._streaming_preview_rows.update(updates.keys())
        else:
            # 批次完成模式：写入最终结果
            translated_lines = batch_data.get("translated_lines", [])

            # R2-BUG-009：只覆盖已确认成功的译文（非空），
            # 取消时空批次不覆盖已有译文。
            updates = {}
            for i, translated_line in enumerate(translated_lines):
                row_index = resolve_row_index(batch_start + i)
                if row_index is not None and row_index < row_count:
                    new_val = translated_line.strip() if translated_line else ""
                    if new_val:
                        updates[row_index] = new_val

            # PERF §7.4：先更新模型，再批量更新视图。
            # P0-4：只把 update_target 返回 True 的行交给适配器渲染。
            accepted_updates = self._apply_to_document(updates)
            if self._table_adapter:
                # skip_empty=True 默认行为：空值不覆盖已有译文
                self._table_adapter.apply_target_updates(accepted_updates)
            # 只有模型确实发生变化时才标记 dirty。旧 generation 被人工编辑保护
            # 拒绝时，不应产生无意义的保存任务或关闭提示。
            if accepted_updates:
                self.schedule_save()
            # P0-2：已提交到文档的行不再是未提交预览，从追踪集合移除。
            self._streaming_preview_rows.difference_update(accepted_updates.keys())

    # ── 渲染分派器：事件泵 → 模式对应的渲染/状态处理 ──────────
    # 以下方法只在 Tk 主线程被事件泵调用。

    def _render_event(self, event: TranslationProgressEvent) -> None:
        """事件泵回调：在主线程渲染单个事件。

        丢弃旧任务的迟到事件（``run_id`` 不匹配），按事件类型路由。
        """
        # 丢弃旧任务的迟到事件
        if event.run_id != self._current_run_id:
            return

        if event.kind is TranslationEventKind.STREAM:
            self._note_stream_activity()
            self._dispatch_stream_event(event)
        elif event.kind is TranslationEventKind.BATCH_COMPLETED:
            self._note_stream_activity()
            self._dispatch_batch_completed_event(event)
        elif event.kind is TranslationEventKind.RUN_COMPLETED:
            self._stop_liveness_ticker()
            self._dispatch_terminal_event(event, is_terminal_run=True)
        elif event.kind is TranslationEventKind.RUN_FAILED:
            self._stop_liveness_ticker()
            self._dispatch_terminal_event(event, is_terminal_run=False, is_failed=True)
        elif event.kind is TranslationEventKind.RUN_CANCELLED:
            self._stop_liveness_ticker()
            self._dispatch_terminal_event(event, is_terminal_run=True, is_cancelled=True)

    def _apply_to_document(self, updates: dict[int, str]) -> dict[int, str]:
        """P0-4：写入 document，只返回实际被接受的行。

        ``TranslationDocument.update_target`` 在行被人工编辑标记保护时
        返回 ``False``。这些行已被用户校对，机器结果（旧 generation）
        不得覆盖视图，因此表格适配器只应渲染被接受的子集。

        无 document 时所有更新都视为可渲染，保留旧行为。
        """
        if not updates:
            return {}
        accepted: dict[int, str] = {}
        retranslated_indices: set[int] = set()
        if self._document:
            for row_index, value in updates.items():
                if getattr(self, "_current_mode", None) == "retranslate":
                    # 用户已明确同意覆盖；只在拿到非空新结果时解除人工保护。
                    self._document.clear_manual_flag(row_index)
                    retranslated_indices.add(row_index)
                if self._document.update_target(row_index, value):
                    accepted[row_index] = value
        else:
            accepted = dict(updates)
            if getattr(self, "_current_mode", None) == "retranslate":
                retranslated_indices.update(updates)
        if retranslated_indices:
            self._on_retranslated(retranslated_indices)
        return accepted

    def _revert_streaming_preview(self) -> None:
        """P0-2：回退所有未提交的流式预览到文档值。

        在 ``RUN_CANCELLED`` / ``RUN_FAILED`` / ``RUN_COMPLETED`` 终态调用。
        将 ``_streaming_preview_rows`` 中记录的行回退为文档中的真实译文，
        避免半成品预览长期滞留 Treeview 而文档 dirty/save 语义不感知。

        - 若无 document 或无 table_adapter：仅清空追踪集合，Treeview 状态
          由调用方在后续操作中自然覆盖。
        - 若某行索引超出文档范围（极端边界）：跳过该行，不抛异常。
        """
        if not self._streaming_preview_rows:
            return
        preview_rows = self._streaming_preview_rows
        self._streaming_preview_rows = set()
        if not self._document or not self._table_adapter:
            return
        revert: dict[int, str] = {}
        doc = self._document
        for row_index in preview_rows:
            if 0 <= row_index < doc.row_count:
                revert[row_index] = doc.row(row_index).target
        if revert:
            # apply_streaming_preview 不跳过空值，可正确回退到空文档值
            self._table_adapter.apply_streaming_preview(revert)

    def _dispatch_stream_event(self, event: TranslationProgressEvent) -> None:
        """流式事件：转回 ``(progress, data)`` 调用模式对应的渲染方法。"""
        batch_data = {
            "batch_start": event.batch_start,
            "streaming": True,
            "expected_lines": event.expected_lines,
            "stream_lines": list(event.stream_lines),
            "stream_start_line": event.stream_start_line,
            "display_batch_start": event.display_batch_start,
            "completed_lines": event.completed_lines,
        }
        self._route_progress_render(event.overall_progress, batch_data)

    def _dispatch_batch_completed_event(self, event: TranslationProgressEvent) -> None:
        """批次完成事件：转回 ``(progress, data)`` 调用模式对应的渲染方法。"""
        batch_data = {
            "batch_start": event.batch_start,
            "streaming": False,
            "translated_lines": list(event.translated_lines),
            "display_batch_start": event.display_batch_start,
        }
        self._route_progress_render(event.overall_progress, batch_data)

    def _route_progress_render(self, progress: float, batch_data: dict) -> None:
        """根据当前模式路由到对应的渲染方法。"""
        mode = self._current_mode
        if mode == "selected":
            self._render_selected_progress(progress, batch_data)
        elif mode == "missing":
            self._render_missing_progress(progress, batch_data)
        else:
            # 默认/全文模式
            self._render_translation_progress(progress, batch_data)

    def _dispatch_terminal_event(
        self,
        event: TranslationProgressEvent,
        *,
        is_terminal_run: bool = False,
        is_failed: bool = False,
        is_cancelled: bool = False,
    ) -> None:
        """终结事件：取回结果并调用模式对应的处理方法。"""
        try:
            result = self._take_pending_result(event.run_id)
            if result is None:
                # worker 异常时只发布了 RUN_FAILED 事件，没有结构化结果
                if is_failed:
                    self._handle_translation_failed(event.message or "未知错误")
                return

            mode = self._current_mode
            self._on_run_terminal(result, mode or "full")
            if mode == "selected":
                self._handle_selected_translation_complete(result)
            elif mode == "missing":
                self._handle_missing_translation_complete(result)
            else:
                self._handle_full_translation_complete(result)
        finally:
            # A RUN terminal is the final reducer event for this run.  Retire
            # every residual stream/pending value before a later run starts.
            if event.run_id == self._current_run_id:
                self._retire_run(event.run_id)
                self._current_run_id = None
                self._current_mode = None

    def _can_start_translation(self) -> bool:
        """Reject duplicate commands while a run is active or cancelling."""
        if not self.is_translating:
            return True
        self.status_updater("翻译任务正在运行或停止中，请等待当前任务结束")
        return False

    def _confirm_preflight(self, action: str) -> bool:
        callback = getattr(self, "_preflight_callback", None)
        if callback is None:
            return True
        try:
            return bool(callback(action))
        except Exception as exc:
            logger.exception("翻译预检失败")
            messagebox.showerror("翻译预检失败", f"无法完成翻译预检：{exc}")
            return False

    def _take_pending_result(self, run_id: str) -> BatchTranslationResult | None:
        """从暂存区取回并移除指定 ``run_id`` 的结果。"""
        with self._results_lock:
            return self._pending_results.pop(run_id, None)

    def _retire_run(self, run_id: str) -> None:
        """Discard queued events and pending data for a run that cannot render."""
        self._mailbox.discard_run(run_id)
        lock = getattr(self, "_results_lock", None)
        if lock is None:
            self._pending_results = getattr(self, "_pending_results", {})
            self._pending_errors = getattr(self, "_pending_errors", {})
            self._pending_results.pop(run_id, None)
            self._pending_errors.pop(run_id, None)
            policies = getattr(self, "_run_context_policies", None)
            if policies is not None:
                policies.pop(run_id, None)
            retired_run_ids = getattr(self, "_retired_run_ids", None)
            if retired_run_ids is None:
                retired_run_ids = deque(maxlen=64)
                self._retired_run_ids = retired_run_ids
            retired_run_ids.append(run_id)
            return
        with lock:
            self._pending_results.pop(run_id, None)
            self._pending_errors.pop(run_id, None)
            policies = getattr(self, "_run_context_policies", None)
            if policies is not None:
                policies.pop(run_id, None)
            retired_run_ids = getattr(self, "_retired_run_ids", None)
            if retired_run_ids is None:
                retired_run_ids = deque(maxlen=64)
                self._retired_run_ids = retired_run_ids
            retired_run_ids.append(run_id)

    def _is_retired_run(self, run_id: str) -> bool:
        with self._results_lock:
            return run_id in getattr(self, "_retired_run_ids", ())

    def _start_liveness_ticker(self, base_message: str) -> None:
        """Start (or restart) the status-bar wait ticker for a new run."""
        self._stop_liveness_ticker()
        self._liveness_base_message = base_message
        self._last_stream_activity_at = time.monotonic()
        self.status_updater(base_message)
        try:
            self._liveness_after_id = self.root.after(_LIVENESS_TICK_MS, self._tick_liveness)
        except (tk.TclError, AttributeError):
            self._liveness_after_id = None

    def _note_stream_activity(self) -> None:
        """Reset the elapsed wait when a stream/batch event arrives."""
        self._last_stream_activity_at = time.monotonic()

    def _tick_liveness(self) -> None:
        """Refresh the status bar with elapsed wait, or stop after the run ends."""
        self._liveness_after_id = None
        if not self.is_translating:
            return
        started = self._last_stream_activity_at
        elapsed = int(time.monotonic() - started) if started is not None else 0
        base = self._liveness_base_message or "正在翻译..."
        if elapsed >= _LIVENESS_SILENCE_SECONDS:
            self.status_updater(f"{base}（等待模型响应 {elapsed}s）")
        else:
            self.status_updater(base)
        try:
            self._liveness_after_id = self.root.after(_LIVENESS_TICK_MS, self._tick_liveness)
        except (tk.TclError, AttributeError):
            self._liveness_after_id = None

    def _stop_liveness_ticker(self) -> None:
        """Cancel the pending after callback. Idempotent.

        Controllers constructed with ``__new__`` in tests may not have the
        ticker attributes at all; treat that as already stopped.
        """
        token = getattr(self, "_liveness_after_id", None)
        self._liveness_after_id = None
        if token is None:
            return
        try:
            self.root.after_cancel(token)
        except (tk.TclError, ValueError, AttributeError):
            pass
        except Exception:  # noqa: BLE001
            pass

    def _handle_full_translation_complete(self, result: BatchTranslationResult):
        """全文翻译完成：在主线程更新 UI（原 ``_on_translation_complete.update_ui``）。"""
        is_retranslation = getattr(self, "_current_mode", None) == "retranslate"
        self.is_translating = False
        self._stop_liveness_ticker()
        self._set_control_states(tk.NORMAL, tk.NORMAL, tk.DISABLED)
        # 复位续写标记
        self._continuing_mode = False
        self._continuing_first_insert = False
        # R2-BUG-014：清理续翻缺失行索引
        self._continue_missing_indices = []
        # P0-2：回退未提交的流式预览到文档值，避免半成品滞留 Treeview。
        # 已通过 BATCH_COMPLETED 提交的行不受影响。
        self._revert_streaming_preview()

        if result.is_cancelled:
            # 用户取消：不显示完成，不触发查漏
            self.status_updater("翻译已停止")
            return

        if result.is_failed:
            # 全部失败：显示错误，不触发查漏
            self._handle_translation_failed(result.error_message or "未知错误")
            return

        # 成功或部分成功
        self.progress_var.set(100)

        if result.status == TranslationStatus.PARTIAL:
            failed_count = len(result.failed_indices)
            operation_name = "重新翻译" if is_retranslation else "翻译"
            self.status_updater(f"{operation_name}部分完成（{failed_count} 行失败）")
            # 记录失败索引用于查漏
            self._last_missing_failed_indices = list(result.failed_indices)
            actionable = self._classify_translation_error(result.error_message)
            if actionable is not None and not actionable.retryable:
                self._show_actionable_error(actionable)
                return
            if is_retranslation:
                messagebox.showwarning(
                    "重新翻译部分完成",
                    f"{failed_count} 行未能生成新译文，已有译文已保留。\n"
                    "仍为空的行将自动尝试补译。",
                )
            else:
                messagebox.showwarning(
                    "翻译部分完成", f"部分内容翻译失败（{failed_count} 行）。\n将尝试补译失败行。"
                )
        else:
            self.status_updater("重新翻译完成" if is_retranslation else "翻译完成")
            self._last_missing_failed_indices = []

        if is_retranslation:
            source_lines, target_lines = self.get_table_data()
            has_missing = any(
                source.strip() and (index >= len(target_lines) or not target_lines[index].strip())
                for index, source in enumerate(source_lines)
            )
            if not has_missing:
                self._missing_check_rounds = 0
                self._last_missing_failed_indices = []
                if result.status != TranslationStatus.PARTIAL:
                    messagebox.showinfo("重新翻译完成", "所有非空内容已重新翻译完成。")
                return

        # BUG-004：启动翻译查漏机制（带次数上限）
        self._missing_check_rounds = 0
        self.root.after(500, self._start_missing_translation_check)

    def _handle_translation_failed(self, error_msg: str):
        """Restore the UI and present a safe, actionable error summary."""
        self.is_translating = False
        self._stop_liveness_ticker()
        self._set_control_states(tk.NORMAL, tk.NORMAL, tk.DISABLED)
        # P0-2：worker 异常时回退未提交的流式预览
        self._revert_streaming_preview()
        actionable = self._classify_translation_error(error_msg) or classify_error(
            RuntimeError("未知翻译错误")
        )
        self.status_updater(f"翻译失败：{actionable.safe_message}")
        self._show_actionable_error(actionable)

    @staticmethod
    def _classify_translation_error(error_message: str | None) -> ActionableError | None:
        """Classify an opaque worker message without exposing it directly to Tk."""
        if not error_message:
            return None
        return classify_error(RuntimeError(error_message))

    @staticmethod
    def _format_actionable_error(actionable: ActionableError) -> str:
        return (
            f"{actionable.safe_message}\n\n"
            f"建议操作：{actionable.recommended_action}\n"
            f"诊断编号：{actionable.correlation_id}"
        )

    def _show_actionable_error(self, actionable: ActionableError) -> None:
        """Log diagnostic detail separately and show only the safe UI summary."""
        error = RuntimeError(actionable.safe_message)
        log_classified_error(error, actionable, context={"surface": "main_translation"})
        messagebox.showerror("翻译错误", self._format_actionable_error(actionable))

    def _schedule_missing_check_retry(self, actionable: ActionableError) -> None:
        """Retry only classified transient errors, with bounded jittered backoff."""
        if not actionable.retryable:
            self.status_updater(f"翻译查漏已停止：{actionable.safe_message}")
            self._show_actionable_error(actionable)
            self._missing_check_rounds = 0
            return
        attempts_completed = max(0, self._missing_check_rounds - 1)
        if attempts_completed >= self._missing_retry_policy.max_attempts:
            self.status_updater("已达到自动补译上限，剩余空行可手动重试")
            return
        delay_ms = int(self._missing_retry_policy.delay_for(attempts_completed) * 1000)
        self.status_updater(f"翻译查漏暂时失败，将在约 {delay_ms / 1000:.1f} 秒后重试")
        self.root.after(delay_ms, self._start_missing_translation_check)

    def _render_selected_progress(self, progress, batch_data):
        """渲染选中行翻译进度（在主线程执行）。

        PERF §7.4/§7.5 步骤4：模型优先更新顺序。
        selected_data 中的相对索引先映射为绝对行号，
        再更新文档模型和表格适配器。
        """
        if progress >= 0:
            self.progress_var.set(progress)
            # PERF-002：移除 update_idletasks()

        if not batch_data or not isinstance(batch_data, dict):
            return

        selected_data = getattr(self, "_selected_translation_data", [])
        if not selected_data:
            return

        batch_start = batch_data.get("batch_start", 0)
        is_streaming = batch_data.get("streaming", False)

        if is_streaming:
            expected_lines = batch_data.get("expected_lines", 1)
            stream_lines, stream_start_line = self._get_stream_snapshot(batch_data)

            updates: dict[int, str] = {}  # 绝对行号 → 译文
            last_item = None
            for i, line in enumerate(stream_lines):
                relative_index = stream_start_line + i
                if relative_index >= expected_lines:
                    break
                sel_row = batch_start + relative_index
                if sel_row < len(selected_data):
                    item = selected_data[sel_row]["item"]
                    abs_row = (
                        self._table_adapter.find_row_index(item) if self._table_adapter else -1
                    )
                    if abs_row >= 0:
                        updates[abs_row] = line.strip()
                    last_item = item
            # P0-2：流式预览只写入 Treeview，不写入 TranslationDocument。
            # 业务模型只在 BATCH_COMPLETED 时提交，避免预览值与最终值相同时
            # update_target() 返回未变化而导致 dirty/save 被跳过。
            if self._table_adapter:
                self._table_adapter.apply_streaming_preview(updates)
            # PERF-002：只滚动到最后一行（仅当至少有一行被接受时）
            if updates and last_item is not None and self._should_follow_stream(batch_data):
                self.translation_table.see(last_item)
            # P0-2：记录未提交预览的行
            self._streaming_preview_rows.update(updates.keys())
        else:
            translated_lines = batch_data.get("translated_lines", [])

            updates = {}
            for i, translated_line in enumerate(translated_lines):
                sel_row = batch_start + i
                if sel_row < len(selected_data):
                    item = selected_data[sel_row]["item"]
                    abs_row = (
                        self._table_adapter.find_row_index(item) if self._table_adapter else -1
                    )
                    if abs_row >= 0:
                        new_val = translated_line.strip() if translated_line else ""
                        if new_val:
                            updates[abs_row] = new_val
            # PERF §7.4：先更新模型，再批量更新视图。
            # P0-4：只把 update_target 返回 True 的行交给适配器渲染。
            accepted_updates = self._apply_to_document(updates)
            if self._table_adapter:
                self._table_adapter.apply_target_updates(accepted_updates)
            if accepted_updates:
                self.schedule_save()
            # P0-2：已提交到文档的行不再是未提交预览
            self._streaming_preview_rows.difference_update(accepted_updates.keys())

    def _handle_selected_translation_complete(self, result: BatchTranslationResult):
        """选中行翻译完成：在主线程更新 UI（原 ``_on_selected_translation_complete``）。"""
        # 恢复界面状态
        self.is_translating = False
        self._stop_liveness_ticker()
        self._set_control_states(tk.NORMAL, tk.NORMAL, tk.DISABLED)
        # P0-2：回退未提交的流式预览到文档值
        self._revert_streaming_preview()

        # 获取翻译的行数
        selected_count = len(getattr(self, "_selected_translation_data", []))

        # 清理临时数据
        if hasattr(self, "_selected_translation_data"):
            delattr(self, "_selected_translation_data")

            # 立即保存
        if result.is_success:
            self.schedule_save(delay_ms=0)

        if result.is_cancelled:
            self.status_updater("已停止选中行翻译")
            return

        if result.is_failed:
            self.status_updater("选中行翻译失败")
            error_detail = result.error_message or "未知错误"
            messagebox.showerror("翻译错误", f"选中行翻译失败：{error_detail}")
            return

        # 设置进度为100%
        self.progress_var.set(100)

        if result.status == TranslationStatus.PARTIAL:
            failed_count = len(result.failed_indices)
            self.status_updater(f"选中行翻译部分完成（{failed_count} 行失败）")
            messagebox.showwarning(
                "翻译部分完成", f"选中 {selected_count} 行，其中 {failed_count} 行翻译失败。"
            )
        else:
            self.status_updater(f"选中的 {selected_count} 行翻译完成")
            messagebox.showinfo("翻译完成", f"已完成 {selected_count} 行的翻译")

    def _start_missing_translation_check(self):
        """启动翻译查漏机制（BUG-004：增加补译次数上限）

        核心逻辑：
        1. 检查所有行，找出原文不为空但译文为空的行
        2. 如果存在空行，自动启动翻译
        3. 一次最多翻译20个空行
        4. 翻译完成后继续检查，直到所有行都翻译完成或达到补译上限
        """
        # 如果正在翻译，跳过
        if self.is_translating:
            return

        # BUG-004：达到补译上限后停止自动循环，允许用户手工重试
        if self._missing_check_rounds >= MAX_MISSING_CHECK_ROUNDS:
            self.status_updater(
                f"已达到自动补译上限（{MAX_MISSING_CHECK_ROUNDS} 次），剩余空行可手动重试"
            )
            messagebox.showwarning(
                "翻译查漏",
                f"已达到自动补译上限（{MAX_MISSING_CHECK_ROUNDS} 次）。\n"
                "仍有部分行未翻译，可点击「继续翻译」手动重试。",
            )
            self._missing_check_rounds = 0
            self._last_missing_failed_indices = []
            return

        # 获取原文和译文
        source_lines, target_lines = self.get_table_data()

        # 查找空行（原文不为空但译文为空）
        empty_indices = []
        for i in range(len(source_lines)):
            if source_lines[i].strip() and (i >= len(target_lines) or not target_lines[i].strip()):
                empty_indices.append(i)

        # 如果没有空行，正常结束
        if not empty_indices:
            self.status_updater("翻译完成，无需查漏")
            messagebox.showinfo("翻译完成", "所有内容已翻译完成！")
            self._missing_check_rounds = 0
            self._last_missing_failed_indices = []
            return

        # 有空行，开始翻译查漏
        self._missing_check_rounds += 1
        total_empty = len(empty_indices)
        self.status_updater(
            f"正在进行翻译查漏（第 {self._missing_check_rounds}/{MAX_MISSING_CHECK_ROUNDS} 次）："
            f"发现 {total_empty} 个空行"
        )

        # 一次最多翻译20个空行
        batch_size = 20
        current_batch_indices = empty_indices[:batch_size]

        # 提取这些空行的原文
        empty_source_lines = [source_lines[i] for i in current_batch_indices]

        # 记录空行位置
        self._missing_translation_indices = current_batch_indices

        # 标记为翻译查漏模式
        self._is_missing_check = True

        # 开始翻译
        self.is_translating = True
        self._set_control_states(tk.DISABLED, tk.DISABLED, tk.NORMAL)

        # PERF：在主线程生成 run_id 并丢弃旧任务事件。
        # P0-3：捕获 run_id 并传入工作线程，避免回调签名不匹配而崩溃。
        run_id = self._new_run_id("missing")
        self._start_liveness_ticker(
            f"正在进行翻译查漏（第 {self._missing_check_rounds}/{MAX_MISSING_CHECK_ROUNDS} 次）"
        )

        # 在新线程中执行翻译
        combined_source = "\n".join(empty_source_lines)
        translation_thread = threading.Thread(
            target=self._translate_missing_worker, args=(combined_source, run_id)
        )
        translation_thread.daemon = True
        translation_thread.start()

    def _translate_missing_worker(self, content, run_id):
        """翻译查漏工作线程。只调用 ``mailbox.publish()``。

        P0-3：``run_id`` 由主线程在 ``_start_missing_translation_check``
        中生成并通过参数传入，工作线程使用闭包显式绑定，不再读取
        可变的 ``self._current_run_id``。
        与全文、选中行 worker 保持同一模式，避免签名再次漂移。
        """
        try:
            self.translator.translate_fast_mode(
                content,
                lambda progress, data: self._publish_progress_event(progress, data, run_id),
                lambda result: self._publish_terminal_event(result, run_id),
            )
        except Exception as exc:
            error_message = str(exc)
            logger.exception("翻译查漏失败")
            self._publish_run_failed(error_message, run_id)

    def _render_missing_progress(self, progress, batch_data):
        """渲染翻译查漏进度（在主线程执行）。

        PERF §7.4/§7.5 步骤4：模型优先更新顺序。
        ``missing_indices`` 已存储绝对行号，直接用于模型和适配器。
        """
        if progress >= 0:
            self.progress_var.set(progress)
            # PERF-002：移除 update_idletasks()

        if not batch_data or not isinstance(batch_data, dict):
            return

        missing_indices = getattr(self, "_missing_translation_indices", [])
        if not missing_indices:
            return

        batch_start = batch_data.get("batch_start", 0)
        is_streaming = batch_data.get("streaming", False)
        # PERF §7.7：用文档模型行数做边界校验，不调用 get_children()
        row_count = (
            self._document.row_count
            if self._document
            else len(self.translation_table.get_children())
        )

        if is_streaming:
            expected_lines = batch_data.get("expected_lines", 1)
            streaming_lines, stream_start_line = self._get_stream_snapshot(batch_data)

            updates: dict[int, str] = {}
            last_row_index = -1
            for i, line in enumerate(streaming_lines):
                stream_index = stream_start_line + i
                if stream_index >= expected_lines:
                    break
                relative_index = batch_start + stream_index
                if relative_index < len(missing_indices):
                    row_index = missing_indices[relative_index]
                    if row_index < row_count:
                        updates[row_index] = line.strip()
                        last_row_index = row_index
            # P0-2：流式预览只写入 Treeview，不写入 TranslationDocument。
            # 业务模型只在 BATCH_COMPLETED 时提交，避免预览值与最终值相同时
            # update_target() 返回未变化而导致 dirty/save 被跳过。
            if self._table_adapter:
                self._table_adapter.apply_streaming_preview(updates)
                if last_row_index in updates and self._should_follow_stream(batch_data):
                    self._table_adapter.see(last_row_index)
            elif last_row_index in updates and self._should_follow_stream(batch_data):
                # 回退路径
                item = self.translation_table.get_children()[last_row_index]
                self.translation_table.see(item)
            # P0-2：记录未提交预览的行
            self._streaming_preview_rows.update(updates.keys())
        else:
            translated_lines = batch_data.get("translated_lines", [])

            updates = {}
            for i, translated_line in enumerate(translated_lines):
                relative_index = batch_start + i
                if relative_index < len(missing_indices):
                    row_index = missing_indices[relative_index]
                    if row_index < row_count:
                        new_val = translated_line.strip() if translated_line else ""
                        if new_val:
                            updates[row_index] = new_val
            # PERF §7.4：先更新模型，再批量更新视图。
            # P0-4：只把 update_target 返回 True 的行交给适配器渲染。
            accepted_updates = self._apply_to_document(updates)
            if self._table_adapter:
                self._table_adapter.apply_target_updates(accepted_updates)
            if accepted_updates:
                self.schedule_save()
            # P0-2：已提交到文档的行不再是未提交预览
            self._streaming_preview_rows.difference_update(accepted_updates.keys())

    @staticmethod
    def _should_follow_stream(batch_data):
        """仅允许最靠前的未完成并发批次控制表格滚动。"""
        batch_start = batch_data.get("batch_start", 0)
        display_batch_start = batch_data.get("display_batch_start")
        return display_batch_start is None or batch_start == display_batch_start

    @staticmethod
    def _get_stream_snapshot(batch_data):
        """获取最新流式快照及其在当前批次中的起始行。"""
        preview_lines = batch_data.get("preview_lines")
        if preview_lines is not None:
            return preview_lines, 0

        stream_lines = batch_data.get("stream_lines")
        if stream_lines is not None:
            return stream_lines, batch_data.get("stream_start_line", 0)

        return batch_data.get("current_text", "").split("\n"), 0

    def _handle_missing_translation_complete(self, result: BatchTranslationResult):
        """翻译查漏完成：在主线程更新 UI 并控制查漏循环。

        原 ``_on_missing_translation_complete.update_ui`` 逻辑，去掉
        ``root.after(0, ...)`` 包装（事件泵已在主线程调用）。
        """
        self.is_translating = False
        self._stop_liveness_ticker()
        self._set_control_states(tk.NORMAL, tk.NORMAL, tk.DISABLED)
        # P0-2：回退未提交的流式预览到文档值
        self._revert_streaming_preview()

        # 清理临时数据
        if hasattr(self, "_missing_translation_indices"):
            delattr(self, "_missing_translation_indices")
        if hasattr(self, "_is_missing_check"):
            delattr(self, "_is_missing_check")

        # 立即保存已接受的结果；取消/失败且没有模型变化时保持 clean。
        if result.is_success:
            self.schedule_save(delay_ms=0)

        # 取消：不继续查漏
        if result.is_cancelled:
            self.status_updater("翻译查漏已停止")
            self._missing_check_rounds = 0
            return

        # 失败：达到上限或继续受限重试
        if result.is_failed:
            error_detail = result.error_message or "未知错误"
            actionable = self._classify_translation_error(error_detail)
            if actionable is None:
                actionable = classify_error(RuntimeError("未知翻译查漏错误"))
            self._schedule_missing_check_retry(actionable)
            return

        # 成功或部分成功：继续检查是否还有空行
        self.root.after(1000, self._start_missing_translation_check)

    def _save_translation_sync(self) -> bool:
        """P0-3：保存译文到当前 session 的 target_path。

        修复说明：
        - 若当前 session 已有 target_path（TXT/EPUB 导入派生）：直接写入，不弹 Save As。
        - 若无 target_path（剪贴板会话）：弹出 Save As，成功后把新路径写回 session，
          下次 Ctrl+S 会直接写入该路径，不再每次询问。
        - 成功后清除 MainWindow 的 dirty 状态，更新保存状态标签。
        - EPUB 映射同步失败时记日志并提示，但译文文件已保存。
        - BUG-006：使用原子写入，失败时显示对话框（不显示成功）。
        """
        try:
            # 从表格获取译文
            _, target_lines = self.get_table_data()
            translated_content = "\n".join(target_lines)

            if not translated_content.strip():
                messagebox.showwarning("保存警告", "没有可保存的译文")
                return False

            current_target = self._get_target_path()
            if current_target is not None:
                # P0-3：当前 session 已有目标，直接覆盖写入。
                file_path = str(current_target)
                used_save_as = False
            else:
                # P0-3：剪贴板会话无 target_path，走 Save As 并写回 session。
                file_path = filedialog.asksaveasfilename(
                    title="保存译文",
                    defaultextension=".txt",
                    filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
                )
                if not file_path:
                    return False  # 用户取消
                used_save_as = True

            self._backup_existing_output(Path(file_path))
            # BUG-006：write_file 现在使用原子写入，失败时抛出异常
            self.file_handler.write_file(file_path, translated_content)
            self.status_updater(f"译文已保存: {Path(file_path).name}")

            # P0-3：Save As 产生的新目标写回 session，后续 Ctrl+S 直接覆盖。
            if used_save_as:
                self._update_target_path(Path(file_path))

            # 若存在EPUB映射，则同步更新映射键值对
            current_mapping_dir = self.get_mapping_dir()
            mapping_failed = False
            if current_mapping_dir:
                try:
                    self.epub_processor.save_translations(str(current_mapping_dir), target_lines)
                except Exception as e:
                    # BUG-006：EPUB映射同步失败需可见，但不影响已保存的txt
                    mapping_failed = True
                    logger.error("EPUB映射同步失败: %s", e)
                    messagebox.showwarning(
                        "保存警告", f"译文文件已保存，但EPUB映射同步失败：\n{str(e)}"
                    )

            # P0-3：只有译文文件和（如有的话）EPUB 映射都成功后，才清除 dirty。
            if not mapping_failed:
                self._on_save_success()
                return True

            return False

        except Exception as e:
            messagebox.showerror("保存错误", f"保存译文失败: {str(e)}")
            return False

    def save_translation(self, *, synchronous: bool = False) -> bool:
        """Save the current translation without blocking normal Tk commands.

        Session replacement and close flows pass ``synchronous=True`` because
        they must know whether the durable write completed before proceeding.
        Interactive saves capture a stable table snapshot in Tk and hand the
        expensive join/write work to ``TextExportJob``.
        """
        if synchronous:
            return self._save_translation_sync()
        if self._export_is_running():
            messagebox.showinfo("保存进行中", "已有导出或保存任务正在运行。")
            return False

        try:
            _, target_lines = self.get_table_data()
            target_snapshot = tuple(target_lines)
            if not any(line.strip() for line in target_snapshot):
                messagebox.showwarning("保存警告", "没有可保存的译文")
                return False

            current_target = self._get_target_path()
            used_save_as = current_target is None
            if current_target is None:
                file_path = filedialog.asksaveasfilename(
                    title="保存译文",
                    defaultextension=".txt",
                    filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
                )
                if not file_path:
                    return False
                output_path = Path(file_path)
            else:
                output_path = Path(current_target)

            mapping_dir = self.get_mapping_dir()

            def save_mapping() -> None:
                if mapping_dir:
                    self.epub_processor.save_translations(str(mapping_dir), list(target_snapshot))

            def on_success() -> None:
                if used_save_as:
                    self._update_target_path(output_path)
                self._on_save_success()

            self._start_text_export(
                kind="译文保存",
                output_path=output_path,
                build_content=lambda: "\n".join(target_snapshot),
                save_mapping=save_mapping if mapping_dir else None,
                on_success=on_success,
                backup_existing=True,
            )
            return True
        except Exception as exc:
            logger.exception("启动译文保存失败")
            messagebox.showerror("保存错误", f"无法开始保存译文: {exc}")
            return False

    def _export_comparison_sync(self):
        """导出对照文件"""
        try:
            source_lines, target_lines = self.get_table_data()
            source_content = "\n".join(source_lines)
            target_content = "\n".join(target_lines)

            if not source_content or not target_content:
                messagebox.showwarning("导出警告", "原文或译文为空")
                return

            file_path = filedialog.asksaveasfilename(
                title="导出对照文件",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            )

            if file_path:
                comparison_content = self.file_handler.create_comparison_file(
                    source_content, target_content
                )
                self.file_handler.write_file(file_path, comparison_content)
                self.status_updater(f"对照文件已导出: {Path(file_path).name}")

        except Exception as e:
            messagebox.showerror("导出错误", f"导出对照文件失败: {str(e)}")

    def export_comparison(self) -> None:
        """Export a comparison file from a Tk-owned snapshot in the background."""
        if self._export_is_running():
            messagebox.showinfo("导出进行中", "已有导出或保存任务正在运行。")
            return
        try:
            source_lines, target_lines = self.get_table_data()
            source_snapshot = tuple(source_lines)
            target_snapshot = tuple(target_lines)
            if not any(source_snapshot) or not any(target_snapshot):
                messagebox.showwarning("导出警告", "原文或译文为空")
                return

            file_path = filedialog.asksaveasfilename(
                title="导出对照文件",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            )
            if not file_path:
                return

            self._start_text_export(
                kind="对照导出",
                output_path=Path(file_path),
                build_content=lambda: self.file_handler.create_comparison_file(
                    "\n".join(source_snapshot), "\n".join(target_snapshot)
                ),
            )
        except Exception as exc:
            logger.exception("启动对照导出失败")
            messagebox.showerror("导出错误", f"无法开始导出对照文件: {exc}")

    def _export_is_running(self) -> bool:
        return self._export_job is not None and self._export_job.is_running

    def _backup_existing_output(self, output_path: Path) -> None:
        """在覆盖译文前把旧版本写入用户选择的备份目录。

        首次导入 TXT 时应用会预创建一个空的 ``*_译文.txt``，该空文件不
        应产生无意义的备份。备份失败时抛出异常并阻止覆盖，确保用户不会
        在“已启用备份”的前提下静默丢失最后一个可恢复版本。
        """
        backup_dir = self._backup_dir
        if backup_dir is None:
            return
        path = Path(output_path)
        if not path.is_file() or path.stat().st_size == 0:
            return
        try:
            backup_path = self.file_handler.backup_file(str(path), backup_dir=backup_dir)
        except Exception as exc:  # noqa: BLE001 - 统一成可见的保存失败
            raise RuntimeError(f"创建译文备份失败: {exc}") from exc
        if not backup_path:
            raise RuntimeError("创建译文备份失败，已取消覆盖原译文")
        logger.info("已创建译文备份: %s", backup_path)

    def _start_text_export(
        self,
        *,
        kind: str,
        output_path: Path,
        build_content: Callable[[], str],
        save_mapping: Callable[[], None] | None = None,
        on_success: Callable[[], None] | None = None,
        backup_existing: bool = False,
    ) -> None:
        job = TextExportJob(
            output_path=output_path,
            build_content=build_content,
            write_content=lambda path, content: self.file_handler.write_file(str(path), content),
            save_mapping=save_mapping,
            backup_existing=self._backup_existing_output if backup_existing else None,
        )
        self._export_job = job
        self._export_kind = kind
        self._export_success_callback = on_success
        job.start()
        self.status_updater(f"{kind}已开始，可继续使用界面")
        self._poll_export_job()

    def export_epub_file(self):
        """在后台将当前译文安全导出为 EPUB。"""
        active_export = self._export_job
        if active_export is not None and active_export.is_running:
            if messagebox.askyesno("导出进行中", "当前保存或导出尚未完成。是否取消？"):
                active_export.cancel()
                self.status_updater("正在取消导出…")
            return
        try:
            current_mapping_dir = self.get_mapping_dir()
            if not current_mapping_dir:
                messagebox.showwarning("导出警告", "当前会话并非EPUB映射，无法导出EPUB")
                return

            _, target_lines = self.get_table_data()
            current_source_path = self.get_source_path()
            default_filename = build_default_epub_filename(current_source_path)

            out_path = filedialog.asksaveasfilename(
                title="导出为EPUB文件",
                defaultextension=".epub",
                initialfile=default_filename,
                filetypes=[("EPUB电子书", "*.epub"), ("所有文件", "*.*")],
            )
            if not out_path:
                return

            image_map = (
                load_image_translation_result(current_mapping_dir) if current_mapping_dir else None
            )
            image_text_map = (
                load_image_text_translations(current_mapping_dir) if current_mapping_dir else None
            )
            mapping_dir = str(current_mapping_dir)
            target_snapshot = tuple(target_lines)

            def save_mapping() -> None:
                self.epub_processor.save_translations(mapping_dir, list(target_snapshot))

            def export_to(temporary_path: Path) -> Path | str:
                return self.epub_processor.export_epub(
                    mapping_dir,
                    str(temporary_path),
                    image_map,
                    image_text_map,
                )

            self._export_job = ExportJob(
                output_path=Path(out_path),
                save_mapping=save_mapping,
                export_to=export_to,
                backup_existing=self._backup_existing_output,
            )
            self._export_kind = "EPUB 导出"
            self._export_success_callback = None
            self._export_job.start()
            self.status_updater("EPUB 导出已开始，可继续使用界面")
            self._poll_export_job()
        except Exception as e:
            messagebox.showerror("导出错误", f"导出EPUB操作失败: {str(e)}")

    def _poll_export_job(self) -> None:
        job = self._export_job
        if job is None:
            return
        terminal = False
        for event in job.drain_events():
            if isinstance(event, ExportProgress):
                self.status_updater(event.message)
                continue
            terminal = True
            kind = self._export_kind or "导出"
            if event.succeeded:
                output_path = event.output_path
                if output_path is None:
                    event = ExportResult(None, error_message="导出任务未返回输出路径")
                else:
                    callback = self._export_success_callback
                    if callback is not None:
                        try:
                            callback()
                        except Exception:
                            logger.exception("处理%s成功回调失败", kind)
                    self.status_updater(f"{kind}已完成: {output_path.name}")
                    messagebox.showinfo("导出成功", f"{kind}已完成: {output_path.name}")
                    self._export_job = None
                    self._export_kind = None
                    self._export_success_callback = None
                    continue
            elif event.cancelled:
                self.status_updater(f"{kind}已取消，原目标文件未修改")
            else:
                self.status_updater(f"{kind}失败")
                messagebox.showerror("导出错误", f"{kind}失败: {event.error_message or '未知错误'}")
            self._export_job = None
            self._export_kind = None
            self._export_success_callback = None
        if terminal:
            self._export_poll_after_id = None
            return
        self._export_poll_after_id = self.root.after(50, self._poll_export_job)
