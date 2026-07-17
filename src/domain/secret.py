#!/usr/bin/env python3
"""
密钥存储领域模型

定义密钥存储状态枚举和保存结果，供 Application 层和 Infrastructure 层共享。

设计要点：
- StorageStatus 表达密钥持久化的三种状态（持久化/仅会话/失败）。
- SecretSaveResult 聚合密钥状态、配置文件状态和错误信息，供 UI 精确提示。
- SecretSaveResult 实现 __bool__，向后兼容旧布尔返回值调用方：
  PERSISTED / SESSION_ONLY 视为 True（允许继续会话），FAILED 视为 False。
- 日志和错误信息不得包含密钥明文。

P1-2：修复 API Key 未持久化时仍提示保存成功。
"""

from dataclasses import dataclass
from enum import Enum


class StorageStatus(str, Enum):
    """密钥存储结果状态

    PERSISTED：已持久化到密钥环，重启后仍可读取。
    SESSION_ONLY：仅写入环境变量，重启后失效（需明确告知用户）。
    FAILED：写入失败。
    """

    PERSISTED = "persisted"
    SESSION_ONLY = "session_only"
    FAILED = "failed"


@dataclass
class SecretSaveResult:
    """密钥与配置保存结果（P1-2）

    聚合密钥持久化状态、配置文件保存状态和错误信息，替代旧的布尔返回值。
    UI 据此精确提示：
    - secret_status == FAILED：配置保存整体失败，不关闭设置窗口。
    - secret_status == SESSION_ONLY：允许继续会话，但提示"密钥未持久化，重启后需重新输入"。
    - secret_status == PERSISTED：正常成功。

    向后兼容：__bool__ 使旧调用方 `if save_api_config(...)` 继续工作。
    PERSISTED / SESSION_ONLY → True，FAILED → False。

    Attributes:
        secret_status: 密钥持久化状态。
        config_saved: 配置文件（JSON）是否成功写入。
        error_message: 失败原因（不含密钥明文），成功时为空。
        provider: 本次保存的提供商名称（用于日志，不含密钥）。
    """

    secret_status: StorageStatus = StorageStatus.PERSISTED
    config_saved: bool = True
    error_message: str = ""
    provider: str = ""

    @property
    def persisted(self) -> bool:
        """密钥已持久化到密钥环（重启后可读取）"""
        return self.secret_status == StorageStatus.PERSISTED

    @property
    def session_only(self) -> bool:
        """密钥仅会话级保存（重启后失效）"""
        return self.secret_status == StorageStatus.SESSION_ONLY

    @property
    def failed(self) -> bool:
        """密钥保存失败"""
        return self.secret_status == StorageStatus.FAILED

    @property
    def user_message(self) -> str:
        """面向用户的提示消息（P1-2 验收标准）"""
        if self.failed:
            return "API Key 保存失败，设置未保存"
        if self.session_only:
            return "设置已保存，但密钥未持久化，重启后需重新输入"
        if not self.config_saved:
            return "密钥已保存，但配置文件写入失败，请重试"
        return "设置已保存"

    def __bool__(self) -> bool:
        """向后兼容布尔判断。

        PERSISTED / SESSION_ONLY 视为 True（允许继续当前会话），
        FAILED 视为 False（配置保存整体失败）。
        配置文件写入失败也视为 False。
        """
        return self.secret_status != StorageStatus.FAILED and self.config_saved
