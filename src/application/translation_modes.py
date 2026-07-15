"""UXF-010: explicit, testable translation execution profiles."""

from dataclasses import dataclass
from enum import Enum

from ..domain.translation import TranslationOptions


class TranslationMode(str, Enum):
    STABLE = "stable"
    BALANCED = "balanced"
    FAST = "fast"
    CUSTOM = "custom"


@dataclass(frozen=True)
class TranslationExecutionProfile:
    mode: TranslationMode
    batch_size: int
    max_retries: int
    concurrency: int
    timeout_seconds: int
    description: str

    def apply(self, options: TranslationOptions) -> TranslationOptions:
        """Return options with the profile's request safety settings."""
        return TranslationOptions(
            target_language=options.target_language,
            model_name=options.model_name,
            batch_size=self.batch_size,
            max_retries=self.max_retries,
            temperature=options.temperature,
            max_tokens=options.max_tokens,
        )


_PROFILES = {
    TranslationMode.STABLE: TranslationExecutionProfile(
        TranslationMode.STABLE, 8, 4, 1, 90, "小批次、低并发，优先隔离失败和保持对齐。"
    ),
    TranslationMode.BALANCED: TranslationExecutionProfile(
        TranslationMode.BALANCED, 20, 3, 2, 60, "默认策略，在吞吐和稳定性之间平衡。"
    ),
    TranslationMode.FAST: TranslationExecutionProfile(
        TranslationMode.FAST, 40, 2, 4, 45, "较大批次和较高并发，适合可承受重试风险的任务。"
    ),
}


def get_translation_profile(
    mode: TranslationMode | str,
    *,
    custom_batch_size: int | None = None,
    custom_max_retries: int | None = None,
    custom_concurrency: int | None = None,
    custom_timeout_seconds: int | None = None,
) -> TranslationExecutionProfile:
    """Resolve a named profile, validating custom settings at the boundary."""
    selected = TranslationMode(mode)
    if selected is not TranslationMode.CUSTOM:
        return _PROFILES[selected]

    batch_size = custom_batch_size or 20
    max_retries = custom_max_retries if custom_max_retries is not None else 3
    concurrency = custom_concurrency or 2
    timeout_seconds = custom_timeout_seconds or 60
    if not 1 <= batch_size <= 200:
        raise ValueError("自定义批次大小必须在 1 到 200 之间")
    if not 0 <= max_retries <= 10:
        raise ValueError("自定义重试次数必须在 0 到 10 之间")
    if not 1 <= concurrency <= 8:
        raise ValueError("自定义并发数必须在 1 到 8 之间")
    if not 10 <= timeout_seconds <= 300:
        raise ValueError("自定义超时必须在 10 到 300 秒之间")
    return TranslationExecutionProfile(
        TranslationMode.CUSTOM,
        batch_size,
        max_retries,
        concurrency,
        timeout_seconds,
        "使用自定义批次、重试、并发和超时设置。",
    )
