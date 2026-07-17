"""UXF-009: actionable error classification and bounded retry policy."""

from dataclasses import dataclass
from enum import Enum
from random import Random

from ..domain.errors import TranslationRequestError


class ErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    MODEL_UNAVAILABLE = "model_unavailable"
    RESPONSE_FORMAT = "response_format"
    LOCAL_IO = "local_io"
    EPUB = "epub"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionableError:
    category: ErrorCategory
    message: str
    recommendation: str
    retryable: bool


def classify_error(error: BaseException) -> ActionableError:
    """Translate infrastructure exceptions into user-facing, safe next actions."""
    message = str(error) or error.__class__.__name__
    status_code = getattr(error, "status_code", None)
    lowered = message.lower()
    if status_code in (401, 403) or any(
        token in lowered for token in ("api key", "unauthorized", "forbidden", "authentication")
    ):
        return ActionableError(
            ErrorCategory.AUTHENTICATION, message, "检查 API Key 和账户权限后重试。", False
        )
    if status_code == 429 or "rate limit" in lowered or "限流" in message:
        return ActionableError(
            ErrorCategory.RATE_LIMIT, message, "稍后重试，或降低并发和批次大小。", True
        )
    if status_code in (402, 429) or any(
        token in lowered for token in ("quota", "balance", "额度", "余额")
    ):
        return ActionableError(ErrorCategory.QUOTA, message, "检查账户余额或额度后再继续。", False)
    if status_code in (404, 410, 503) or "model" in lowered and "available" in lowered:
        return ActionableError(
            ErrorCategory.MODEL_UNAVAILABLE,
            message,
            "更换可用模型后重试失败行。",
            status_code == 503,
        )
    if isinstance(error, (TimeoutError, ConnectionError)) or any(
        token in lowered for token in ("timeout", "connect", "network", "连接", "超时")
    ):
        return ActionableError(ErrorCategory.NETWORK, message, "检查网络连接后重试失败行。", True)
    if isinstance(error, OSError):
        return ActionableError(
            ErrorCategory.LOCAL_IO, message, "检查文件路径、权限和磁盘空间后重试保存。", False
        )
    if isinstance(error, TranslationRequestError) and "line" in lowered:
        return ActionableError(
            ErrorCategory.RESPONSE_FORMAT, message, "重试失败行；若仍失败，请更换模型。", True
        )
    return ActionableError(ErrorCategory.UNKNOWN, message, "查看日志并重试失败行。", True)


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
