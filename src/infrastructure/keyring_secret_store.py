#!/usr/bin/env python3
"""
基于 keyring 的密钥存储实现（P1-2）

将 utils/secure_storage.py 的函数式 API 包装为对象式 SecretStore 协议实现。
优先使用系统密钥环，不可用时降级到环境变量（仅会话级）。

这是 SecretStore 协议的具体实现，由 bootstrap.py 注入到 ConfigManager。
测试时可注入内存替身（FakeSecretStore），无需真实 keyring。
"""

from ..domain.secret import StorageStatus
from ..utils.secure_storage import (
    delete_key,
    get_key,
    store_key,
)


class KeyringSecretStore:
    """SecretStore 协议的具体实现：基于 keyring + 环境变量降级"""

    def store(self, identifier: str, key: str) -> StorageStatus:
        """存储密钥，返回 StorageStatus"""
        return store_key(identifier, key)

    def retrieve(self, identifier: str) -> str:
        """读取密钥，不存在返回空字符串"""
        return get_key(identifier)

    def delete(self, identifier: str) -> bool:
        """删除密钥，幂等"""
        return delete_key(identifier)
