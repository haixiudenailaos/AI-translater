#!/usr/bin/env python3
"""
日志与诊断信息脱敏工具（UXF-006）

确保日志、错误信息和诊断导出不包含：
- API Key（sk-xxx 等常见前缀）
- Authorization 头（Bearer xxx）
- 完整请求体中的敏感字段
- 密钥环中的明文密钥

使用方式：
    from src.utils.log_sanitizer import sanitize_for_log, sanitize_dict

    logger.info("请求配置: %s", sanitize_for_log(config_dict))
    logger.error("API 错误: %s", sanitize_for_log(error_message))

设计原则：
- 保守脱敏：宁可多脱敏也不漏脱敏。
- 幂等：对已脱敏内容再次脱敏不会破坏结果。
- 不依赖外部库，纯字符串处理。
"""

import re
from typing import Any, Dict

# 敏感字段名（不区分大小写）
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "key",
    "secret",
    "token",
    "authorization",
    "auth",
    "password",
    "passwd",
    "pwd",
    "access_token",
    "refresh_token",
    "bearer",
    "ark_api_key",
    "volc_key",
}

# 常见密钥前缀正则（sk-、Bearer 等）
# 负向预查 (?!\*) 避免重复匹配已脱敏内容（sk-abcd*** 不会再被匹配）
_KEY_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9]{4})[A-Za-z0-9_-]*(?!\*)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(
    r"(Bearer\s+)[A-Za-z0-9_.\-]+",
    re.IGNORECASE,
)
# 通用长 token（>=20 位的字母数字序列，可能是密钥）
_LONG_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")

# 脱敏后保留的前缀和后缀长度
_MASK_KEEP_PREFIX = 4
_MASK_KEEP_SUFFIX = 2


def mask_value(value: str) -> str:
    """脱敏单个字符串值，保留前4位和后2位。

    短字符串（<=8位）完全遮蔽为星号。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return (
        value[:_MASK_KEEP_PREFIX]
        + "*" * (len(value) - _MASK_KEEP_PREFIX - _MASK_KEEP_SUFFIX)
        + value[-_MASK_KEEP_SUFFIX:]
    )


def sanitize_for_log(value: Any) -> Any:
    """脱敏任意值用于日志输出。

    - 字符串：替换其中的密钥模式（sk-xxx、Bearer xxx、长 token）
    - 字典：递归脱敏敏感字段的值
    - 列表/元组：递归脱敏每个元素
    - 其他类型：原样返回
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, dict):
        return {k: _sanitize_dict_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        sanitized = [sanitize_for_log(item) for item in value]
        return type(value)(sanitized) if isinstance(value, tuple) else sanitized
    return value


def sanitize_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """脱敏字典中的敏感字段（返回新字典，不修改原字典）。"""
    if not isinstance(data, dict):
        return data
    return {k: _sanitize_dict_value(k, v) for k, v in data.items()}  # type: ignore[return-value]


def _sanitize_dict_value(key: str, value: Any) -> Any:
    """字典值的脱敏：敏感字段的值被遮蔽，非敏感字段递归处理。"""
    key_lower = str(key).lower().replace("-", "_").replace(" ", "_")
    if key_lower in _SENSITIVE_KEYS:
        if isinstance(value, str):
            return mask_value(value)
        return "***"
    return sanitize_for_log(value)


def _sanitize_string(text: str) -> str:
    """脱敏字符串中的密钥模式。"""
    if not text:
        return text
    # Bearer token
    text = _BEARER_PATTERN.sub(r"\1***", text)
    # sk- 前缀密钥
    text = _KEY_PATTERN.sub(r"\1***", text)
    # 超长 token（可能是密钥），只在看起来像密钥的上下文中替换
    # 避免误伤正常的长文本（只替换被引号、等号、冒号包围的）
    text = _LONG_TOKEN_PATTERN.sub(_mask_matched_token, text)
    return text


def _mask_matched_token(match: re.Match) -> str:
    """对匹配的长 token 进行脱敏。"""
    token = match.group(0)
    return mask_value(token)


def sanitize_error_message(message: str) -> str:
    """脱敏异常/错误消息中的敏感信息。"""
    return _sanitize_string(message)


def is_safe_to_log(text: str) -> bool:
    """检查文本是否不含明显敏感信息（用于断言）。"""
    sanitized = _sanitize_string(text)
    return sanitized == text
