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
import logging
from typing import Dict, Any, Optional, List, Callable
from ..core.smart_cache import SmartCache
from ..core.batch_processor import BatchProcessor, get_batch_processor


logging.basicConfig(level=logging.DEBUG, format='[DEBUG] %(message)s')
logger = logging.getLogger(__name__)

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

        # HTTP连接池与超时配置（优化性能）
        http_limits = config.get("http_limits", {})
        self._max_keepalive = http_limits.get("max_keepalive_connections", 20)  # ✅ 增加保活连接数
        self._max_connections = http_limits.get("max_connections", 50)  # ✅ 增加最大连接数
        self._timeout = config.get("http_timeout", 90.0)  # ✅ 增加超时时间
        self._keepalive_expiry = http_limits.get("keepalive_expiry", 300.0)  # ✅ 新增：保活过期时间（5分钟）

        # ✅ 新增：心跳保活机制
        self._heartbeat_enabled = config.get("enable_heartbeat", True)
        self._heartbeat_interval = config.get("heartbeat_interval", 60)  # 60秒心跳一次
        self._heartbeat_thread = None
        self._heartbeat_stop_event = threading.Event()

        # 初始化持久客户端
        self._recreate_client()
        
        # ✅ 启动心跳保活线程
        if self._heartbeat_enabled:
            self._start_heartbeat()
        
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
    
    def close(self):
        """关闭 API 实例，释放资源
        
        ✅ 新增：确保正确释放连接池和心跳线程
        """
        # 停止心跳线程
        self._stop_heartbeat()
        
        # 关闭客户端
        if self._current_client:
            try:
                self._current_client.close()
            except:
                pass
            self._current_client = None
        
        # 关闭批处理器
        if self.batch_processor:
            try:
                self.batch_processor.shutdown()
            except:
                pass

    def _recreate_client(self):
        """重建持久HTTP客户端（连接池）
        
        ✅ 性能优化：
        - 增加连接池大小，提升并发能力
        - 配置保活过期时间，减少连接重建开销
        - 增加超时时间，适应流式翻译场景
        """
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
                max_connections=self._max_connections,
                keepalive_expiry=self._keepalive_expiry  # ✅ 新增：保活过期时间
            )
            # ✅ 配置传输层参数，优化性能
            transport = httpx.HTTPTransport(
                retries=1,  # 自动重试1次
                limits=limits
            )
            self._current_client = httpx.Client(
                timeout=self._timeout,
                transport=transport,
                http2=True  # ✅ 启用HTTP/2支持（如果服务器支持）
            )
        except Exception as e:
            print(f"重建HTTP客户端失败: {e}")
            # 降级：使用基础配置
            limits = httpx.Limits(
                max_keepalive_connections=self._max_keepalive,
                max_connections=self._max_connections
            )
            self._current_client = httpx.Client(timeout=self._timeout, limits=limits)
    
    def _start_heartbeat(self):
        """启动心跳保活线程
        
        ✅ 性能优化：定期发送轻量级请求，保持连接活跃
        - 避免连接超时被关闭
        - 减少重新建立连接的开销
        - 提升流式翻译的响应速度
        """
        def heartbeat_worker():
            while not self._heartbeat_stop_event.is_set():
                try:
                    # 等待心跳间隔
                    if self._heartbeat_stop_event.wait(timeout=self._heartbeat_interval):
                        break  # 收到停止信号
                    
                    # 发送心跳请求（轻量级）
                    if self._current_client and not self._cancel_event.is_set():
                        try:
                            # 使用极小的请求来保持连接
                            resp = self._current_client.post(
                                f"{self.base_url}/chat/completions",
                                headers=self.headers,
                                json={
                                    "model": self.model_name,
                                    "messages": [{"role": "user", "content": "ping"}],
                                    "max_tokens": 1,
                                    "temperature": 0.0,
                                    "stream": False
                                },
                                timeout=3.0  # 快速超时
                            )
                            # 不关心响应内容，只要连接保持活跃即可
                        except Exception:
                            # 心跳失败，尝试重建客户端
                            self._recreate_client()
                except Exception:
                    pass
        
        # 启动心跳线程
        self._heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
        self._heartbeat_thread.start()
    
    def _stop_heartbeat(self):
        """停止心跳保活线程"""
        if self._heartbeat_thread:
            self._heartbeat_stop_event.set()
            self._heartbeat_thread.join(timeout=2.0)

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
            
    def vision_query(self, image_base64: str, mime_type: str, prompt: str, model_override: str = None) -> Optional[str]:
        """使用视觉模型分析图片内容

        Args:
            image_base64: 图片的base64编码（不含data:前缀）
            mime_type: 图片MIME类型，如 image/png
            prompt: 发送给视觉模型的提示词
            model_override: 可选，覆盖默认模型名称（用于指定视觉模型）

        Returns:
            模型的文本响应，失败返回None
        """
        logger.debug(f"[vision_query] ====== 开始视觉查询 ======")
        logger.debug(f"[vision_query] 参数: mime_type={mime_type}, base64长度={len(image_base64)}, model_override={model_override}")
        logger.debug(f"[vision_query] Prompt: {prompt[:100]}..." if len(prompt) > 100 else f"[vision_query] Prompt: {prompt}")
        
        if not self.api_key:
            logger.error("[vision_query] API密钥为空")
            print("API密钥为空")
            return None

        if self._cancel_event.is_set():
            logger.debug("[vision_query] 请求被取消")
            return None

        try:
            logger.debug(f"[vision_query] 获取HTTP客户端...")
            client = self._get_client()

            if self._cancel_event.is_set():
                logger.debug("[vision_query] 请求被取消（在获取客户端后）")
                return None

            # 构建多模态消息
            logger.debug(f"[vision_query] 构建多模态消息...")
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}",
                                "detail": "high"
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ]
            logger.debug(f"[vision_query] 消息结构: role={messages[0]['role']}, content类型数={len(messages[0]['content'])}")

            request_data = {
                "model": model_override or self.model_name,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": 0.3,
                "stream": False
            }
            logger.debug(f"[vision_query] 请求数据: model={request_data['model']}, max_tokens={request_data['max_tokens']}, temperature={request_data['temperature']}")
            logger.debug(f"[vision_query] 请求URL: {self.base_url}/chat/completions")

            response = client.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=request_data
            )

            logger.debug(f"[vision_query] 响应状态码: {response.status_code}")

            if response.status_code == 200:
                result = response.json()
                logger.debug(f"[vision_query] 响应JSON: {json.dumps(result, ensure_ascii=False)[:200]}...")
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0]["message"]["content"]
                    logger.debug(f"[vision_query] 成功获取响应内容: {repr(content[:100])}...")
                    return content
                else:
                    logger.warning(f"[vision_query] 响应中没有choices或choices为空: {result}")
            else:
                logger.error(f"[vision_query] 视觉查询失败: status={response.status_code}, text={response.text}")
                print(f"视觉查询失败: {response.status_code} - {response.text}")

        except Exception as e:
            logger.error(f"[vision_query] 视觉查询请求异常: {type(e).__name__}: {e}", exc_info=True)
            if not self._cancel_event.is_set():
                print(f"视觉查询请求失败: {e}")

        logger.debug(f"[vision_query] ====== 视觉查询结束，返回None ======")
        return None

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
        """流式翻译（增强：5次重试机制）"""
        # 检查是否已被取消
        if self._cancel_event.is_set():
            return None
        
        # ✅ 新增：重试机制（最多5次）
        max_retries = 5
        for retry_count in range(max_retries):
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
                        # ✅ 状态码异常，触发重试
                        if retry_count < max_retries - 1:
                            print(f"流式请求失败 (HTTP {response.status_code})，正在重试 ({retry_count + 1}/{max_retries})...")
                            self._recreate_client()  # 重建客户端
                            time.sleep(1)  # 短暂延迟后重试
                            continue
                        else:
                            print(f"流式请求失败: {max_retries}次重试后仍失败 (HTTP {response.status_code})")
                            return None
                    
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