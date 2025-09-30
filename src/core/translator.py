#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译引擎模块
负责协调翻译流程，支持逐行模式和快速模式
"""

import threading
import time
from typing import Callable, Optional, List
import re

from ..api.siliconflow_api import SiliconFlowAPI

class TranslatorEngine:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.api = None
        self.is_paused = False
        self.is_stopped = False
        
        # 延迟初始化API与正则（按需构建）
        self._re_many_newlines = None
        # 不在构造时初始化 API，首次使用时再构建
        
    def _init_api(self):
        """初始化API客户端"""
        api_config = self.config_manager.get_api_config()
        if api_config.get("provider") == "siliconflow":
            self.api = SiliconFlowAPI(api_config)
            
    def _ensure_api(self):
        """惰性初始化API客户端"""
        if self.api is None:
            self._init_api()

    def _get_re_many_newlines(self):
        """按需预编译：匹配≥2个换行"""
        if self._re_many_newlines is None:
            self._re_many_newlines = re.compile(r'\n{2,}')
        return self._re_many_newlines

    def refresh_api(self):
        """刷新API配置"""
        self._init_api()
            
    def translate_line_by_line(self, content: str, progress_callback: Callable, complete_callback: Callable):
        """逐行翻译模式（改为流式增量输出）"""
        # 重置状态，确保开始新翻译时状态干净
        self.reset()
        # 惰性初始化 API
        self._ensure_api()
        
        try:
            lines = content.split('\n')
            total_lines = len(lines)
            app_config = self.config_manager.get_app_config()
            context_lines = app_config.get("context_lines", 2)
            
            translated_lines = []
            
            for i, line in enumerate(lines):
                # 检查是否暂停或停止
                while self.is_paused and not self.is_stopped:
                    time.sleep(0.1)
                    
                if self.is_stopped:
                    break
                    
                # 跳过空行
                if not line.strip():
                    translated_lines.append(line)
                    progress = (i + 1) / total_lines * 100
                    progress_callback(progress, line + '\n')
                    continue
                    
                # 构建上下文
                context = self._build_context(lines, i, context_lines)
                
                # 组装提示词并流式翻译，实时渲染到译文框
                app_cfg = self.config_manager.get_app_config()
                base_prompt = app_cfg.get("translation_prompt", "")
                glossary_prompt = self.config_manager.get_glossary_prompt()
                target_language = app_cfg.get("target_language", "中文")
                
                full_prompt = f"{base_prompt}\n\n"
                if context:
                    full_prompt += f"【上下文信息】\n{context}\n\n"
                if glossary_prompt:
                    full_prompt += glossary_prompt + "\n\n"
                full_prompt += f"请将以下文本翻译为{target_language}：\n{line}"
                
                translated_parts: List[str] = []
                # 更新进度（该行的固定进度值）
                progress = (i + 1) / total_lines * 100
                def on_chunk(chunk: str):
                    translated_parts.append(chunk)
                    # 实时输出增量片段（异常安全）
                    try:
                        progress_callback(progress, chunk)
                    except Exception as _e:
                        print(f"[fast/newlines] line_by_line on_chunk callback error: {type(_e).__name__}: {_e}")
                
                try:
                    response = self.api.translate_stream_enhanced(full_prompt, callback=on_chunk, context=None)
                    # 若回调未收到内容，则使用返回值备用
                    translated_line = ''.join(translated_parts) if translated_parts else (response.strip() if response else line)
                except Exception as e:
                    print(f"翻译错误: {e}")
                    translated_line = line
                
                translated_lines.append(translated_line)
                # 行结束补一个换行
                progress_callback(progress, '\n')
                
                # 短暂延迟，避免API请求过快
                time.sleep(0.1)
                
            if not self.is_stopped:
                complete_callback()
                
        except Exception as e:
            raise e
            
    def translate_fast_mode(self, content: str, progress_callback: Callable, complete_callback: Callable):
        """快速翻译模式（改为流式增量输出）"""
        # 重置状态，确保开始新翻译时状态干净
        self.reset()
        # 惰性初始化 API
        self._ensure_api()
        
        try:
            app_config = self.config_manager.get_app_config()
            chunk_size = app_config.get("chunk_size", 1000)
            
            # 获取缓存清理配置
            fast_flush_interval = app_config.get("fast_flush_interval", 2)
            fast_flush_strategy = app_config.get("fast_flush_strategy", "optimize")
            
            # 按字数分块
            chunks = self._split_into_chunks(content, chunk_size)
            total_chunks = len(chunks)
            
            # 预编译常用换正规则（按需）
            re_many = self._get_re_many_newlines()
            
            translated_chunks = []
            last_ended_with_newline = False  # 跟踪上一块是否以换行结束
            
            for i, chunk in enumerate(chunks):
                # 检查是否暂停或停止
                while self.is_paused and not self.is_stopped:
                    time.sleep(0.1)
                    
                if self.is_stopped:
                    break
                    
                # 使用流式翻译当前块，实时渲染到译文框
                app_cfg = self.config_manager.get_app_config()
                base_prompt = app_cfg.get("translation_prompt", "")
                glossary_prompt = self.config_manager.get_glossary_prompt()
                target_language = app_cfg.get("target_language", "中文")
                
                full_prompt = f"{base_prompt}\n\n"
                if glossary_prompt:
                    full_prompt += glossary_prompt + "\n\n"
                full_prompt += f"请将以下文本翻译为{target_language}：\n\n{chunk}"
                
                # 打印本块开始时的换行统计
                self._log_newline_debug("chunk_start", i, 0, chunk)
                
                translated_parts: List[str] = []
                # 更新进度
                progress = (i + 1) / total_chunks * 100
                
                def on_chunk(chunk_part: str):
                    translated_parts.append(chunk_part)
                    # 实时打印增量片段换行统计
                    piece_idx = len(translated_parts)
                    self._log_newline_debug("on_chunk", i, piece_idx, chunk_part)
                    
                    # 处理流式输出中的换行符，避免多余空行
                    processed_part = chunk_part
                    # 片段级压缩：将连续≥3个换行压为2个
                    try:
                        processed_part = re_many.sub('\n', processed_part)
                    except Exception as _e:
                        print(f"[fast/newlines] compress(part) error: {type(_e).__name__}: {_e}")
                    
                    # 桥接规则B：上一块以换行结束且当前块首片段以换行开头，则去掉一个前导换行
                    if piece_idx == 1 and i > 0 and translated_chunks:
                        try:
                            if translated_chunks[-1].endswith('\n') and processed_part.startswith('\n'):
                                processed_part = processed_part.lstrip('\n')
                                if self._should_log_fast_debug("minimal"):
                                    print(f"[fast/newlines] bridge chunk={i} piece=1 removed_leading_newline")
                        except Exception as _e:
                            print(f"[fast/newlines] bridge error: {type(_e).__name__}: {_e}")
                    
                    # 实时增量输出到UI（异常安全）
                    try:
                        progress_callback(progress, processed_part)
                    except Exception as _e:
                        print(f"[fast/newlines] on_chunk callback error: {type(_e).__name__}: {_e}; fallback to raw part")
                        try:
                            progress_callback(progress, chunk_part)
                        except Exception as __e:
                            print(f"[fast/newlines] fallback callback failed: {type(__e).__name__}: {__e}")
                
                try:
                    response = self.api.translate_stream_enhanced(full_prompt, callback=on_chunk, context=None)
                    translated_chunk = ''.join(translated_parts) if translated_parts else (response.strip() if response else chunk)
                except Exception as e:
                    print(f"翻译错误: {e}")
                    translated_chunk = chunk
                # 块级兜底压缩：将连续≥3个换行压为2个
                try:
                    translated_chunk = re_many.sub('\n', translated_chunk)
                    translated_chunk = translated_chunk.rstrip('\n') + '\n'
                except Exception as _e:
                    print(f"[fast/newlines] compress(chunk) error: {type(_e).__name__}: {_e}")
                
                translated_chunks.append(translated_chunk)
                # 本块结束统计
                self._log_newline_debug("chunk_end", i, 0, translated_chunk)
                # 在块结束处显式输出一个换行以确保块间分隔（非最后一块）
                if i < total_chunks - 1:
                    try:
                        progress_callback(progress,'\n')
                    except Exception as _e:
                        print(f"[fast/newlines] chunk_end emit newline error: {type(_e).__name__}: {_e}")
                
                # 智能处理块间分隔符，避免重复换行
                if i < total_chunks - 1:  # 不是最后一块
                    # 检查当前块是否以换行结束
                    current_ends_with_newline = translated_chunk.endswith('\n')
                    
                    # 打印桥接换行决策
                    if self._should_log_fast_debug("minimal"):
                        next_stats = self._newline_stats(chunks[i+1])
                        curr_stats = self._newline_stats(chunk)
                        print(f"[fast/newlines] bridge decision chunk {i} -> {i+1}: src_current_trailing={curr_stats['trailing']}, src_next_leading={next_stats['leading']}, will_add_newline=False")
                    
                    # 移除原有的块间分隔符逻辑，改为在流式输出时处理
                    # 这样可以避免在块结束时添加换行符，而是在下一块开始时智能添加
                    last_ended_with_newline = current_ends_with_newline
                
                # 周期性缓存清理，避免内存占用过高
                if (i + 1) % fast_flush_interval == 0:
                    if self._should_log_fast_debug("minimal"):
                        print(f"[fast/flush] after chunk {i+1}, strategy={fast_flush_strategy}")
                    if hasattr(self.api, 'optimize_cache') and fast_flush_strategy == "optimize":
                        self.api.optimize_cache()
                    elif hasattr(self.api, 'clear_cache') and fast_flush_strategy == "clear":
                        self.api.clear_cache()
                
                # 短暂延迟
                time.sleep(0.2)
                
            if not self.is_stopped:
                complete_callback()
                
        except Exception as e:
            raise e
            
    def _build_context(self, lines: List[str], current_index: int, context_lines: int) -> str:
        """构建上下文"""
        context = []
        
        # 添加前面的行
        start = max(0, current_index - context_lines)
        for i in range(start, current_index):
            if lines[i].strip():
                context.append(f"前文: {lines[i]}")
                
        # 添加后面的行
        end = min(len(lines), current_index + context_lines + 1)
        for i in range(current_index + 1, end):
            if lines[i].strip():
                context.append(f"后文: {lines[i]}")
                
        return '\n'.join(context) if context else ""
        
    def _translate_single_line(self, line: str, context: str) -> str:
        """翻译单行文本"""
        if not self.api:
            return line
            
        app_config = self.config_manager.get_app_config()
        base_prompt = app_config.get("translation_prompt", "")
        glossary_prompt = self.config_manager.get_glossary_prompt()
        target_language = self.config_manager.get_app_config().get("target_language", "中文")
        
        # 构建完整提示词
        full_prompt = f"{base_prompt}\n\n"
        
        if context:
            full_prompt += f"【上下文信息】\n{context}\n\n"
            
        if glossary_prompt:
            full_prompt += glossary_prompt + "\n\n"
            
        full_prompt += f"请将以下文本翻译为{target_language}：\n{line}"
        
        try:
            response = self.api.translate(full_prompt)
            return response.strip() if response else line
        except Exception as e:
            print(f"翻译错误: {e}")
            return line
            
    def _translate_chunk(self, chunk: str) -> str:
        """翻译文本块"""
        if not self.api:
            return chunk
            
        app_config = self.config_manager.get_app_config()
        base_prompt = self.config_manager.get_app_config().get("translation_prompt", "")
        glossary_prompt = self.config_manager.get_glossary_prompt()
        target_language = self.config_manager.get_app_config().get("target_language", "中文")
        
        # 构建完整提示词
        full_prompt = f"{base_prompt}\n\n"
        
        if glossary_prompt:
            full_prompt += glossary_prompt + "\n\n"
            
        full_prompt += f"请将以下文本翻译为{target_language}：\n\n{chunk}"
        
        try:
            response = self.api.translate(full_prompt)
            return response.strip() if response else chunk
        except Exception as e:
            print(f"翻译错误: {e}")
            return chunk
            
    def _split_into_chunks(self, content: str, chunk_size: int) -> List[str]:
        """将文本按字数分块"""
        chunks = []
        lines = content.split('\n')
        current_chunk = []
        current_size = 0
        
        for line in lines:
            line_size = len(line)
            
            # 如果当前块加上这一行会超过限制，先保存当前块
            if current_size + line_size > chunk_size and current_chunk:
                chunks.append('\n'.join(current_chunk))
                current_chunk = []
                current_size = 0
                
            current_chunk.append(line)
            current_size += line_size
            
        # 添加最后一块
        if current_chunk:
            chunks.append('\n'.join(current_chunk))
            
        return chunks
        
    # ========== Newline debug helpers (fast mode) ==========
    def _get_fast_debug_level(self) -> str:
        """获取快速模式换行调试等级：off|minimal|standard|verbose"""
        try:
            lvl = self.config_manager.get_app_config().get("fast_debug_newlines", "standard")
            if isinstance(lvl, bool):
                return "standard" if lvl else "off"
            return str(lvl).lower()
        except Exception:
            return "standard"

    def _should_log_fast_debug(self, need: str = "minimal") -> bool:
        """根据等级判断是否需要打印日志"""
        order = {"off": 0, "minimal": 1, "standard": 2, "verbose": 3}
        level = self._get_fast_debug_level()
        return order.get(level, 2) >= order.get(need, 1)

    def _newline_stats(self, text: str) -> dict:
        """统计文本中的换行特征"""
        if text is None:
            text = ""
        total = text.count("\n")
        has_crlf = "\r\n" in text
        # 计算前导/尾随连续换行数
        leading = 0
        for ch in text:
            if ch == "\n":
                leading += 1
            else:
                break
        trailing = 0
        for ch in reversed(text):
            if ch == "\n":
                trailing += 1
            else:
                break
        # 最大连续换行
        max_cons = 0
        cur = 0
        for ch in text:
            if ch == "\n":
                cur += 1
                if cur > max_cons:
                    max_cons = cur
            else:
                cur = 0
        sample_tail = text[-40:].replace("\r", "\\r").replace("\n", "\\n")
        return {
            "total": total,
            "leading": leading,
            "trailing": trailing,
            "max_cons": max_cons,
            "has_crlf": has_crlf,
            "len": len(text),
            "tail": sample_tail
        }

    def _log_newline_debug(self, where: str, chunk_idx: int, piece_idx: int, text: str):
        """按配置打印换行调试信息"""
        if not self._should_log_fast_debug("minimal"):
            return
        stats = self._newline_stats(text)
        lvl = self._get_fast_debug_level()
        prefix = f"[fast/newlines] {where} chunk={chunk_idx}"
        if piece_idx:
            prefix += f" piece={piece_idx}"
        if lvl == "minimal":
            print(f"{prefix} | total={stats['total']} trailing={stats['trailing']} len={stats['len']}")
        elif lvl == "standard":
            print(f"{prefix} | total={stats['total']} leading={stats['leading']} trailing={stats['trailing']} max_cons={stats['max_cons']} crlf={stats['has_crlf']} len={stats['len']}")
        else:  # verbose
            print(f"{prefix} | total={stats['total']} leading={stats['leading']} trailing={stats['trailing']} max_cons={stats['max_cons']} crlf={stats['has_crlf']} len={stats['len']} tail='{stats['tail']}'")

    def pause(self):
        """暂停翻译"""
        self.is_paused = True
        
    def resume(self):
        """恢复翻译"""
        self.is_paused = False
        
    def stop(self):
        """停止翻译"""
        self.is_stopped = True
        self.is_paused = False
        # 取消所有正在进行的API请求
        if self.api:
            self.api.cancel_requests()
        
    def reset(self):
        """重置状态"""
        self.is_paused = False
        self.is_stopped = False
        # 重置API取消状态
        if self.api:
            self.api.reset_cancel()