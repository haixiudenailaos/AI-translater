"""Stable streaming profile shared by all text translation models."""

from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

SILICONFLOW_PROVIDER = "siliconflow"
DEEPSEEK_PROVIDER = "deepseek"
OPENAI_COMPATIBLE_PROVIDER = "openai_compatible"

SILICONFLOW_DEEPSEEK_V32_MODEL = "deepseek-ai/DeepSeek-V3.2"
DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"

DEFAULT_TRANSLATION_BATCH_LINES = 10
MAX_STABLE_TRANSLATION_BATCH_LINES = 20
DEFAULT_TRANSLATION_INPUT_TOKENS = 6000
MAX_STABLE_TRANSLATION_INPUT_TOKENS = 6000
DEFAULT_TRANSLATION_CONCURRENCY = 1

# Queue translation is an unattended throughput-oriented path. It deliberately
# uses larger requests and more in-flight batches than the interactive editor.
DEFAULT_QUEUE_TRANSLATION_BATCH_LINES = 80
MAX_QUEUE_TRANSLATION_BATCH_LINES = 200
DEFAULT_QUEUE_TRANSLATION_INPUT_TOKENS = 16000
MAX_QUEUE_TRANSLATION_INPUT_TOKENS = 20000
DEFAULT_QUEUE_TRANSLATION_CONCURRENCY = 2
MAX_QUEUE_TRANSLATION_CONCURRENCY = 4

# 队列翻译并发优化阶段 1（QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §3）：
# 全局公平调度器的策略参数。所有并发只有一个清晰 owner——全局
# ``ProviderLimiter.current_limit``，不再有"文件并发 × 文件内并发"两层乘法。
#
# - ``queue_max_in_flight_requests``：ProviderLimiter 的 configured_max，
#   即主编辑器与后台队列在同一 Provider 上合计的在途请求上限
#   （AIMD 在此基础上动态调整；字段名为兼容旧配置而保留 queue_ 前缀）。
# - ``queue_hard_request_cap``：硬上限，不可突破。ThreadPoolExecutor 的
#   max_workers 也以此为准，避免两层并发乘法导致实际请求数失控。
# - ``queue_max_active_tasks``：Coordinator 同时调度的任务数上限
#   （超过此数量的任务进入 PENDING 等待）。
# - ``queue_per_task_soft_limit``：round-robin 第一轮每个任务最多取得的
#   槽位数（防止单任务独占）。
# - ``queue_adaptive_concurrency``：是否启用 AIMD 自适应并发。
# - ``queue_rpm_limit`` / ``queue_tpm_limit``：Provider 级 RPM/TPM 限流，
#   0 表示不限制。供共享 ProviderLimiter 使用。
DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS = 2
MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS = 4
DEFAULT_QUEUE_HARD_REQUEST_CAP = 4
MAX_QUEUE_HARD_REQUEST_CAP = 8
DEFAULT_QUEUE_MAX_ACTIVE_TASKS = 4
MAX_QUEUE_MAX_ACTIVE_TASKS = 16
DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT = 1
MAX_QUEUE_PER_TASK_SOFT_LIMIT = 4
DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY = True
DEFAULT_QUEUE_RPM_LIMIT = 0
MAX_QUEUE_RPM_LIMIT = 10000
DEFAULT_QUEUE_TPM_LIMIT = 0
MAX_QUEUE_TPM_LIMIT = 10_000_000


