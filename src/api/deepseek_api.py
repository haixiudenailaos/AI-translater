#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deepseek API接口模块
支持流式翻译、智能缓存和批处理优化
"""

import httpx
import json
import time
import threading
from typing import Dict, Any, Optional, List, Callable
from ..core.smart_cache import SmartCache
from ..core.batch_processor import BatchProcessor, get_batch_processor

class DeepseekAPI:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.base_url = config.get("base_url", "https://api.deepseek.com/v1")
        self.api_key = config.get("api_key", "").strip()
        self.model_name = config.get("model_name", "deepseek-chat")
        self.max_tokens = config.get("max_tokens", 2048)
        self.temperature = config.get("temperature", 0.3)
        self._cancel_event = threading.Event()
        self._current_client = None

        # HTTP连接池与超时配置
        http_limits = config.get("http_limits", {})
        self._max_keepalive = http_limits.get("max_keepalive_connections", 10)
        self._max_connections = http_limits.get("max_connections", 20)
        self._timeout = config.get("http_timeout", 60.0)

        # 初始化持久客户端
        self._recreate_client()
        
        # 设置请求头
        if self.api_key:
            self.headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
        else:
            self.headers = {
                "Content-Type": "application/json"
            }
            
        # 初始化缓存和批处理
        self.enable_cache = config.get("enable_cache", True)
        self.enable_batch = config.get("enable_batch", True)
        self.enable_stream = config.get("enable_stream", True)
        
        if self.enable_cache:
            cache_config = config.get("cache_config", {})
            self.cache = SmartCache(
                max_memory_size=cache_config.get("max_memory_size", 1000),
                max_file_size=cache_config.get("max_file_size", 10000),
                cache_dir=cache_config.get("cache_dir"),
                ttl_hours=cache_config.get("ttl_hours", 24)
            )
        else:
            self.cache = None
            
        if self.enable_batch:
            batch_config = config.get("batch_config", {})
            self.batch_processor = get_batch_processor(
                max_batch_size=batch_config.get("max_batch_size", 10),
                max_wait_time=batch_config.get("max_wait_time", 0.5),
                max_workers=batch_config.get("max_workers", 4),
                enable_priority=batch_config.get("enable_priority", True),
                enable_deduplication=batch_config.get("enable_deduplication", True)
            )
            self.batch_processor.set_api_handler(self._batch_translate_handler)
        else:
            self.batch_processor = None
            
        # 流式翻译回调管理
        self.stream_callbacks: Dict[str, Callable] = {}
    
    def cancel_requests(self):
        """取消当前所有请求"""
        self._cancel_event.set()
        if self._current_client:
            try:
                self._current_client.close()
            except:
                pass
    
    def reset_cancel(self):
        """重置取消状态"""
        self._cancel_event.clear()
        self._current_client = None

    def _recreate_client(self):
        """重建持久HTTP客户端（连接池）"""
        try:
            if self._current_client:
                try:
                    self._current_client.close()
                except:
                    pass
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections
            )
            self._current_client = httpx.Client(timeout=self._timeout, limits=limits)
        except Exception as e:
            print(f"重建HTTP客户端失败: {e}")
            self._current_client = httpx.Client(timeout=self._timeout)

    def _get_client(self) -> httpx.Client:
        """获取持久客户端；若不存在则重建"""
        if not self._current_client:
            self._recreate_client()
        return self._current_client
        
    def test_connection(self) -> bool:
        """测试API连接"""
        if not self.api_key:
            print("API密钥为空")
            return False

        def _check_chat_once(client: httpx.Client) -> Optional[bool]:
            try:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "temperature": 0.0,
                        "stream": False
                    },
                    timeout=min(self._timeout, 3.0)
                )
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                        if "choices" in body and isinstance(body["choices"], list) and len(body["choices"]) > 0:
                            return True
                        return False
                    except Exception:
                        return True
                elif resp.status_code in (401, 403):
                    print("连接失败：鉴权错误（API Key 可能无效或权限不足）")
                    return False
                elif resp.status_code == 429:
                    print("已连接：触发速率限制（429）")
                    return True
                elif resp.status_code in (400, 404):
                    try:
                        body = resp.json()
                        raw = str(body.get("error") or body.get("message") or resp.text)
                    except Exception:
                        raw = resp.text
                    err_text = raw.lower()
                    keywords = [
                        "model", "not found", "unknown", "invalid", "unsupported",
                        "模型", "不存在", "未知", "无效", "不支持", "未找到"
                    ]
                    if any(k in err_text for k in keywords):
                        print("连接失败：模型不可用或不存在")
                        return False
                    return None
                else:
                    return None
            except Exception:
                return None

        client = self._get_client()
        result = _check_chat_once(client)
        if result is True:
            return True
        if result is False:
            return False

        # 重建客户端后快速重试一次
        self._recreate_client()
        client = self._get_client()
        result = _check_chat_once(client)
        if result is True:
            return True
        if result is False:
            return False

        print("连接测试失败：服务不可达或接口异常")
        return False
            
    def translate(self, text: str) -> Optional[str]:
        """翻译文本"""
        if not self.api_key:
            print("API密钥为空")
            return None
        return self._direct_translate(text)
        
    def _batch_translate_handler(self, texts: List[str], contexts: List[Dict[str, Any]]) -> List[Optional[str]]:
        """批处理翻译处理器"""
        results = []
        
        for text, context in zip(texts, contexts):
            if self.cache:
                cached_result = self.cache.get(text, context)
                if cached_result:
                    results.append(cached_result)
                    continue
                    
            result = self._direct_translate(text, context)
            
            if result and self.cache:
                self.cache.set(text, result, context)
                
            results.append(result)
            
        return results
        
    def _direct_translate(self, text: str, context: Dict[str, Any] = None) -> Optional[str]:
        """直接翻译（不使用缓存和批处理）"""
        if not self.api_key:
            print("API密钥为空")
            return None
        
        if self._cancel_event.is_set():
            return None
            
        try:
            client = self._get_client()

            if self._cancel_event.is_set():
                return None
            
            request_data = {
                "model": context.get("model", self.model_name) if context else self.model_name,
                "messages": [
                    {"role": "user", "content": text}
                ],
                "max_tokens": context.get("max_tokens", self.max_tokens) if context else self.max_tokens,
                "temperature": context.get("temperature", self.temperature) if context else self.temperature,
                "stream": False
            }
            
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=request_data
            )
            
            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    return result["choices"][0]["message"]["content"]
            else:
                # 简单重试一次
                self._recreate_client()
                client = self._get_client()
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=request_data
                )
                if response.status_code == 200:
                    result = response.json()
                    if "choices" in result and len(result["choices"]) > 0:
                        return result["choices"][0]["message"]["content"]
                else:
                    print(f"API请求失败: {response.status_code} - {response.text}")
                    
        except httpx.ConnectError:
            self._recreate_client()
            try:
                client = self._get_client()
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=request_data
                )
                if response.status_code == 200:
                    result = response.json()
                    if "choices" in result and len(result["choices"]) > 0:
                        return result["choices"][0]["message"]["content"]
            except Exception:
                return None
        except Exception as e:
            if not self._cancel_event.is_set():
                print(f"翻译请求失败: {e}")
            
        return None
        
    def translate_with_cache(self, text: str, context: Dict[str, Any] = None) -> Optional[str]:
        """带缓存的翻译"""
        if self.cache:
            cached_result = self.cache.get(text, context)
            if cached_result:
                return cached_result
                
        result = self.translate(text)
        
        if result and self.cache:
            self.cache.set(text, result, context)
            
        return result
        
    def translate_stream(self, text: str, callback=None):
        """流式翻译（增强：5次重试机制）"""
        if self._cancel_event.is_set():
            return None
        
        # ✅ 新增：重试机制（最多5次）
        max_retries = 5
        for retry_count in range(max_retries):
            try:
                client = self._get_client()
                
                if self._cancel_event.is_set():
                    return None
                
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json={
                        "model": self.model_name,
                        "messages": [
                            {"role": "user", "content": text}
                        ],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                        "stream": True
                    }
                ) as response:
                    
                    if response.status_code != 200:
                        # ✅ 状态码异常，触发重试
                        if retry_count < max_retries - 1:
                            print(f"流式请求失败 (HTTP {response.status_code})，正在重试 ({retry_count + 1}/{max_retries})...")
                            self._recreate_client()
                            time.sleep(1)
                            continue
                        else:
                            print(f"流式请求失败: {max_retries}次重试后仍失败 (HTTP {response.status_code})")
                            return None
                    
                    full_content = ""
                    for line in response.iter_lines():
                        if self._cancel_event.is_set():
                            return None
                            
                        if line.startswith("data: "):
                            data_str = line[6:]
                            
                            if data_str.strip() == "[DONE]":
                                break
                                
                            try:
                                data = json.loads(data_str)
                                if "choices" in data and len(data["choices"]) > 0:
                                    delta = data["choices"][0].get("delta", {})
                                    if "content" in delta:
                                        content = delta["content"]
                                        full_content += content
                                        
                                        if callback:
                                            callback(content)
                                            
                            except json.JSONDecodeError:
                                continue
                    
                    # ✅ 成功获取结果，返回
                    return full_content
                        
            except httpx.ConnectError as e:
                # ✅ 连接错误，触发重试
                if self._cancel_event.is_set():
                    return None
                if retry_count < max_retries - 1:
                    print(f"连接错误: {str(e)}，正在重试 ({retry_count + 1}/{max_retries})...")
                    self._recreate_client()
                    time.sleep(1)
                    continue
                else:
                    print(f"流式翻译失败: {max_retries}次重试后仍无法连接 - {str(e)}")
                    return None
            except httpx.TimeoutException as e:
                # ✅ 超时错误，触发重试
                if self._cancel_event.is_set():
                    return None
                if retry_count < max_retries - 1:
                    print(f"请求超时: {str(e)}，正在重试 ({retry_count + 1}/{max_retries})...")
                    self._recreate_client()
                    time.sleep(1)
                    continue
                else:
                    print(f"流式翻译失败: {max_retries}次重试后仍超时 - {str(e)}")
                    return None
            except Exception as e:
                # ✅ 其他异常，触发重试
                if self._cancel_event.is_set():
                    return None
                if retry_count < max_retries - 1:
                    print(f"流式翻译异常: {str(e)}，正在重试 ({retry_count + 1}/{max_retries})...")
                    self._recreate_client()
                    time.sleep(1)
                    continue
                else:
                    print(f"流式翻译失败: {max_retries}次重试后仍失败 - {str(e)}")
                    return None
        
        # ✅ 兜底：理论上不会到达这里，但为了安全起见
        return None
        
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        if self.cache:
            return self.cache.get_stats()
        return {}
        
    def clear_cache(self):
        """清空缓存"""
        if self.cache:
            self.cache.clear_all()
