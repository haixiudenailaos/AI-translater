#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译控制器模块
从MainWindow中提取的翻译流程控制逻辑
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
from pathlib import Path
from typing import Callable, Tuple, List, Optional, Any

from ..core.translation_result import BatchTranslationResult, TranslationStatus
from ..utils.logger import get_logger

logger = get_logger(__name__)

# BUG-004：自动查漏单行/单批最多补译次数
MAX_MISSING_CHECK_ROUNDS = 2


class TkUpdateCoalescer:
    """PERF-002：合并 Tk UI 更新，固定节流间隔。

    流式回调可能每秒产生数十到数百个事件，直接调度到主线程会淹没 Tk 事件循环。
    本类将中间快照覆盖保留，只按固定间隔刷新最新快照；主控制器使用 40ms，
    让当前行能连续显示，同时避免每个 token 都触发 Tk 重绘。
    批次完成等最终事件应调用 cancel_pending 后立即渲染，不经过节流。
    """

    def __init__(self, root: tk.Tk, interval_ms: int = 75):
        self.root = root
        self.interval_ms = interval_ms
        self._pending: dict[Any, tuple] = {}  # coalesce_key -> (render_fn, args)
        self._after_id: Optional[str] = None

    def submit(
        self, render_fn: Callable, *args: Any, coalesce_key: Any = None
    ) -> None:
        """提交流式快照；同一批次只保留最新快照。"""
        self._pending[coalesce_key] = (render_fn, args)
        if self._after_id is None:
            self._after_id = self.root.after(self.interval_ms, self._flush)

    def cancel_pending(self, coalesce_key: Any = None) -> None:
        """取消指定批次；不传批次时取消全部待处理快照。"""
        if coalesce_key is None:
            self._pending.clear()
        else:
            self._pending.pop(coalesce_key, None)

        if not self._pending and self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _flush(self) -> None:
        self._after_id = None
        pending = list(self._pending.values())
        self._pending.clear()
        for render_fn, args in pending:
            render_fn(*args)


