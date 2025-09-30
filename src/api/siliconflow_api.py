#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
硅基流动API接口模块
增强版本：支持流式翻译、智能缓存和批处理优化
"""

import httpx
import json
import time
import threading
from typing import Dict, Any, Optional, List, Callable
from ..core.smart_cache import SmartCache
from ..core.batch_processor import BatchProcessor, get_batch_processor

class SiliconFlowAPI:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.base_url = config.get("base_url", "https://api.siliconflow.cn/v1")
        self.api_key = config.get("api_key", "").strip()
        self.model_name = config.get("model_name", "deepseek-ai/DeepSeek-V3.1-Terminus")
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
        
        # 设置请求头，确保API密钥不为空
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
            # 关闭旧客户端
            if self._current_client:
                try:
                    self._current_client.close()
                except:
                    pass
            # 创建新客户端，配置连接池与超时
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
        """测试API连接（快速且避免误判）
        简化策略：
        - 以最小化的 /chat/completions 请求为准（max_tokens=1, temperature=0），3秒超时
        - 200 且包含有效 choices 视为成功（不强制校验返回的 model 字段，避免别名/映射导致误判）
        - 401/403 明确为鉴权失败；400/404 若错误信息包含 model 不存在/未知，判定为模型不可用
        - 其它错误快速重试一次后失败
        """
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
                        # 返回 200 但解析失败，通常也表示服务正常
                        return True
                elif resp.status_code in (401, 403):
                    print("连接失败：鉴权错误（API Key 可能无效或权限不足）")
                    return False
                elif resp.status_code == 429:
                    # 触发限速，说明服务与鉴权正常，视为连接成功
                    print("已连接：触发速率限制（429）")
                    return True
                elif resp.status_code in (400, 404):
                    # 检查错误消息中是否包含模型相关提示（同时支持中英关键词）
                    try:
                        body = resp.json()
                        raw = str(body.get("error") or body.get("message") or resp.text)
                    except Exception:
                        raw = resp.text
                    err_text = raw.lower()
                    # 英文与中文常见提示关键词
                    keywords = [
                        "model", "not found", "unknown", "invalid", "unsupported",
                        "模型", "不存在", "未知", "无效", "不支持", "未找到"
                    ]
                    if any(k in err_text for k in keywords):
                        print("连接失败：模型不可用或不存在")
                        return False
                    # 无明显模型关键词，无法明确判定
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
            # 检查缓存
            if self.cache:
                cached_result = self.cache.get(text, context)
                if cached_result:
                    results.append(cached_result)
                    continue
                    
            # 执行翻译
            result = self._direct_translate(text, context)
            
            # 保存到缓存
            if result and self.cache:
                self.cache.set(text, result, context)
                
            results.append(result)
            
        return results
        
    def _direct_translate(self, text: str, context: Dict[str, Any] = None) -> Optional[str]:
        """直接翻译（不使用缓存和批处理）"""
        if not self.api_key:
            print("API密钥为空")
            return None
        
        # 检查是否已被取消
        if self._cancel_event.is_set():
            return None
            
        try:
            client = self._get_client()

            # 再次检查取消状态
            if self._cancel_event.is_set():
                return None
            
            # 构建请求参数
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
                # 简单重试一次：重建客户端并重试
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
            # 连接被取消或中断，尝试重建后再试一次
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
            
        return None
        
    def translate_with_cache(self, text: str, context: Dict[str, Any] = None) -> Optional[str]:
        """带缓存的翻译"""
        # 检查缓存
        if self.cache:
            cached_result = self.cache.get(text, context)
            if cached_result:
                return cached_result
                
        # 执行翻译
        result = self.translate(text)
        
        # 保存到缓存
        if result and self.cache:
            self.cache.set(text, result, context)
            
        return result
        
    def translate_batch(self, texts: List[str], contexts: List[Dict[str, Any]] = None, 
                       priority: int = 0) -> List[Optional[str]]:
        """批处理翻译"""
        if not self.batch_processor:
            # 如果没有启用批处理，逐个翻译
            results = []
            for i, text in enumerate(texts):
                context = contexts[i] if contexts and i < len(contexts) else None
                result = self.translate_with_cache(text, context)
                results.append(result)
            return results
            
        # 使用批处理
        futures = []
        for i, text in enumerate(texts):
            context = contexts[i] if contexts and i < len(contexts) else {}
            future = self.batch_processor.submit_request(text, context, priority=priority)
            futures.append(future)
            
        # 等待所有结果
        results = []
        for future in futures:
            try:
                result = future.result(timeout=60)  # 60秒超时
                results.append(result)
            except Exception as e:
                print(f"批处理翻译失败: {e}")
                results.append(None)
                
        return results
        
    def translate_stream_enhanced(self, text: str, callback: Callable[[str], None] = None, 
                                 context: Dict[str, Any] = None, stream_id: str = None) -> Optional[str]:
        """增强的流式翻译"""
        if not self.enable_stream:
            # 如果未启用流式，使用普通翻译
            result = self.translate_with_cache(text, context)
            if callback and result:
                callback(result)
            return result
            
        # 检查缓存
        if self.cache:
            cached_result = self.cache.get(text, context)
            if cached_result:
                # 模拟流式输出缓存结果
                if callback:
                    self._simulate_stream_output(cached_result, callback)
                return cached_result
                
        # 注册回调
        if stream_id and callback:
            self.stream_callbacks[stream_id] = callback
            
        # 执行流式翻译
        result = self.translate_stream(text, callback)
        
        # 保存到缓存
        if result and self.cache:
            self.cache.set(text, result, context)
            
        # 清理回调
        if stream_id and stream_id in self.stream_callbacks:
            del self.stream_callbacks[stream_id]
            
        return result
        
    def _simulate_stream_output(self, text: str, callback: Callable[[str], None], 
                               chunk_size: int = 3, delay: float = 0.05):
        """模拟流式输出（用于缓存结果）"""
        def stream_worker():
            for i in range(0, len(text), chunk_size):
                if self._cancel_event.is_set():
                    break
                chunk = text[i:i + chunk_size]
                callback(chunk)
                time.sleep(delay)
                
        threading.Thread(target=stream_worker, daemon=True).start()
        
    def cancel_stream(self, stream_id: str):
        """取消特定的流式翻译"""
        if stream_id in self.stream_callbacks:
            del self.stream_callbacks[stream_id]
            
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        if self.cache:
            return self.cache.get_stats()
        return {}
        
    def get_batch_stats(self) -> Dict[str, Any]:
        """获取批处理统计信息"""
        if self.batch_processor:
            return self.batch_processor.get_stats()
        return {}
        
    def clear_cache(self):
        """清空缓存"""
        if self.cache:
            self.cache.clear_all()
            
    def optimize_cache(self):
        """优化缓存"""
        if self.cache:
            self.cache.optimize_cache()
            
    def flush_batch(self):
        """刷新批处理队列"""
        if self.batch_processor:
            self.batch_processor.flush_pending()
            
    def configure_cache(self, **kwargs):
        """配置缓存参数"""
        if self.cache:
            # 重新创建缓存实例
            cache_config = self.config.get("cache_config", {})
            cache_config.update(kwargs)
            self.cache = SmartCache(
                max_memory_size=cache_config.get("max_memory_size", 1000),
                max_file_size=cache_config.get("max_file_size", 10000),
                cache_dir=cache_config.get("cache_dir"),
                ttl_hours=cache_config.get("ttl_hours", 24)
            )
            
    def configure_batch(self, **kwargs):
        """配置批处理参数"""
        if self.batch_processor:
            self.batch_processor.configure(**kwargs)
            
    def get_enhanced_stats(self) -> Dict[str, Any]:
        """获取增强功能统计信息"""
        stats = {
            "cache_enabled": self.enable_cache,
            "batch_enabled": self.enable_batch,
            "stream_enabled": self.enable_stream,
            "active_streams": len(self.stream_callbacks)
        }
        
        if self.cache:
            stats["cache_stats"] = self.get_cache_stats()
            
        if self.batch_processor:
            stats["batch_stats"] = self.get_batch_stats()
            
        return stats
        
        # 检查是否已被取消
        if self._cancel_event.is_set():
            return None
            
        try:
            with httpx.Client(timeout=60.0) as client:
                self._current_client = client
                
                # 再次检查取消状态
                if self._cancel_event.is_set():
                    return None
                
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json={
                        "model": self.model_name,
                        "messages": [
                            {"role": "user", "content": text}
                        ],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                        "stream": False
                    }
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if "choices" in result and len(result["choices"]) > 0:
                        return result["choices"][0]["message"]["content"]
                else:
                    print(f"API请求失败: {response.status_code} - {response.text}")
                    
        except httpx.ConnectError:
            # 连接被取消或中断
            return None
        except Exception as e:
            if not self._cancel_event.is_set():
                print(f"翻译请求失败: {e}")
        finally:
            self._current_client = None
            
        return None
        
    def translate_stream(self, text: str, callback=None):
        """流式翻译"""
        # 检查是否已被取消
        if self._cancel_event.is_set():
            return None
            
        try:
            client = self._get_client()
            
            # 再次检查取消状态
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
                    # 重建客户端后重试一次
                    self._recreate_client()
                    client = self._get_client()
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
                    ) as response2:
                        if response2.status_code != 200:
                            print(f"流式请求失败: {response2.status_code}")
                            return None
                        response = response2
                
                full_content = ""
                for line in response.iter_lines():
                    # 在每次迭代中检查取消状态
                    if self._cancel_event.is_set():
                        return None
                        
                    if line.startswith("data: "):
                        data_str = line[6:]  # 移除 "data: " 前缀
                        
                        if data_str.strip() == "[DONE]":
                            break
                            
                        try:
                            data = json.loads(data_str)
                            if "choices" in data and len(data["choices"]) > 0:
                                delta = data["choices"][0].get("delta", {})
                                if "content" in delta:
                                    content = delta["content"]
                                    full_content += content
                                    
                                    # 调用回调函数
                                    if callback:
                                        callback(content)
                                        
                        except json.JSONDecodeError:
                            continue
                            
                return full_content
                    
        except httpx.ConnectError:
            # 连接被取消或中断
            return None
        except Exception as e:
            if not self._cancel_event.is_set():
                print(f"流式翻译失败: {e}")
        finally:
            self._current_client = None
            
        return None