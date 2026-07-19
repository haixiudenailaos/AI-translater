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

from dataclasses import dataclass, field
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
        """任一必要持久化步骤失败。"""
        return not self.succeeded

    @property
    def succeeded(self) -> bool:
        """密钥状态与配置文件都成功保存。"""
        return self.secret_status != StorageStatus.FAILED and self.config_saved

    @property
    def user_message(self) -> str:
        """面向用户的提示消息（P1-2 验收标准）"""
        if self.secret_status == StorageStatus.FAILED:
            return "API Key 保存失败，设置未保存"
        if not self.config_saved:
            return "密钥已保存，但配置文件写入失败，请重试"
        if self.session_only:
            return "设置已保存，但密钥未持久化，重启后需重新输入"
        return "设置已保存"

    def __bool__(self) -> bool:
        """向后兼容布尔判断。

        PERSISTED / SESSION_ONLY 视为 True（允许继续当前会话），
        FAILED 视为 False（配置保存整体失败）。
        配置文件写入失败也视为 False。
        """
        return self.succeeded


@dataclass
class ConfigSaveResult:
    """ENG-1：聚合配置各部分保存结果。

    收集 API 配置、应用设置、术语表等不同子系统的保存状态，
    供关闭流程据此决定是否阻断退出、提示用户重试或继续不保存退出。

    设计要点：
    - 任一关键部分失败即整体 failed（关闭应阻断或要求显式确认）。
    - SESSION_ONLY 不视为失败，但 ``session_only`` 标记供 UI 提示。
    - ``user_message`` 提供面向用户的可操作摘要（脱敏，不含密钥明文）。
    - ``__bool__`` 与 ``failed`` 取反，向后兼容旧 ``if save_config(...)`` 调用。
    """

    api: SecretSaveResult = field(default_factory=lambda: SecretSaveResult())
    app_config_saved: bool = True
    glossary_saved: bool = True
    app_config_error: str = ""
    glossary_error: str = ""

    @property
    def failed(self) -> bool:
        """任一关键部分失败即整体失败"""
        return not bool(self.api) or not self.app_config_saved or not self.glossary_saved

    @property
    def session_only(self) -> bool:
        """密钥仅会话级保存（不阻断退出，但需告知用户）"""
        return self.api.session_only

    @property
    def user_message(self) -> str:
        if self.failed:
            parts: list[str] = []
            if self.api.failed:
                api_msg = self.api.user_message
                # 附上具体错误原因（已脱敏，不含密钥明文）
                if self.api.error_message and self.api.error_message not in api_msg:
                    api_msg = f"{api_msg}（{self.api.error_message}）"
                parts.append(f"API 配置: {api_msg}")
            if not self.app_config_saved:
                parts.append(f"应用配置: {self.app_config_error or '保存失败'}")
            if not self.glossary_saved:
                parts.append(f"术语表: {self.glossary_error or '保存失败'}")
            return "配置保存失败：\n" + "\n".join(parts)
        if self.session_only:
            return self.api.user_message
        return "设置已保存"

    def __bool__(self) -> bool:
        return not self.failed
