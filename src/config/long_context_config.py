#!/usr/bin/env python3
"""超长上下文翻译的应用配置段（``app_config.json`` 的 ``long_context``）。

职责单一：定义默认值，并把磁盘上不可信的 JSON 规范化成可诊断、幂等的配置段。
本模块不依赖 Tk、队列或 API 客户端；预算算术在
:mod:`src.domain.translation_policy`，此处只负责"配置长什么样"。

配置格式::

    {
      "long_context": {
        "schema_version": 1,
        "context_window_tokens": 32768
      }
    }

``context_window_tokens`` 是**用户给超长模式的总预算**（输入 + 输出合计），
与 API 配置中同名字段的"当前模型容量"语义不同，两者不可混用。
"""

import copy
from typing import Any, Dict, Final

from ..domain.translation_policy import (
    LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS,
    coerce_context_window_tokens,
)
from ..utils.logger import get_logger

logger = get_logger(__name__)

LONG_CONTEXT_CONFIG_KEY: Final = "long_context"
LONG_CONTEXT_SCHEMA_VERSION: Final = 1

DEFAULT_LONG_CONTEXT_CONFIG: Final[Dict[str, Any]] = {
    "schema_version": LONG_CONTEXT_SCHEMA_VERSION,
    "context_window_tokens": LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS,
}


def default_long_context_config() -> Dict[str, Any]:
    """返回默认配置段的独立副本。"""
    return copy.deepcopy(DEFAULT_LONG_CONTEXT_CONFIG)


def normalize_long_context_config(existing: object) -> Dict[str, Any]:
    """把磁盘上的 ``long_context`` 段规范化为当前 schema。

    加载与保存共用本入口，保证深层默认合并与幂等（``normalize(normalize(x))``
    与 ``normalize(x)`` 相等）。

    规则：

    - 缺字段 / 非映射 / 损坏值：回退到默认值，并记录可诊断日志。
    - 1 / 0 / 负数 / 小数 / 布尔值 / 非数字字符串：不是合法 token 预算，
      拒绝并回退默认值（不通过 ``int(1.5)`` 静默截断）。允许
      ``"131072"`` 这类纯数字字符串，因为它来自 Tk 输入框且无歧义。
    - ``schema_version`` 比当前更新：本程序不认识该版本，保留原值并保留
      已知字段的规范化结果，不盲目重写成旧版本号（避免破坏较新配置）。
    - 未知字段：原样保留，避免用户/后续版本的数据在保存时被丢弃。
    """
    if not isinstance(existing, dict):
        if existing is not None:
            logger.warning(
                "long_context 配置段类型异常（%s），已回退默认值", type(existing).__name__
            )
        return default_long_context_config()

    normalized: Dict[str, Any] = default_long_context_config()

    raw_schema = existing.get("schema_version")
    if isinstance(raw_schema, bool) or not isinstance(raw_schema, int) or raw_schema <= 0:
        if raw_schema is not None:
            logger.warning("long_context.schema_version 无效（%r），已回退默认值", raw_schema)
    elif raw_schema > LONG_CONTEXT_SCHEMA_VERSION:
        # 较新 schema：保留版本号，继续按已知字段尽力规范化。
        normalized["schema_version"] = raw_schema
    else:
        normalized["schema_version"] = raw_schema

    raw_tokens = existing.get("context_window_tokens")
    tokens = coerce_context_window_tokens(raw_tokens)
    if tokens is None:
        if raw_tokens is not None:
            logger.warning(
                "long_context.context_window_tokens 无效（%r），已回退默认值 %d",
                raw_tokens,
                LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS,
            )
        else:
            normalized["context_window_tokens"] = LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS
    else:
        normalized["context_window_tokens"] = tokens

    # 保留未知字段，避免较新版本写入的数据被本次保存抹掉。
    for key, value in existing.items():
        if key not in normalized:
            normalized[key] = copy.deepcopy(value)

    return normalized


def with_long_context_config(app_config: Dict[str, Any]) -> Dict[str, Any]:
    """返回已规范化 ``long_context`` 段的应用配置副本。

    供 ``ConfigManager`` 在处理不可信磁盘配置后统一收敛；调用方不得自行
    拼接该段。输入不被修改。
    """
    result = dict(app_config)
    result[LONG_CONTEXT_CONFIG_KEY] = normalize_long_context_config(
        app_config.get(LONG_CONTEXT_CONFIG_KEY)
    )
    return result


def read_context_window_tokens(app_config: Dict[str, Any]) -> int:
    """从应用配置读取用户总预算；缺失或损坏时返回默认值。"""
    section = app_config.get(LONG_CONTEXT_CONFIG_KEY)
    if isinstance(section, dict):
        tokens = coerce_context_window_tokens(section.get("context_window_tokens"))
        if tokens is not None:
            return tokens
    return LONG_CONTEXT_DEFAULT_CONTEXT_WINDOW_TOKENS


__all__ = [
    "DEFAULT_LONG_CONTEXT_CONFIG",
    "LONG_CONTEXT_CONFIG_KEY",
    "LONG_CONTEXT_SCHEMA_VERSION",
    "default_long_context_config",
    "normalize_long_context_config",
    "read_context_window_tokens",
    "with_long_context_config",
]