def build_queue_policy_from_app_config(app_config: Dict[str, Any]):
    """从应用配置构造 ``QueuePolicy``（阶段 1 设置接线）。

    阶段 2 起被 ``ConcurrentTranslationManager`` / ``QueueTranslationCoordinator``
    调用。本函数集中处理默认值/上限裁剪，避免调用方重复实现。
    返回 ``QueuePolicy`` 实例（位于 ``src/core/queue_scheduler``）。
    """
    # 延迟导入避免配置层依赖核心层（queue_scheduler 反向依赖 translation_profile
    # 的 normalize_openai_base_url，循环依赖会破坏分层）。
    from ..core.queue_scheduler import QueuePolicy

    def _clamp(value_name: str, default: int, lo: int, hi: int) -> int:
        raw = app_config.get(value_name, default)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    max_in_flight = _clamp(
        "queue_max_in_flight_requests",
        DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS,
        1,
        MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS,
    )
    hard_cap = _clamp(
        "queue_hard_request_cap",
        DEFAULT_QUEUE_HARD_REQUEST_CAP,
        max_in_flight,
        MAX_QUEUE_HARD_REQUEST_CAP,
    )
    max_active = _clamp(
        "queue_max_active_tasks",
        DEFAULT_QUEUE_MAX_ACTIVE_TASKS,
        1,
        MAX_QUEUE_MAX_ACTIVE_TASKS,
    )
    per_task_soft = _clamp(
        "queue_per_task_soft_limit",
        DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT,
        1,
        MAX_QUEUE_PER_TASK_SOFT_LIMIT,
    )
    # 兼容旧字段 queue_translation_concurrency：若用户未显式设置新字段，
    # 退回旧行为（max_in_flight = queue_translation_concurrency）。
    legacy_concurrency = _clamp(
        "queue_translation_concurrency",
        DEFAULT_QUEUE_TRANSLATION_CONCURRENCY,
        1,
        MAX_QUEUE_TRANSLATION_CONCURRENCY,
    )
    if "queue_max_in_flight_requests" not in app_config:
        max_in_flight = min(max_in_flight, legacy_concurrency)
        hard_cap = max(hard_cap, min(MAX_QUEUE_HARD_REQUEST_CAP, legacy_concurrency * 2))

    target_tokens = _clamp(
        "queue_batch_max_input_tokens",
        DEFAULT_QUEUE_TRANSLATION_INPUT_TOKENS,
        512,
        MAX_QUEUE_TRANSLATION_INPUT_TOKENS,
    )
    max_tokens = max(
        target_tokens,
        min(
            MAX_QUEUE_TRANSLATION_INPUT_TOKENS,
            target_tokens * 2,
        ),
    )
    max_lines = _clamp(
        "queue_batch_lines",
        DEFAULT_QUEUE_TRANSLATION_BATCH_LINES,
        1,
        MAX_QUEUE_TRANSLATION_BATCH_LINES,
    )

    rpm_limit = _clamp(
        "queue_rpm_limit",
        DEFAULT_QUEUE_RPM_LIMIT,
        0,
        MAX_QUEUE_RPM_LIMIT,
    )
    tpm_limit = _clamp(
        "queue_tpm_limit",
        DEFAULT_QUEUE_TPM_LIMIT,
        0,
        MAX_QUEUE_TPM_LIMIT,
    )
    adaptive = bool(
        app_config.get(
            "queue_adaptive_concurrency",
            DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY,
        )
    )

    return QueuePolicy(
        max_in_flight_requests=max_in_flight,
        hard_request_cap=hard_cap,
        max_active_tasks=max_active,
        per_task_soft_limit=per_task_soft,
        target_batch_input_tokens=target_tokens,
        max_batch_input_tokens=max_tokens,
        max_batch_lines=max_lines,
        min_batch_input_tokens=512,
        adaptive_concurrency=adaptive,
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
    )


def default_base_url_for_provider(provider: str) -> str:
    if provider == DEEPSEEK_PROVIDER:
        return "https://api.deepseek.com/v1"
    if provider == OPENAI_COMPATIBLE_PROVIDER:
        return ""
    return "https://api.siliconflow.cn/v1"


def normalize_openai_base_url(value: str) -> str:
    """Validate and normalize the root URL of an OpenAI-compatible API."""
    raw_url = (value or "").strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须是以 http:// 或 https:// 开头的有效地址")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL 不能包含查询参数或锚点")

    path = parsed.path.rstrip("/")
    completion_path = "/chat/completions"
    if path.lower().endswith(completion_path):
        path = path[: -len(completion_path)].rstrip("/")

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def apply_text_translation_profile(config: Dict[str, Any]) -> Dict[str, Any]:
    """Force streaming while preserving the selected provider and model."""
    normalized = dict(config)
    if normalized.get("provider") == DEEPSEEK_PROVIDER:
        legacy_official_models = {
            "deepseek-ai/DeepSeek-V3.2-Exp",
            "deepseek-chat",
        }
        if normalized.get("model_name") in legacy_official_models:
            normalized["model_name"] = DEEPSEEK_V4_FLASH_MODEL
    normalized["enable_stream"] = True
    return normalized
