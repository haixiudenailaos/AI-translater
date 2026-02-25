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
from typing import Callable, Tuple, List


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
        translation_mode: tk.StringVar,
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
        self.translation_mode = translation_mode
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
        self._selected_translation_data = []
        self._missing_translation_indices = []
        self._is_missing_check = False

        # Continuation mode flags
        self._continuing_mode = False
        self._continuing_first_insert = False
        self._translation_buffer = []

        # 右键菜单
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="翻译选中行", command=self.translate_selected_rows)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="取消", command=lambda: self.context_menu.unpost())

    def start_translation(self):
        """开始翻译（完全重构：分批翻译机制）"""
        # 从表格获取原文
        source_lines, _ = self.get_table_data()
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

        # ✅ 重置续翻起始行（开始翻译时从0开始）
        self._continue_start_line = 0

        # 清空译文列
        for item in self.translation_table.get_children():
            values = list(self.translation_table.item(item)['values'])
            values[2] = ""  # 清空译文
            self.translation_table.item(item, values=values)

        # 在新线程中执行翻译
        source_content = "\n".join(source_lines)
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(source_content, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()

    def continue_translation(self):
        """继续翻译（修复：智能检查空译文行，确保完整翻译）

        核心逻辑：
        1. 检查所有行，找出需要翻译的行（原文不为空但译文为空）
        2. 如果存在需要翻译的行，则进入翻译流程
        3. 只有当所有原文行都有对应的非空译文时，才提示翻译完成
        4. 不清除任何已有的译文，保持已翻译内容不变
        """
        # 获取原文和译文
        source_lines, target_lines = self.get_table_data()

        # ✅ 新逻辑：检查所有行，找出需要翻译的行
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

        # ✅ 找到第一个需要翻译的行作为起始位置
        start_idx = need_translation_indices[0]

        # 取剩余原文（从第一个需要翻译的行开始）
        remaining_lines = source_lines[start_idx:] if start_idx < len(source_lines) else []
        remaining_content = '\n'.join(remaining_lines).strip()

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

        # 记录续译起始行（用于回调中计算绝对位置）
        self._continue_start_line = start_idx

        # 在新线程中执行翻译，对剩余内容进行
        translation_thread = threading.Thread(
            target=self._translate_worker,
            args=(remaining_content, self.translation_mode.get())
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
            args=(combined_source, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()

    def _translate_worker(self, content, mode):
        """翻译工作线程"""
        try:
            self.status_updater("正在翻译...")

            if mode == "逐行模式":
                self.translator.translate_line_by_line(
                    content,
                    self._on_translation_progress,
                    self._on_translation_complete
                )
            else:
                self.translator.translate_fast_mode(
                    content,
                    self._on_translation_progress,
                    self._on_translation_complete
                )

        except Exception as e:
            self.root.after(0, lambda: self._on_translation_error(str(e)))

    def _translate_selected_worker(self, content, mode):
        """选中行翻译工作线程"""
        try:
            self.translator.translate_fast_mode(
                content,
                self._on_selected_translation_progress,
                self._on_selected_translation_complete
            )
        except Exception as e:
            self.root.after(0, lambda: self._on_translation_error(str(e)))

    def _on_translation_progress(self, progress, batch_data):
        """翻译进度回调（修复：确保进度条持续可见，避免多余空行）

        参数：
            progress: 进度百分比
            batch_data: {
                'batch_start': 起始行号（0-based）,
                'streaming': True/False,  # 是否为流式输出
                'current_text': '当前流式文本',  # streaming=True时有效
                'expected_lines': 预期行数,  # streaming=True时有效
                'translated_lines': 译文列表  # streaming=False时有效
            }

        核心逻辑：
        1. 如果streaming=True，实时显示流式翻译结果
        2. 如果streaming=False，将批次翻译完成的结果写入表格
        3. 不依赖换行符拆分，只按行号对应
        """
        def update_ui():
            # ✅ 修复1：确保进度条始终更新并可见
            if progress >= 0:
                self.progress_var.set(progress)
                # 强制刷新进度条显示
                self.progress_bar.update_idletasks()

            if not batch_data or not isinstance(batch_data, dict):
                return

            batch_start = batch_data.get('batch_start', 0)
            is_streaming = batch_data.get('streaming', False)

            # 获取所有表格项
            items = self.translation_table.get_children()
            if not items:
                return

            # 计算绝对位置：如果是续翻，需要加上续翻起始偏移
            continue_offset = getattr(self, '_continue_start_line', 0)
            absolute_start = continue_offset + batch_start

            if is_streaming:
                # 流式输出模式：实时显示当前翻译结果
                current_text = batch_data.get('current_text', '')
                expected_lines = batch_data.get('expected_lines', 1)

                # ✅ 彻底过滤空行：将连续的多个空行合并为一个，避免大量空行堆积
                all_lines = current_text.split('\n')
                streaming_lines = []
                prev_empty = False
                for line in all_lines:
                    if line.strip():  # 有内容的行
                        streaming_lines.append(line)
                        prev_empty = False
                    else:  # 空行
                        # 只在前一行不是空行时才保留一个空行
                        if not prev_empty and streaming_lines:  # 且不是第一行
                            streaming_lines.append('')
                            prev_empty = True
                        # 否则跳过这个空行

                # 实时显示：不超过预期行数
                last_item = None
                for i, line in enumerate(streaming_lines[:expected_lines]):
                    row_index = absolute_start + i
                    if row_index < len(items):
                        item = items[row_index]
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = line.strip()  # 实时更新译文栏
                        self.translation_table.item(item, values=values)
                        last_item = item

                # 只滚动到最后更新的行
                if last_item is not None:
                    self.translation_table.see(last_item)
            else:
                # 批次完成模式：写入最终结果
                translated_lines = batch_data.get('translated_lines', [])

                # 将翻译结果写回对应的行
                last_item = None
                for i, translated_line in enumerate(translated_lines):
                    row_index = absolute_start + i
                    if row_index < len(items):
                        item = items[row_index]
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = translated_line.strip()  # 更新译文
                        self.translation_table.item(item, values=values)
                        last_item = item

                # 滚动到最后写入的行
                if last_item is not None:
                    self.translation_table.see(last_item)

                # 触发保存
                self.schedule_save()

        self.root.after(0, update_ui)

    def _on_translation_complete(self):
        """翻译完成回调（新增：自动翻译查漏机制）"""
        def update_ui():
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.progress_var.set(100)
            self.status_updater("翻译完成")
            # 复位续写标记
            self._continuing_mode = False
            self._continuing_first_insert = False

            # ✅ 新增：启动翻译查漏机制
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
        """选中行翻译进度回调"""
        def update_ui():
            # 更新进度条
            if progress >= 0:
                self.progress_var.set(progress)
                self.progress_bar.update_idletasks()

            if not batch_data or not isinstance(batch_data, dict):
                return

            # 获取选中的数据
            selected_data = getattr(self, '_selected_translation_data', [])
            if not selected_data:
                return

            batch_start = batch_data.get('batch_start', 0)
            is_streaming = batch_data.get('streaming', False)

            if is_streaming:
                # 流式输出模式：实时显示当前翻译结果
                current_text = batch_data.get('current_text', '')
                expected_lines = batch_data.get('expected_lines', 1)

                # ✅ 彻底过滤空行：将连续的多个空行合并为一个，避免大量空行堆积
                all_lines = current_text.split('\n')
                streaming_lines = []
                prev_empty = False
                for line in all_lines:
                    if line.strip():  # 有内容的行
                        streaming_lines.append(line)
                        prev_empty = False
                    else:  # 空行
                        # 只在前一行不是空行时才保留一个空行
                        if not prev_empty and streaming_lines:  # 且不是第一行
                            streaming_lines.append('')
                            prev_empty = True
                        # 否则跳过这个空行

                # 实时显示：不超过预期行数
                last_item = None
                for i, line in enumerate(streaming_lines[:expected_lines]):
                    row_index = batch_start + i
                    if row_index < len(selected_data):
                        item = selected_data[row_index]['item']
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = line.strip()  # 实时更新译文栏
                        self.translation_table.item(item, values=values)
                        last_item = item

                # 只滚动到最后更新的行
                if last_item is not None:
                    self.translation_table.see(last_item)
            else:
                # 批次完成模式：写入最终结果
                translated_lines = batch_data.get('translated_lines', [])

                # 将翻译结果写回对应的行
                last_item = None
                for i, translated_line in enumerate(translated_lines):
                    row_index = batch_start + i
                    if row_index < len(selected_data):
                        item = selected_data[row_index]['item']
                        values = list(self.translation_table.item(item)['values'])
                        values[2] = translated_line.strip()  # 更新译文
                        self.translation_table.item(item, values=values)
                        last_item = item

                # 滚动到最后写入的行
                if last_item is not None:
                    self.translation_table.see(last_item)

                # 触发保存
                self.schedule_save()

        self.root.after(0, update_ui)

    def _on_selected_translation_complete(self):
        """选中行翻译完成回调"""
        def complete_ui():
            # 恢复界面状态
            self.is_translating = False
            self.translate_btn.config(state=tk.NORMAL)
            self.continue_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)

            # 设置进度为100%
            self.progress_var.set(100)

            # 获取翻译的行数
            selected_count = len(getattr(self, '_selected_translation_data', []))

            # 清理临时数据
            if hasattr(self, '_selected_translation_data'):
                delattr(self, '_selected_translation_data')

            # 立即保存
            self.schedule_save(delay_ms=0)

            self.status_updater(f"选中的 {selected_count} 行翻译完成")
            messagebox.showinfo("翻译完成", f"已完成 {selected_count} 行的翻译")

        self.root.after(0, complete_ui)

    def _start_missing_translation_check(self):
        """启动翻译查漏机制（新增：自动检测并翻译空行）

        核心逻辑：
        1. 检查所有行，找出原文不为空但译文为空的行
        2. 如果存在空行，自动启动翻译
        3. 一次最多翻译20个空行
        4. 翻译完成后继续检查，直到所有行都翻译完成
        """
        # 如果正在翻译，跳过
        if self.is_translating:
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
            return

        # 有空行，开始翻译查漏
        total_empty = len(empty_indices)
        self.status_updater(f"正在进行翻译查漏：发现 {total_empty} 个空行")

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
            args=(combined_source, self.translation_mode.get())
        )
        translation_thread.daemon = True
        translation_thread.start()

    def _translate_missing_worker(self, content, mode):
        """翻译查漏工作线程"""
        try:
            self.translator.translate_fast_mode(
                content,
                self._on_missing_translation_progress,
                self._on_missing_translation_complete
            )
        except Exception as e:
            self.root.after(0, lambda: self._on_translation_error(str(e)))

    def _on_missing_translation_progress(self, progress, batch_data):
        """翻译查漏进度回调"""
        def update_ui():
            # 更新进度条
            if progress >= 0:
                self.progress_var.set(progress)
                self.progress_bar.update_idletasks()

            if not batch_data or not isinstance(batch_data, dict):
                return

            # 获取空行位置
            missing_indices = getattr(self, '_missing_translation_indices', [])
            if not missing_indices:
                return

            batch_start = batch_data.get('batch_start', 0)
            is_streaming = batch_data.get('streaming', False)
            items = self.translation_table.get_children()

            if is_streaming:
                # 流式输出模式
                current_text = batch_data.get('current_text', '')
                expected_lines = batch_data.get('expected_lines', 1)
                streaming_lines = [line for line in current_text.split('\n') if line.strip()]

                last_item = None
                for i, line in enumerate(streaming_lines[:expected_lines]):
                    relative_index = batch_start + i
                    if relative_index < len(missing_indices):
                        row_index = missing_indices[relative_index]
                        if row_index < len(items):
                            item = items[row_index]
                            values = list(self.translation_table.item(item)['values'])
                            values[2] = line.strip()
                            self.translation_table.item(item, values=values)
                            last_item = item

                # 只滚动到最后更新的行
                if last_item is not None:
                    self.translation_table.see(last_item)
            else:
                # 批次完成模式
                translated_lines = batch_data.get('translated_lines', [])

                last_item = None
                for i, translated_line in enumerate(translated_lines):
                    relative_index = batch_start + i
                    if relative_index < len(missing_indices):
                        row_index = missing_indices[relative_index]
                        if row_index < len(items):
                            item = items[row_index]
                            values = list(self.translation_table.item(item)['values'])
                            values[2] = translated_line.strip()
                            self.translation_table.item(item, values=values)
                            last_item = item

                # 滚动到最后写入的行
                if last_item is not None:
                    self.translation_table.see(last_item)

                # 保存
                self.schedule_save()

        self.root.after(0, update_ui)

    def _on_missing_translation_complete(self):
        """翻译查漏完成回调：继续检查是否还有空行"""
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

            # 继续检查是否还有空行（循环执行）
            self.root.after(1000, self._start_missing_translation_check)

        self.root.after(0, update_ui)

    def save_translation(self):
        """保存译文。

        修复说明：确保译文与原文按行号严格对齐。
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
                        pass  # EPUB映射同步失败，忽略

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

            try:
                self.epub_processor.save_translations(
                    str(current_mapping_dir),
                    target_lines
                )
            except Exception:
                pass

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
                            image_map = json.load(f)
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
                messagebox.showinfo("导出成功", f"已导出EPUB文件: {Path(result_path).name}")
            except Exception as e:
                messagebox.showerror("导出错误", f"EPUB导出失败: {str(e)}")
        except Exception as e:
            messagebox.showerror("导出错误", f"导出EPUB操作失败: {str(e)}")
