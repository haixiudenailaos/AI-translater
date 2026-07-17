#!/usr/bin/env python3
"""
BUG-009 / R2-BUG-002：安全密钥存储工具

优先使用系统密钥环（keyring），不可用时降级到环境变量（仅会话级，需明确告知用户）。
配置文件中不再保存完整 API Key，只保存提供商名称等引用信息。

R2-BUG-002 修复要点：
- 区分 PERSISTED / SESSION_ONLY / FAILED 三种状态，不再仅返回布尔值
- 只有持久化写入成功并回读一致后才视为成功
- 会话级降级必须明确提示用户"重启后失效"
- 使用 keyring 后端的真实优先级或模块类型判断可用性
- 不在日志中写入 Key 明文
"""

import os

from ..domain.secret import StorageStatus
from .logger import get_logger

logger = get_logger(__name__)

# 密钥环服务名
_KEYRING_SERVICE = "AI-Translator"

# 是否可用 keyring（延迟探测，避免每次调用都尝试导入）
_keyring_available: bool | None = None


__all__ = [
    "StorageStatus",
    "store_key",
    "store_key_bool",
    "get_key",
    "delete_key",
    "mask_key",
]


# 视为不可用的 keyring 后端名称集合
_UNAVAILABLE_BACKEND_NAMES = {
    "NullKeyring",
    "fail.Keyring",
    "Keyring",  # 失败后端的 __name__ 通常只是 "Keyring"，需进一步判断
}


def _detect_keyring() -> bool:
    """检测 keyring 库是否可用且有可用后端。

    R2-BUG-002：使用后端的真实优先级或模块类型判断可用性，
    不再仅依赖 type(backend).__name__。
    """
    global _keyring_available
    if _keyring_available is not None:
        return _keyring_available
    try:
        import keyring as _kr

        backend = _kr.get_keyring()

        # 1) 名称判断
        backend_name = type(backend).__name__
        if backend_name in _UNAVAILABLE_BACKEND_NAMES:
            _keyring_available = False
            logger.info(
                "keyring 后端 %s 视为不可用，将降级使用环境变量",
                backend_name,
            )
            return False

        # 2) 模块判断：失败后端通常来自 keyring.backends.fail
        backend_module = type(backend).__module__ or ""
        if "fail" in backend_module.lower() or "null" in backend_module.lower():
            _keyring_available = False
            logger.info(
                "keyring 后端模块 %s 视为不可用，将降级使用环境变量",
                backend_module,
            )
            return False

        # 3) 优先级判断：可用后端 priority 应大于 0
        priority = getattr(backend, "priority", 1)
        if callable(priority):
            try:
                priority = priority()
            except Exception:
                priority = 1
        if not priority or priority <= 0:
            _keyring_available = False
            logger.info(
                "keyring 后端优先级 %s 视为不可用，将降级使用环境变量",
                priority,
            )
            return False

        _keyring_available = True
    except Exception as e:
        logger.info("keyring 不可用，将降级使用环境变量: %s", e)
        _keyring_available = False
    return _keyring_available


def _env_var_name(identifier: str) -> str:
    """将标识符转换为环境变量名。"""
    return "AI_TRANSLATOR_KEY_" + identifier.replace(":", "_").replace("-", "_").upper()


def store_key(identifier: str, key: str) -> StorageStatus:
    """存储密钥。

    R2-BUG-002：返回 StorageStatus 而非布尔值。

    Args:
        identifier: 密钥标识符，如 "provider:siliconflow"
        key: 密钥明文

    Returns:
        StorageStatus.PERSISTED: 已写入密钥环并回读一致
        StorageStatus.SESSION_ONLY: 仅写入环境变量（重启后失效）
        StorageStatus.FAILED: 写入失败
    """
    key = (key or "").strip()
    if not key:
        # 空密钥视为删除
        delete_key(identifier)
        return StorageStatus.PERSISTED  # 删除视为持久化成功

    if _detect_keyring():
        try:
            import keyring as _kr

            _kr.set_password(_KEYRING_SERVICE, identifier, key)
            # R2-BUG-002：回读验证一致才视为持久化成功
            read_back = _kr.get_password(_KEYRING_SERVICE, identifier)
            if read_back and read_back.strip() == key:
                # 持久化成功后清理可能残留的旧会话覆盖。
                os.environ.pop(_env_var_name(identifier), None)
                logger.debug("密钥已存入密钥环: %s", identifier)
                return StorageStatus.PERSISTED
            logger.warning(
                "密钥环写入后回读不一致 [%s]，降级到环境变量",
                identifier,
            )
        except Exception as e:
            logger.warning(
                "密钥环写入失败，降级到环境变量 [%s]: %s",
                identifier,
                e,
            )

    # 降级：环境变量（仅当前进程有效，重启后需重新设置或由启动脚本注入）
    try:
        os.environ[_env_var_name(identifier)] = key
    except Exception as e:
        logger.error("环境变量写入失败 [%s]: %s", identifier, e)
        return StorageStatus.FAILED

    logger.debug("密钥已存入环境变量（重启后失效）: %s", identifier)
    return StorageStatus.SESSION_ONLY


def store_key_bool(identifier: str, key: str) -> bool:
    """向后兼容的布尔返回包装。

    PERSISTED 和 SESSION_ONLY 视为 True，FAILED 视为 False。
    新代码应直接使用 store_key。
    """
    return store_key(identifier, key) != StorageStatus.FAILED


def get_key(identifier: str) -> str:
    """读取密钥；会话覆盖优先于密钥环中的持久化值。

    Args:
        identifier: 密钥标识符

    Returns:
        密钥明文，不存在时返回空字符串
    """
    # keyring 写入失败时 store_key() 会把新值保存为会话覆盖。
    # 必须优先读取它，否则同一进程会继续使用 keyring 中的旧值。
    session_value = os.environ.get(_env_var_name(identifier), "").strip()
    if session_value:
        return session_value

    if _detect_keyring():
        try:
            import keyring as _kr

            value = _kr.get_password(_KEYRING_SERVICE, identifier)
            if value:
                return value.strip()
        except Exception as e:
            logger.warning(
                "密钥环读取失败，降级到环境变量 [%s]: %s",
                identifier,
                e,
            )

    return ""


def delete_key(identifier: str) -> bool:
    """从密钥环删除密钥；同时清理环境变量。

    Args:
        identifier: 密钥标识符

    Returns:
        True 表示删除成功或密钥不存在
    """
    if _detect_keyring():
        try:
            import keyring as _kr

            _kr.delete_password(_KEYRING_SERVICE, identifier)
        except Exception:
            # 密钥不存在也会抛异常，忽略
            pass

    # 清理环境变量
    os.environ.pop(_env_var_name(identifier), None)
    return True


def mask_key(key: str) -> str:
    """脱敏密钥用于日志输出，仅保留前4位和后2位。

    Args:
        key: 完整密钥

    Returns:
        脱敏后的字符串，如 "sk-1***xy"
    """
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 6) + key[-2:]