class TranslationController:
    """翻译控制器：管理翻译流程、进度更新、状态管理"""

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
        get_mapping_dir: Callable
    ):
        self.root = root
        self.config_manager = config_manager
        self.translator = translator
        self.file_handler = file_handler
        self.epub_processor = epub_processor
        self.translation_table = translation_table
        self.progress_var = progress_var
        self.progress_bar = progress_bar
        self.translate_btn = translate_btn
        self.continue_btn = continue_btn
        self.stop_btn = stop_btn
        self.status_updater = status_updater
        self.get_table_data = get_table_data
        self.schedule_save = schedule_save
        self.open_settings = open_settings
        self.get_source_path = get_source_path
        self.get_mapping_dir = get_mapping_dir

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

        # Continuation mode flags
        self._continuing_mode = False
        self._continuing_first_insert = False
        self._translation_buffer = []

        # 右键菜单
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="翻译选中行", command=self.translate_selected_rows)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="取消", command=lambda: self.context_menu.unpost())

        # PERF-002：流式 UI 更新节流器（同一时刻只有一种翻译在运行，可共享）
        self._stream_coalescer = TkUpdateCoalescer(self.root, interval_ms=40)

    def start_translation(self):
        """开始翻译（完全重构：分批翻译机制）"""
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

        # 更新界面状态
        self.is_translating = True
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        pending_indices = [
            index
            for index, source_line in enumerate(source_lines)
            if source_line.strip()
            and (index >= len(target_lines) or not target_lines[index].strip())
        ]
        if not pending_indices:
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            messagebox.showinfo("提示", "所有非空内容均已有译文。")
            return

        # 通过缺失行索引映射写回，保留已有译文和人工校对结果。
        self._continue_start_line = 0
        self._continue_missing_indices = pending_indices

        # 在新线程中执行翻译
        source_content = "\n".join(source_lines[index] for index in pending_indices)
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(source_content,)
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
        remaining_content = '\n'.join(missing_source_lines).strip()

        if not remaining_content:
            messagebox.showinfo("提示", "当前无可继续的原文内容，已全部翻译或原文为空。")
            return

        if not self.config_manager.is_api_configured():
            messagebox.showwarning("配置警告", "请先配置API设置")
            self.open_settings()
            return

        # 更新界面状态（不清空译文）
        self.is_translating = True
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        # R2-BUG-014：记录缺失行原始索引，用于回调按索引写回（不再使用连续偏移）
        self._continue_missing_indices = need_translation_indices
        self._continue_start_line = 0

        # 在新线程中执行翻译，只翻译缺失行
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(remaining_content,)
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
            self.is_translating = False

            # ✅ 修复3：不删除未翻译的行，只清空未翻译部分的译文
            # 不做任何删除操作，保留所有原文和已翻译的译文

            # 更新UI状态
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.status_updater("翻译已停止，已翻译内容已保留")

            # 重置进度条为当前实际进度
            items = self.translation_table.get_children()
            if items:
                # 计算实际翻译进度
                total = len(items)
                translated = 0
                for item in items:
                    values = self.translation_table.item(item)['values']
                    if len(values) > 2 and values[2].strip():  # 译文不为空
                        translated += 1
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

        # 提取选中行的原文和位置信息
        selected_data = []
        for item in selection:
            values = self.translation_table.item(item)['values']
            if values:
                line_num = values[0]  # 行号
                source_text = values[1]  # 原文
                selected_data.append({
                    'item': item,
                    'line_num': line_num,
                    'source_text': source_text
                })

        if not selected_data:
            messagebox.showwarning("翻译警告", "选中的行没有内容")
            return

        # 更新界面状态
        self.is_translating = True
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        # 保存选中的数据（用于回调中定位）
        self._selected_translation_data = selected_data

        # 提取原文内容
        source_texts = [item['source_text'] for item in selected_data]
        combined_source = '\n'.join(source_texts)

        # 在新线程中执行翻译
        translation_thread = threading.Thread(
            target=self._translate_selected_worker,
            args=(combined_source,)
        )
        translation_thread.daemon = True
        translation_thread.start()

    def _translate_worker(self, content):
        """翻译工作线程"""
        try:
            # R2-BUG-026：后台线程不得直接操作 Tk 控件，调度到主线程
            self.root.after(0, lambda: self.status_updater("正在翻译..."))

            # BUG-008：两种模式无真实行为差异，统一使用快速模式
            self.translator.translate_fast_mode(
                content,
                self._on_translation_progress,
                self._on_translation_complete
            )

        except Exception as exc:
            # BUG-002：在离开 except 块前绑定消息，避免 NameError
            error_message = str(exc)
            logger.exception("后台翻译失败")
            self.root.after(0, lambda message=error_message: self._on_translation_error(message))

    def _translate_selected_worker(self, content):
        """选中行翻译工作线程"""
        try:
            self.translator.translate_fast_mode(
                content,
                self._on_selected_translation_progress,
                self._on_selected_translation_complete
            )
        except Exception as exc:
            # BUG-002：在离开 except 块前绑定消息，避免 NameError
            error_message = str(exc)
            logger.exception("选中行翻译失败")
            self.root.after(0, lambda message=error_message: self._on_translation_error(message))

    def _on_translation_progress(self, progress, batch_data):
        """翻译进度回调（PERF-002：流式更新节流）

        参数：
            progress: 进度百分比
            batch_data: {
                'batch_start': 起始行号（0-based）,
                'streaming': True/False,  # 是否为流式输出
                'current_text': '当前流式文本',  # streaming=True时有效
                'preview_lines': ['已完成行', '当前未完成行'],  # 优先使用的完整快照
                'expected_lines': 预期行数,  # streaming=True时有效
                'translated_lines': 译文列表  # streaming=False时有效
            }
        """
        is_streaming = batch_data.get('streaming', False) if batch_data else False
        batch_start = batch_data.get('batch_start', 0) if batch_data else 0
        if is_streaming:
            # 并发批次分别合并，避免后到达的批次覆盖其他批次的快照。
            self._stream_coalescer.submit(
                self._render_translation_progress, progress, batch_data,
                coalesce_key=batch_start,
            )
        else:
            # 只丢弃本批次的中间快照，不影响其他并发批次。
            self._stream_coalescer.cancel_pending(coalesce_key=batch_start)
            self.root.after(0, lambda: self._render_translation_progress(progress, batch_data))

    def _render_translation_progress(self, progress, batch_data):
        """渲染翻译进度（在主线程执行）"""
        if progress >= 0:
            self.progress_var.set(progress)
            # PERF-002：移除 update_idletasks()，让 Tk 事件循环自然刷新

        if not batch_data or not isinstance(batch_data, dict):
            return

        batch_start = batch_data.get('batch_start', 0)
        is_streaming = batch_data.get('streaming', False)

        items = self.translation_table.get_children()
        if not items:
            return

        # R2-BUG-014：续翻模式通过缺失行索引映射回原位置，不覆盖已有译文
        continue_missing_indices = getattr(self, '_continue_missing_indices', None)
        continue_offset = getattr(self, '_continue_start_line', 0)

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
            expected_lines = batch_data.get('expected_lines', 1)
            stream_lines, stream_start_line = self._get_stream_snapshot(batch_data)

            # 实时显示：不超过预期行数
            last_item = None
            for i, line in enumerate(stream_lines):
                relative_index = stream_start_line + i
                if relative_index >= expected_lines:
                    break
                row_index = resolve_row_index(batch_start + relative_index)
                if row_index is not None and row_index < len(items):
                    item = items[row_index]
                    values = list(self.translation_table.item(item)['values'])
                    values[2] = line.strip()
                    self.translation_table.item(item, values=values)
                    last_item = item
            # PERF-002：只滚动到最后一行，不逐行滚动
            if last_item is not None and self._should_follow_stream(batch_data):
                self.translation_table.see(last_item)
        else:
            # 批次完成模式：写入最终结果
            translated_lines = batch_data.get('translated_lines', [])

            for i, translated_line in enumerate(translated_lines):
                row_index = resolve_row_index(batch_start + i)
                if row_index is not None and row_index < len(items):
                    item = items[row_index]
                    values = list(self.translation_table.item(item)['values'])
                    # R2-BUG-009：只覆盖已确认成功的译文（非空），取消时空批次不覆盖已有译文
                    new_val = translated_line.strip() if translated_line else ""
                    if new_val:
                        values[2] = new_val
                        self.translation_table.item(item, values=values)

            # 触发保存
            self.schedule_save()

    def _on_translation_complete(self, result: BatchTranslationResult):
        """翻译完成回调（BUG-004：基于结构化结果区分状态）"""
        def update_ui():
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            # 复位续写标记
            self._continuing_mode = False
            self._continuing_first_insert = False
            # R2-BUG-014：清理续翻缺失行索引
            self._continue_missing_indices = []

            if result.is_cancelled:
                # 用户取消：不显示完成，不触发查漏
                self.status_updater("翻译已停止")
                return

            if result.is_failed:
                # 全部失败：显示错误，不触发查漏
                self.status_updater("翻译失败")
                error_detail = result.error_message or "未知错误"
                messagebox.showerror("翻译错误", f"翻译失败：{error_detail}")
                return

            # 成功或部分成功
            self.progress_var.set(100)

            if result.status == TranslationStatus.PARTIAL:
                failed_count = len(result.failed_indices)
                self.status_updater(f"翻译部分完成（{failed_count} 行失败）")
                # 记录失败索引用于查漏
                self._last_missing_failed_indices = list(result.failed_indices)
                messagebox.showwarning(
                    "翻译部分完成",
                    f"部分内容翻译失败（{failed_count} 行）。\n将尝试补译失败行。"
                )
            else:
                self.status_updater("翻译完成")
                self._last_missing_failed_indices = []

            # BUG-004：启动翻译查漏机制（带次数上限）
            self._missing_check_rounds = 0
            self.root.after(500, self._start_missing_translation_check)

        self.root.after(0, update_ui)

    def _on_translation_error(self, error_msg):
        """翻译错误回调"""
        self.is_translating = False
        self.translate_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_updater("翻译失败")
        messagebox.showerror("翻译错误", f"翻译过程中出现错误: {error_msg}")

    def _on_selected_translation_progress(self, progress, batch_data):
        """选中行翻译进度回调（PERF-002：流式更新节流）"""
        is_streaming = batch_data.get('streaming', False) if batch_data else False
        batch_start = batch_data.get('batch_start', 0) if batch_data else 0
        if is_streaming:
            self._stream_coalescer.submit(
                self._render_selected_progress, progress, batch_data,
                coalesce_key=batch_start,
            )
        else:
            self._stream_coalescer.cancel_pending(coalesce_key=batch_start)
            self.root.after(0, lambda: self._render_selected_progress(progress, batch_data))

    def _render_selected_progress(self, progress, batch_data):
        """渲染选中行翻译进度（在主线程执行）"""
        if progress >= 0:
            self.progress_var.set(progress)
            # PERF-002：移除 update_idletasks()

        if not batch_data or not isinstance(batch_data, dict):
            return

        selected_data = getattr(self, '_selected_translation_data', [])
        if not selected_data:
            return

        batch_start = batch_data.get('batch_start', 0)
        is_streaming = batch_data.get('streaming', False)

        if is_streaming:
            expected_lines = batch_data.get('expected_lines', 1)
            stream_lines, stream_start_line = self._get_stream_snapshot(batch_data)

            last_item = None
            for i, line in enumerate(stream_lines):
                relative_index = stream_start_line + i
                if relative_index >= expected_lines:
                    break
                row_index = batch_start + relative_index
                if row_index < len(selected_data):
                    item = selected_data[row_index]['item']
                    values = list(self.translation_table.item(item)['values'])
                    values[2] = line.strip()
                    self.translation_table.item(item, values=values)
                    last_item = item
            # PERF-002：只滚动到最后一行
            if last_item is not None and self._should_follow_stream(batch_data):
                self.translation_table.see(last_item)
        else:
            translated_lines = batch_data.get('translated_lines', [])

            for i, translated_line in enumerate(translated_lines):
                row_index = batch_start + i
                if row_index < len(selected_data):
                    item = selected_data[row_index]['item']
                    values = list(self.translation_table.item(item)['values'])
                    new_val = translated_line.strip() if translated_line else ""
                    if new_val:
                        values[2] = new_val
                        self.translation_table.item(item, values=values)

            self.schedule_save()

    def _on_selected_translation_complete(self, result: BatchTranslationResult):
        """选中行翻译完成回调（BUG-004：基于结构化结果区分状态）"""
        def complete_ui():
            # 恢复界面状态
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)

            # 获取翻译的行数
            selected_count = len(getattr(self, '_selected_translation_data', []))

            # 清理临时数据
            if hasattr(self, '_selected_translation_data'):
                delattr(self, '_selected_translation_data')

            # 立即保存
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
                    "翻译部分完成",
                    f"选中 {selected_count} 行，其中 {failed_count} 行翻译失败。"
                )
            else:
                self.status_updater(f"选中的 {selected_count} 行翻译完成")
                messagebox.showinfo("翻译完成", f"已完成 {selected_count} 行的翻译")

        self.root.after(0, complete_ui)

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
            self.status_updater(f"已达到自动补译上限（{MAX_MISSING_CHECK_ROUNDS} 次），剩余空行可手动重试")
            messagebox.showwarning(
                "翻译查漏",
                f"已达到自动补译上限（{MAX_MISSING_CHECK_ROUNDS} 次）。\n"
                "仍有部分行未翻译，可点击「继续翻译」手动重试。"
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
        self.translate_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        # 在新线程中执行翻译
        combined_source = '\n'.join(empty_source_lines)
        translation_thread = threading.Thread(
            target=self._translate_missing_worker,
            args=(combined_source,)
        )
        translation_thread.daemon = True
        translation_thread.start()

    def _translate_missing_worker(self, content):
        """翻译查漏工作线程"""
        try:
            self.translator.translate_fast_mode(
                content,
                self._on_missing_translation_progress,
                self._on_missing_translation_complete
            )
        except Exception as exc:
            # BUG-002：在离开 except 块前绑定消息，避免 NameError
            error_message = str(exc)
            logger.exception("翻译查漏失败")
            self.root.after(0, lambda message=error_message: self._on_translation_error(message))

    def _on_missing_translation_progress(self, progress, batch_data):
        """翻译查漏进度回调（PERF-002：流式更新节流）"""
        is_streaming = batch_data.get('streaming', False) if batch_data else False
        batch_start = batch_data.get('batch_start', 0) if batch_data else 0
        if is_streaming:
            self._stream_coalescer.submit(
                self._render_missing_progress, progress, batch_data,
                coalesce_key=batch_start,
            )
        else:
            self._stream_coalescer.cancel_pending(coalesce_key=batch_start)
            self.root.after(0, lambda: self._render_missing_progress(progress, batch_data))

    def _render_missing_progress(self, progress, batch_data):
        """渲染翻译查漏进度（在主线程执行）"""
        if progress >= 0:
            self.progress_var.set(progress)
            # PERF-002：移除 update_idletasks()

        if not batch_data or not isinstance(batch_data, dict):
            return

        missing_indices = getattr(self, '_missing_translation_indices', [])
        if not missing_indices:
            return

        batch_start = batch_data.get('batch_start', 0)
        is_streaming = batch_data.get('streaming', False)
        items = self.translation_table.get_children()

        if is_streaming:
            expected_lines = batch_data.get('expected_lines', 1)
            streaming_lines, stream_start_line = self._get_stream_snapshot(batch_data)

            last_item = None
            for i, line in enumerate(streaming_lines):
                stream_index = stream_start_line + i
                if stream_index >= expected_lines:
                    break
                relative_index = batch_start + stream_index
                if relative_index < len(missing_indices):
                    row_index = missing_indices[relative_index]
                    if row_index < len(items):
                        item = items[row_index]
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = line.strip()
                        self.translation_table.item(item, values=values)
                        last_item = item
            # PERF-002：只滚动到最后一行
            if last_item is not None and self._should_follow_stream(batch_data):
                self.translation_table.see(last_item)
        else:
            translated_lines = batch_data.get('translated_lines', [])

            for i, translated_line in enumerate(translated_lines):
                relative_index = batch_start + i
                if relative_index < len(missing_indices):
                    row_index = missing_indices[relative_index]
                    if row_index < len(items):
                        item = items[row_index]
                        values = list(self.translation_table.item(item)['values'])
                        new_val = translated_line.strip() if translated_line else ""
                        if new_val:
                            values[2] = new_val
                            self.translation_table.item(item, values=values)

            self.schedule_save()

    @staticmethod
    def _should_follow_stream(batch_data):
        """仅允许最靠前的未完成并发批次控制表格滚动。"""
        batch_start = batch_data.get('batch_start', 0)
        display_batch_start = batch_data.get('display_batch_start')
        return display_batch_start is None or batch_start == display_batch_start

    @staticmethod
    def _get_stream_snapshot(batch_data):
        """获取最新流式快照及其在当前批次中的起始行。"""
        preview_lines = batch_data.get('preview_lines')
        if preview_lines is not None:
            return preview_lines, 0

        stream_lines = batch_data.get('stream_lines')
        if stream_lines is not None:
            return stream_lines, batch_data.get('stream_start_line', 0)

        return batch_data.get('current_text', '').split('\n'), 0

    def _on_missing_translation_complete(self, result: BatchTranslationResult):
        """翻译查漏完成回调（BUG-004：基于结果状态控制循环）"""
        def update_ui():
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)

            # 清理临时数据
            if hasattr(self, '_missing_translation_indices'):
                delattr(self, '_missing_translation_indices')
            if hasattr(self, '_is_missing_check'):
                delattr(self, '_is_missing_check')

            # 立即保存
            self.schedule_save(delay_ms=0)

            # 取消：不继续查漏
            if result.is_cancelled:
                self.status_updater("翻译查漏已停止")
                self._missing_check_rounds = 0
                return

            # 失败：达到上限或继续受限重试
            if result.is_failed:
                error_detail = result.error_message or "未知错误"
                self.status_updater(f"翻译查漏失败：{error_detail}")
                # 失败也消耗一次补译机会，由 _start_missing_translation_check 判定上限
                self.root.after(1000, self._start_missing_translation_check)
                return

            # 成功或部分成功：继续检查是否还有空行
            self.root.after(1000, self._start_missing_translation_check)

        self.root.after(0, update_ui)

    def save_translation(self):
        """保存译文。

        修复说明：确保译文与原文按行号严格对齐。
        BUG-006：使用原子写入，失败时显示对话框（不显示成功）。
        """
        try:
            # 从表格获取译文
            _, target_lines = self.get_table_data()
            translated_content = "\n".join(target_lines)

            if not translated_content.strip():
                messagebox.showwarning("保存警告", "没有可保存的译文")
                return

            file_path = filedialog.asksaveasfilename(
                title="保存译文",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )

            if file_path:
                # BUG-006：write_file 现在使用原子写入，失败时抛出异常
                self.file_handler.write_file(file_path, translated_content)
                self.status_updater(f"译文已保存: {Path(file_path).name}")

                # 若存在EPUB映射，则同步更新映射键值对
                current_mapping_dir = self.get_mapping_dir()
                if current_mapping_dir:
                    try:
                        self.epub_processor.save_translations(
                            str(current_mapping_dir),
                            target_lines
                        )
                    except Exception as e:
                        # BUG-006：EPUB映射同步失败需可见，但不影响已保存的txt
                        logger.error("EPUB映射同步失败: %s", e)
                        messagebox.showwarning(
                            "保存警告",
                            f"译文文件已保存，但EPUB映射同步失败：\n{str(e)}"
                        )

        except Exception as e:
            messagebox.showerror("保存错误", f"保存译文失败: {str(e)}")

    def export_comparison(self):
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
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )

            if file_path:
                comparison_content = self.file_handler.create_comparison_file(
                    source_content, target_content
                )
                self.file_handler.write_file(file_path, comparison_content)
                self.status_updater(f"对照文件已导出: {Path(file_path).name}")

        except Exception as e:
            messagebox.showerror("导出错误", f"导出对照文件失败: {str(e)}")

    def export_epub_file(self):
        """基于mapping将译文写回并导出为EPUB文件（新增：自动命名为"原文_译文"）"""
        try:
            current_mapping_dir = self.get_mapping_dir()
            if not current_mapping_dir:
                messagebox.showwarning("导出警告", "当前会话并非EPUB映射，无法导出EPUB")
                return

            # 先同步一次映射（使用当前表格内容）
            _, target_lines = self.get_table_data()

            # R2-BUG-019：映射保存失败必须中止导出，或由用户明确确认使用旧映射
            using_stale_mapping = False
            try:
                self.epub_processor.save_translations(
                    str(current_mapping_dir),
                    target_lines
                )
            except Exception as e:
                logger.error("导出前EPUB映射同步失败: %s", e)
                confirmed = messagebox.askyesno(
                    "映射保存失败",
                    f"导出前映射同步失败：\n{str(e)}\n\n"
                    "继续导出将使用旧映射中的译文，可能与当前表格内容不一致。\n"
                    "是否仍要继续导出？",
                )
                if not confirmed:
                    self.status_updater("EPUB导出已取消（映射保存失败）")
                    return
                using_stale_mapping = True

            # ✅ 新增：自动生成默认文件名为"原文_译文"
            default_filename = ""
            current_source_path = self.get_source_path()
            if current_source_path:
                source_stem = current_source_path.stem
                default_filename = f"{source_stem}_译文.epub"

            out_path = filedialog.asksaveasfilename(
                title="导出为EPUB文件",
                defaultextension=".epub",
                initialfile=default_filename,
                filetypes=[("EPUB电子书", "*.epub"), ("所有文件", "*.*")]
            )
            if not out_path:
                return

            # 加载插图翻译结果
            image_map = None
            if current_mapping_dir:
                result_file = current_mapping_dir / "image_translation_result.json"
                if result_file.exists():
                    try:
                        import json
                        with open(result_file, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                        # R2-BUG-018：兼容新旧格式
                        # 新格式: {"result_map": {...}, "run_at": ..., "result_count": ...}
                        # 旧格式: {original_path: new_filename}
                        if isinstance(raw, dict) and "result_map" in raw:
                            image_map = raw["result_map"]
                        else:
                            image_map = raw
                    except Exception:
                        pass

            # 加载图片文字翻译结果
            image_text_map = None
            if current_mapping_dir:
                text_trans_file = current_mapping_dir / "image_text_translations.json"
                if text_trans_file.exists():
                    try:
                        import json
                        with open(text_trans_file, "r", encoding="utf-8") as f:
                            image_text_map = json.load(f)
                    except Exception:
                        pass

            try:
                result_path = self.epub_processor.export_epub(
                    str(current_mapping_dir), out_path, image_map, image_text_map
                )
                self.status_updater(f"EPUB已导出: {Path(result_path).name}")
                # R2-BUG-019：使用旧映射导出时在提示中明确说明
                if using_stale_mapping:
                    messagebox.showwarning(
                        "导出完成（使用旧映射）",
                        f"已导出EPUB文件: {Path(result_path).name}\n\n"
                        "警告：映射保存失败，导出使用的是旧映射中的译文，\n"
                        "可能与当前表格内容不一致。",
                    )
                else:
                    messagebox.showinfo("导出成功", f"已导出EPUB文件: {Path(result_path).name}")
            except Exception as e:
                messagebox.showerror("导出错误", f"EPUB导出失败: {str(e)}")
        except Exception as e:
            messagebox.showerror("导出错误", f"导出EPUB操作失败: {str(e)}")
