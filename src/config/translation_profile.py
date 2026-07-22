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
# 仅用于自动分批时预留模型输出上下文，不会作为 max_tokens 发送给 API。
DEFAULT_OUTPUT_TOKEN_RESERVE = 4096

# Queue translation is an unattended throughput-oriented path. It deliberately
# uses larger requests and more in-flight batches than the interactive editor.
DEFAULT_QUEUE_TRANSLATION_BATCH_LINES = 80
MAX_QUEUE_TRANSLATION_BATCH_LINES = 200
DEFAULT_QUEUE_TRANSLATION_INPUT_TOKENS = 16000
MAX_QUEUE_TRANSLATION_INPUT_TOKENS = 20000
DEFAULT_QUEUE_TRANSLATION_CONCURRENCY = 2
MAX_QUEUE_TRANSLATION_CONCURRENCY = 4

# 面向用户的全局并发档位。2/4/8 能覆盖常见的免费、标准和高额度 API，
# 同时保留 1-16 的自定义范围供私有部署或高配额账户使用。
QUEUE_CONCURRENCY_CUSTOM = "custom"
QUEUE_CONCURRENCY_PRESETS = {
    "small": 2,
    "medium": 4,
    "large": 8,
}
DEFAULT_QUEUE_CONCURRENCY_PRESET = "medium"
MAX_QUEUE_CUSTOM_CONCURRENCY = 16

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
DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS = QUEUE_CONCURRENCY_PRESETS[DEFAULT_QUEUE_CONCURRENCY_PRESET]
MAX_QUEUE_MAX_IN_FLIGHT_REQUESTS = MAX_QUEUE_CUSTOM_CONCURRENCY
DEFAULT_QUEUE_HARD_REQUEST_CAP = 4
MAX_QUEUE_HARD_REQUEST_CAP = MAX_QUEUE_CUSTOM_CONCURRENCY
DEFAULT_QUEUE_MAX_ACTIVE_TASKS = 4
MAX_QUEUE_MAX_ACTIVE_TASKS = 16
DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT = 1
MAX_QUEUE_PER_TASK_SOFT_LIMIT = 4
DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY = True
DEFAULT_QUEUE_RPM_LIMIT = 0
MAX_QUEUE_RPM_LIMIT = 10000
DEFAULT_QUEUE_TPM_LIMIT = 0
MAX_QUEUE_TPM_LIMIT = 10_000_000


def detect_queue_concurrency_preset(app_config: Dict[str, Any]) -> str:
    """Return the UI preset matching an existing global concurrency value.

    The explicit ``custom`` choice is preserved even when its value happens to
    equal a preset. Older configs without a preset key are matched by value.
    """
    raw_concurrency = app_config.get(
        "queue_max_in_flight_requests", DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS
    )
    try:
        concurrency = int(raw_concurrency)
    except (TypeError, ValueError):
        concurrency = DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS
    concurrency = max(1, min(MAX_QUEUE_CUSTOM_CONCURRENCY, concurrency))

    stored_preset = app_config.get("queue_concurrency_preset")
    if stored_preset == QUEUE_CONCURRENCY_CUSTOM:
        return QUEUE_CONCURRENCY_CUSTOM
    if (
        isinstance(stored_preset, str)
        and stored_preset in QUEUE_CONCURRENCY_PRESETS
        and QUEUE_CONCURRENCY_PRESETS[stored_preset] == concurrency
    ):
        return stored_preset

    for preset, preset_concurrency in QUEUE_CONCURRENCY_PRESETS.items():
        if preset_concurrency == concurrency:
            return preset
    return QUEUE_CONCURRENCY_CUSTOM


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

    preset = app_config.get("queue_concurrency_preset")
    preset_default = QUEUE_CONCURRENCY_PRESETS.get(
        str(preset), DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS
    )
    max_in_flight = _clamp(
        "queue_max_in_flight_requests",
        preset_default,
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
    if (
        "queue_max_in_flight_requests" not in app_config
        and "queue_concurrency_preset" not in app_config
        and "queue_translation_concurrency" in app_config
    ):
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


# P1-1：自定义 API 端点的 HTTP 回环白名单。
# 只允许 HTTPS 远程传输 Bearer 密钥；HTTP 仅限本地回环，
# 防止通过明文链路把密钥泄漏到网络中间设备。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_host(host: str) -> bool:
    """判断 host 是否属于本地回环（IPv4/IPv6/localhost）。

    P1-1：仅对回环地址允许 HTTP，远程地址强制 HTTPS。
    """
    if not host:
        return False
    host_lower = host.lower().strip("[]")
    if host_lower in _LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 整段都是回环，但 IPv6 回环只有 ::1
    if host_lower.count(".") == 3 and host_lower.split(".")[0] == "127":
        try:
            return all(0 <= int(octet) <= 255 for octet in host_lower.split("."))
        except ValueError:
            return False
    return False


def normalize_openai_base_url(value: str) -> str:
    """Validate and normalize the root URL of an OpenAI-compatible API.

    P1-1：拒绝通过远程明文 HTTP 发送 Bearer 密钥。HTTPS 总是允许；
    HTTP 仅对回环地址（``localhost`` / ``127.0.0.1`` / ``::1``）放行，
    以保留本地推理服务（如 Ollama）的兼容路径。
    """
    raw_url = (value or "").strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须是以 http:// 或 https:// 开头的有效地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Base URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL 不能包含查询参数或锚点")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname or ""):
        raise ValueError(
            "远程 API 必须使用 HTTPS；如需使用本地推理服务（Ollama 等），"
            "请使用 http://127.0.0.1、http://localhost 或 http://[::1]"
        )

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
