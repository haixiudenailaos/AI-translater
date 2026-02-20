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
from ..api.deepseek_api import DeepseekAPI

class TranslatorEngine:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.api = None
        self.is_stopped = False
        
        # 延迟初始化API与正则（按需构建）
        self._re_many_newlines = None
        # 不在构造时初始化 API，首次使用时再构建
        
    def _init_api(self):
        """初始化API客户端"""
        api_config = self.config_manager.get_api_config()
        provider = api_config.get("provider", "siliconflow")
        
        if provider == "deepseek":
            self.api = DeepseekAPI(api_config)
        elif provider == "siliconflow":
            self.api = SiliconFlowAPI(api_config)
        else:
            # 默认使用 SiliconFlow
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
        """逐行翻译模式（重构：分批翻译 + 流式输出）
        
        核心原则：
        1. 按配置的batch_lines（默认20行）拆分原文
        2. 每批使用流式翻译，实时显示翻译进度
        3. 每批翻译完成后，准确写入对应行号位置
        4. 绝对不依赖换行符拆分，只按行号对应
        """
        # 重置状态
        self.reset()
        self._ensure_api()
        
        try:
            lines = content.split('\n')
            total_lines = len(lines)
            app_config = self.config_manager.get_app_config()
            batch_lines = app_config.get("batch_lines", 20)  # 每批翻译的行数
            
            # 按批次处理
            for batch_start in range(0, total_lines, batch_lines):
                # 检查是否停止
                if self.is_stopped:
                    break
                
                # 获取当前批次的原文行
                batch_end = min(batch_start + batch_lines, total_lines)
                batch_source_lines = lines[batch_start:batch_end]
                
                # 翻译当前批次（使用流式输出，传递总行数用于进度计算）
                batch_translated_lines = self._translate_batch(
                    batch_source_lines, 
                    progress_callback, 
                    batch_start,
                    total_lines  # ✅ 传递总行数
                )
                
                # 计算总进度
                overall_progress = (batch_end / total_lines) * 100
                
                # 回调：传递批次翻译完成的结果
                progress_callback(overall_progress, {
                    'batch_start': batch_start,
                    'translated_lines': batch_translated_lines,
                    'streaming': False  # 标记为批次完成
                })
                
                # 短暂延迟
                time.sleep(0.1)
            
            if not self.is_stopped:
                complete_callback()
                
        except Exception as e:
            raise e
            
    def translate_fast_mode(self, content: str, progress_callback: Callable, complete_callback: Callable):
        """快速翻译模式（重构：分批翻译 + 流式输出）
        
        核心原则：
        1. 按配置的batch_lines（默认20行）拆分原文
        2. 每批使用流式翻译，实时显示翻译进度
        3. 每批翻译完成后，准确写入对应行号位置
        4. 绝对不依赖换行符拆分，只按行号对应
        """
        # 重置状态
        self.reset()
        self._ensure_api()
        
        try:
            lines = content.split('\n')
            total_lines = len(lines)
            app_config = self.config_manager.get_app_config()
            batch_lines = app_config.get("batch_lines", 20)  # 每批翻译的行数
            
            # 按批次处理
            for batch_start in range(0, total_lines, batch_lines):
                # 检查是否停止
                if self.is_stopped:
                    break
                
                # 获取当前批次的原文行
                batch_end = min(batch_start + batch_lines, total_lines)
                batch_source_lines = lines[batch_start:batch_end]
                
                # 翻译当前批次（使用流式输出，传递总行数用于进度计算）
                batch_translated_lines = self._translate_batch(
                    batch_source_lines, 
                    progress_callback, 
                    batch_start,
                    total_lines  # ✅ 传递总行数
                )
                
                # 计算总进度
                overall_progress = (batch_end / total_lines) * 100
                
                # 回调：传递批次翻译完成的结果
                progress_callback(overall_progress, {
                    'batch_start': batch_start,
                    'translated_lines': batch_translated_lines,
                    'streaming': False  # 标记为批次完成
                })
                
                # 短暂延迟
                time.sleep(0.2)
            
            if not self.is_stopped:
                complete_callback()
                
        except Exception as e:
            raise e
            
    def _translate_batch(self, batch_lines: List[str], progress_callback: Callable, batch_start: int, total_lines: Optional[int] = None) -> List[str]:
        """翻译一批原文行，使用流式输出提升体验（增强：行号标记机制）
        
        核心逻辑：
        1. 为每行原文添加行号标记，确保API返回时能正确对齐
        2. 使用流式翻译，实时显示结果
        3. 翻译完成后，解析行号标记并按序排列译文
        4. 确保返回的译文行数 = 原文行数
        5. ✅ 增强：API请求失败时自动重试最多5次
        6. ✅ 修复进度条：流式阶段也显示准确进度
        7. ✅ 新增：行号标记机制解决对齐问题
        """
        if not batch_lines:
            return []
        
        # ✅ 新增：为每行添加行号标记
        marked_lines = []
        for i, line in enumerate(batch_lines):
            line_marker = f"[LINE_{i+1:03d}]"
            marked_lines.append(f"{line_marker}{line}")
        
        # 合并带标记的原文
        batch_content = '\n'.join(marked_lines)
        expected_lines = len(batch_lines)
        
        # 构建提示词
        app_config = self.config_manager.get_app_config()
        base_prompt = app_config.get("translation_prompt", "")
        glossary_prompt = self.config_manager.get_glossary_prompt()
        target_language = app_config.get("target_language", "中文")
        
        full_prompt = f"{base_prompt}\n\n"
        if glossary_prompt:
            full_prompt += glossary_prompt + "\n\n"
        full_prompt += f"""请将以下文本翻译为{target_language}。

重要说明：
1. 每行文本前都有行号标记 [LINE_XXX]，请在译文中保留这些标记
2. 保持原文的换行结构，每行对应翻译
3. 译文格式：[LINE_XXX]译文内容

原文：
{batch_content}"""
        
        # ✅ 新增：重试机制（最多5次）
        max_retries = 5
        for retry_count in range(max_retries):
            try:
                # 使用流式翻译，提升用户体验
                stream_buffer = []
                
                # ✅ 新增：按行更新的流式回调状态
                completed_lines = []  # 已完成的完整行
                last_sent_lines = 0   # 上次发送给UI的行数
                
                def stream_callback(chunk):
                    """流式回调：按完整行更新UI"""
                    nonlocal completed_lines, last_sent_lines
                    
                    stream_buffer.append(chunk)
                    current_text = ''.join(stream_buffer)
                    
                    # ✅ 解析当前文本，识别完整的行
                    lines = current_text.split('\n')
                    
                    # 检查是否有新的完整行（除了最后一行，因为可能还在输出中）
                    if len(lines) > 1:
                        # 前面的行都是完整的，最后一行可能还在输出中
                        complete_lines_now = lines[:-1]
                        
                        # 如果有新的完整行
                        if len(complete_lines_now) > len(completed_lines):
                            # 更新已完成的行列表
                            completed_lines = complete_lines_now[:]
                            
                            # 过滤行号标记，只保留译文内容
                            filtered_lines = []
                            for line in completed_lines:
                                filtered_line = re.sub(r'\[LINE_\d+\]', '', line)
                                filtered_lines.append(filtered_line)
                            
                            filtered_text = '\n'.join(filtered_lines)
                            
                            # ✅ 计算流式阶段的进度
                            if total_lines and total_lines > 0:
                                # 已完成到当前批次起始位置的进度
                                base_progress = (batch_start / total_lines) * 100
                                # 当前批次占总进度的比例
                                batch_weight = (expected_lines / total_lines) * 100
                                # 根据已完成行数估算当前批次内的进度
                                line_progress = len(completed_lines) / expected_lines if expected_lines > 0 else 0
                                streaming_progress = base_progress + (batch_weight * line_progress)
                                streaming_progress = min(streaming_progress, 100.0)  # 不超过100%
                            else:
                                streaming_progress = 0
                            
                            # 只在有新完整行时才回调更新UI
                            progress_callback(streaming_progress, {
                                'batch_start': batch_start,
                                'streaming': True,  # 标记为流式输出
                                'current_text': filtered_text,  # ✅ 使用过滤后的文本
                                'expected_lines': expected_lines,
                                'completed_lines': len(completed_lines)  # 新增：已完成行数
                            })
                            
                            last_sent_lines = len(completed_lines)
                
                # 调用流式翻译API
                if self.api is None:
                    raise Exception("API client not initialized")
                response = self.api.translate_stream(full_prompt, stream_callback)
                
                # ✅ 增强：检查翻译结果是否有效
                if not response or not response.strip():
                    # 没有翻译结果，使用流式缓冲区的内容（可能为空）
                    translated_content = ''.join(stream_buffer).strip()
                    if not translated_content:
                        # 流式缓冲区也为空，触发重试
                        if retry_count < max_retries - 1:
                            print(f"翻译结果为空，正在重试 ({retry_count + 1}/{max_retries})...")
                            time.sleep(1)  # 短暂延迟后重试
                            continue
                        else:
                            # 所有重试都失败，返回空行列表
                            print(f"翻译失败：{max_retries}次重试后仍无结果")
                            return [''] * expected_lines
                else:
                    translated_content = response.strip()
                
                # ✅ 新增：解析行号标记并按序排列译文
                all_lines = translated_content.split('\n')
                
                # 解析行号标记，构建行号到译文的映射
                line_mapping = {}
                unmarked_lines = []  # 没有行号标记的行
                
                line_marker_pattern = re.compile(r'^\[LINE_(\d+)\](.*)$')
                
                for line in all_lines:
                    match = line_marker_pattern.match(line)
                    if match:
                        line_num = int(match.group(1))
                        content = match.group(2)
                        line_mapping[line_num] = content
                    else:
                        # 没有行号标记的行，可能是API返回格式异常
                        if line.strip():  # 只保留非空行
                            unmarked_lines.append(line)
                
                # 按行号顺序重建译文列表
                translated_lines = []
                for i in range(1, expected_lines + 1):
                    if i in line_mapping:
                        translated_lines.append(line_mapping[i])
                    else:
                        # 缺失的行号，尝试从未标记行中补充
                        if unmarked_lines:
                            translated_lines.append(unmarked_lines.pop(0))
                        else:
                            translated_lines.append('')  # 空行占位
                
                # ✅ 关键：确保译文行数 = 原文行数
                if len(translated_lines) < expected_lines:
                    # 译文行数不足，补充空行
                    translated_lines.extend([''] * (expected_lines - len(translated_lines)))
                elif len(translated_lines) > expected_lines:
                    # 译文行数过多，截断
                    translated_lines = translated_lines[:expected_lines]
                
                # ✅ 成功获取翻译结果，返回
                return translated_lines
                
            except Exception as e:
                # ✅ 增强：捕获异常后重试
                if retry_count < max_retries - 1:
                    print(f"翻译请求失败: {str(e)}，正在重试 ({retry_count + 1}/{max_retries})...")
                    time.sleep(1)  # 短暂延迟后重试
                    continue
                else:
                    # 所有重试都失败，返回空行列表
                    print(f"翻译失败：{max_retries}次重试后仍失败 - {str(e)}")
                    return [''] * expected_lines
        
        # ✅ 兜底：理论上不会到达这里，但为了安全起见
        return [''] * expected_lines

    def stop(self):
        """停止翻译"""
        self.is_stopped = True
        # 取消所有正在进行的API请求
        if self.api:
            self.api.cancel_requests()
        
    def reset(self):
        """重置状态"""
        self.is_stopped = False
        # 重置API取消状态
        if self.api:
            self.api.reset_cancel()
