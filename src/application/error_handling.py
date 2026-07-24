"""UX-3 / UXF-009: actionable error classification and bounded retry policy.

P1-UX-3：所有异常先转换为 ``ActionableError(category, safe_message,
recommended_action, retryable, correlation_id)``。

- ``safe_message``：脱敏后的用户可读文案（不含 API Key、文件路径、响应正文）。
- ``recommended_action``：用户可执行的下一步动作。
- ``correlation_id``：用于关联 UI、日志和后续诊断的 12 位短码。
- 原始异常 / 路径 / 响应摘要只进入脱敏日志（``logger`` 调用方负责）。
- UI 通过 ``format_diagnostic_info`` 拼装可安全复制的诊断字符串，
  不暴露内部堆栈。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from random import Random

from ..domain.errors import TranslationRequestError
from ..utils.logger import get_logger

logger = get_logger(__name__)


class ErrorCategory(str, Enum):
    """错误分类。值即对外可见的稳定字符串。"""

    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    MODEL_UNAVAILABLE = "model_unavailable"
    RESPONSE_FORMAT = "response_format"
    LOCAL_IO = "local_io"
    EPUB = "epub"
    UNKNOWN = "unknown"


# ── 脱敏工具 ────────────────────────────────────────────

# Windows / POSIX 路径前缀（盘符或 / 开头），含中文与 ASCII。
# 不替换单纯的「译文.txt」这种相对名，但仍会替换「C:\Users\...」之类绝对路径。
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/])(?:[^\s:<>\"|?*]+\.)+[A-Za-z0-9_]+")
# 显式 API Key 头部（Authorization: Bearer xxx / X-API-Key: xxx / api_key=xxx）
_APIKEY_PATTERN = re.compile(
    r"(?i)"
    r"(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|(?:x[-_]?api[-_]?key|api[-_]?key|secret|token)\s*[:=]\s*[A-Za-z0-9._\-]{6,}"
)
# URL（含 Basic Auth 凭据）
_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>]+",
)
# 形如 "response: {...}" / "body: ..." 的响应摘要（只保留首尾提示）
_RESPONSE_BODY_PATTERN = re.compile(r"(?is)\b(response|body|content)\s*[:=]\s*[\[{].*?[\]}]")


def sanitize_error_message(text: str) -> str:
    """脱敏原始错误消息。

    按优先级替换：API Key → URL → 文件绝对路径 → 响应正文片段。
    替换后保留 HTTP 状态码、异常类名等公共信息，便于用户对照排错。
    """
    if not text:
        return ""
    sanitized = text
    sanitized = _APIKEY_PATTERN.sub("<api_key>", sanitized)
    sanitized = _URL_PATTERN.sub("<endpoint>", sanitized)
    sanitized = _PATH_PATTERN.sub("<path>", sanitized)
    sanitized = _RESPONSE_BODY_PATTERN.sub(r"\1: <redacted>", sanitized)
    # 长度兜底：防止意外的全量堆栈被展示
    if len(sanitized) > 500:
        sanitized = sanitized[:497] + "..."
    return sanitized


def make_correlation_id() -> str:
    """生成 12 位十六进制 correlation_id（uuid4 前 12 位）。

    与现有 attempt_id / run_id 风格保持一致，足够区分单次会话内错误。
    """
    return uuid.uuid4().hex[:12]


# ── ActionableError ────────────────────────────────────


@dataclass(frozen=True)
class ActionableError:
    """用户可执行错误。

    Attributes:
        category: 错误类别（对应不同 UI 动作）。
        safe_message: 脱敏后可向用户展示的简短文案。
        recommended_action: 用户可执行的下一步（如「重新登录」「降低并发」）。
        retryable: 是否可自动重试（``RetryPolicy`` 据此决定是否重试）。
        correlation_id: 用于关联 UI、日志和诊断信息的 12 位短码。
    """

    category: ErrorCategory
    safe_message: str
    recommended_action: str
    retryable: bool
    correlation_id: str = ""


def classify_error(
    error: BaseException,
    *,
    correlation_id: str | None = None,
) -> ActionableError:
    """将基础设施异常转换为用户可读的 ``ActionableError``。

    原始异常的内部细节（路径、Key、响应正文）通过 ``sanitize_error_message``
    脱敏后进入 ``safe_message``。完整原始异常由调用方使用
    ``logger.exception`` 记录，并通过 ``correlation_id`` 与 UI 关联。

    Args:
        error: 捕获到的任意异常。
        correlation_id: 已有的关联 ID；未提供时生成新 ID。
    """
    if correlation_id is None:
        correlation_id = make_correlation_id()

    raw_message = str(error) or error.__class__.__name__
    safe_message = sanitize_error_message(raw_message)
    status_code = getattr(error, "status_code", None)
    lowered = raw_message.lower()

    if "并发批次过大" in raw_message and "服务商拒绝" in raw_message:
        return ActionableError(
            ErrorCategory.RATE_LIMIT,
            safe_message,
            "当前已按并发 1 运行；请稍后重试或检查服务商限制。",
            True,
            correlation_id,
        )

    # 401/403：认证 / 授权
    if status_code in (401, 403) or any(
        token in lowered
        for token in ("api key", "unauthorized", "forbidden", "authentication", "鉴权", "未授权")
    ):
        return ActionableError(
            ErrorCategory.AUTHENTICATION,
            safe_message or "认证失败",
            "检查 API Key 和账户权限后重试。",
            False,
            correlation_id,
        )

    # 429 / rate limit（在 quota 之前判定，避免被误归类为余额）
    if status_code == 429 or "rate limit" in lowered or "限流" in raw_message:
        return ActionableError(
            ErrorCategory.RATE_LIMIT,
            safe_message or "请求频率超限",
            "稍后重试，或降低并发和批次大小。",
            True,
            correlation_id,
        )

    # 402 / quota / balance
    if status_code == 402 or any(
        token in lowered for token in ("quota", "balance", "额度", "余额")
    ):
        return ActionableError(
            ErrorCategory.QUOTA,
            safe_message or "账户额度不足",
            "检查账户余额或额度后再继续。",
            False,
            correlation_id,
        )

    # 404/410/503：模型不可用
    if status_code in (404, 410, 503) or ("model" in lowered and "available" in lowered):
        return ActionableError(
            ErrorCategory.MODEL_UNAVAILABLE,
            safe_message or "模型不可用",
            "更换可用模型后重试失败行。",
            status_code == 503,
            correlation_id,
        )

    # 网络超时 / 连接失败
    if isinstance(error, TimeoutError | ConnectionError) or any(
        token in lowered for token in ("timeout", "connect", "network", "连接", "超时", "网络")
    ):
        return ActionableError(
            ErrorCategory.NETWORK,
            safe_message or "网络连接失败",
            "检查网络连接后重试失败行。",
            True,
            correlation_id,
        )

    # 本地 IO / 权限 / 磁盘
    if isinstance(error, OSError):
        return ActionableError(
            ErrorCategory.LOCAL_IO,
            safe_message or "文件读写失败",
            "检查文件路径、权限和磁盘空间后重试保存。",
            False,
            correlation_id,
        )

    # 响应格式错误
    if isinstance(error, TranslationRequestError) and (
        "line" in lowered or "format" in lowered or "格式" in raw_message
    ):
        return ActionableError(
            ErrorCategory.RESPONSE_FORMAT,
            safe_message or "响应格式错误",
            "重试失败行；若仍失败，请更换模型。",
            True,
            correlation_id,
        )

    # EPUB 相关
    if "epub" in lowered or "Epub" in error.__class__.__name__:
        return ActionableError(
            ErrorCategory.EPUB,
            safe_message or "EPUB 处理失败",
            "检查 EPUB 文件结构或重新导入。",
            True,
            correlation_id,
        )

    # 未知：建议查看日志
    return ActionableError(
        ErrorCategory.UNKNOWN,
        safe_message or "未知错误",
        "查看日志并重试失败行。",
        True,
        correlation_id,
    )


def format_diagnostic_info(
    error: BaseException,
    actionable: ActionableError,
    *,
    extra: dict | None = None,
) -> str:
    """格式化可安全复制的诊断信息字符串。

    包含 correlation_id、类别、安全文案和建议动作，不包含原始堆栈、
    API Key、完整文件路径。可附加最多 5 个键值对（如 task_id、batch_id）。
    """
    lines = [
        f"correlation_id: {actionable.correlation_id}",
        f"category: {actionable.category.value}",
        f"message: {actionable.safe_message}",
        f"recommended_action: {actionable.recommended_action}",
        f"retryable: {actionable.retryable}",
    ]
    if extra:
        # 额外信息最多 5 项，全部走脱敏
        for k, v in list(extra.items())[:5]:
            lines.append(f"{k}: {sanitize_error_message(str(v))}")
    return "\n".join(lines)


def log_classified_error(
    error: BaseException,
    actionable: ActionableError,
    *,
    context: dict | None = None,
) -> None:
    """记录分类后的错误到日志。

    原始异常（含完整堆栈、路径、响应摘要）通过 ``logger.exception`` 落到
    脱敏日志（日志文件目录由用户控制，不直接展示给 UI）。
    correlation_id 关联日志条目与 UI 显示。
    """
    ctx_str = ""
    if context:
        ctx_str = " | ".join(f"{k}={v}" for k, v in context.items())
    logger.exception(
        "ActionableError[%s] correlation_id=%s %s",
        actionable.category.value,
        actionable.correlation_id,
        ctx_str,
        exc_info=error,
    )


# ── RetryPolicy ────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 20.0
    jitter_ratio: float = 0.2

    def should_retry(self, error: BaseException, attempts_completed: int) -> bool:
        return attempts_completed < self.max_attempts and classify_error(error).retryable

    def delay_for(self, attempts_completed: int, *, random_source: Random | None = None) -> float:
        """Return capped exponential backoff with bounded positive/negative jitter."""
        raw_delay = min(self.max_delay_seconds, self.base_delay_seconds * (2**attempts_completed))
        generator = random_source or Random()
        jitter = generator.uniform(-self.jitter_ratio, self.jitter_ratio)
        return max(0.0, raw_delay * (1 + jitter))
