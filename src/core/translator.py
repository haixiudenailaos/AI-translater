#!/usr/bin/env python3
"""
翻译引擎模块
负责协调翻译流程，分批翻译 + 流式输出
"""

import hashlib
import re
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

from ..api.deepseek_api import DeepseekAPI
from ..api.openai_compatible_api import OpenAICompatibleAPI
from ..api.siliconflow_api import SiliconFlowAPI
from ..application.batch_planner import (
    BatchPlanningError,
    compute_protocol_overhead,
    plan_batches,
)
from ..application.context_budget import is_unsatisfiable_for_provider
from ..config.translation_profile import (
    DEFAULT_OUTPUT_TOKEN_RESERVE,
    DEFAULT_QUEUE_TRANSLATION_BATCH_LINES,
    DEFAULT_QUEUE_TRANSLATION_CONCURRENCY,
    DEFAULT_QUEUE_TRANSLATION_INPUT_TOKENS,
    DEFAULT_TRANSLATION_BATCH_LINES,
    DEFAULT_TRANSLATION_CONCURRENCY,
    DEFAULT_TRANSLATION_INPUT_TOKENS,
    MAX_QUEUE_TRANSLATION_BATCH_LINES,
    MAX_QUEUE_TRANSLATION_CONCURRENCY,
    MAX_QUEUE_TRANSLATION_INPUT_TOKENS,
    MAX_STABLE_TRANSLATION_BATCH_LINES,
    MAX_STABLE_TRANSLATION_INPUT_TOKENS,
    SMALL_MODEL_MODE_CONFIG_KEY,
    apply_text_translation_profile,
)
from ..domain.translation_policy import (
    ContextMode,
    ResolvedContextBudget,
    TranslationContextPolicy,
)
from ..utils.logger import get_logger
from .queue_provider import (
    ProviderLimiter,
    ProviderLimiterRegistry,
    provider_runtime_key_for,
)
from .translation_result import (
    BatchTranslationResult,
    TranslationCancelled,
    TranslationRequestError,
    TranslationStatus,
)

logger = get_logger(__name__)

# PERF-001：预编译行号标记正则，避免在热路径重复编译。
#
# 部分模型不会严格复现提示词中的 ``[LINE_001]``，而会改写为
# ``[LINE-001]``、``[LINE 001]`` 或全角括号。标记本身是内部协议，
# 因此在行首对这些等价写法做宽容解析；锚定行首则可避免误删正文中
# 本来就存在的 ``[LINE-001]`` 字样。
_LINE_MARKER_PATTERN = r"[\[［【]\s*LINE\s*[_\-‐‑‒–—－:：.．\s]*" r"(?P<line_number>\d+)\s*[\]］】]"
_LINE_MARKER_RE = re.compile(_LINE_MARKER_PATTERN, re.IGNORECASE)
_LINE_MARKER_PARSE_RE = re.compile(
    rf"^\ufeff?[ \t]*(?:[-*+>][ \t]+)?(?:\*\*|__)?{_LINE_MARKER_PATTERN}"
    r"(?:\*\*|__)?(?P<content>.*)$",
    re.IGNORECASE,
)
_SMALL_MODEL_PROVIDER_REJECTION_MESSAGE = (
    "小模型逐行翻译的并发批次过大，已被服务商拒绝。"
    "翻译已停止；当前并发已限制为 1，请稍后重试或检查服务商限制。"
)
_SMALL_MODEL_STREAM_CLOSED_MESSAGE = (
    "小模型逐行翻译时流式连接被服务商提前关闭。翻译已停止；当前已按单并发逐行运行。"
)
_SMALL_MODEL_LONG_CONTEXT_CONFLICT_MESSAGE = (
    "小模型模式与超长上下文翻译不能同时使用：小模型模式强制逐行翻译，"
    "与超长批次语义冲突。请先关闭小模型模式（设置 → 翻译设置），"
    "或改用普通翻译。"
)


def _unsatisfiable_tpm_message(plan: "TranslationBatchPlan", tpm_limit: int) -> str:
    """单个请求的输入估算超过 TPM 上限时的可操作错误。"""
    return (
        f"本次请求估算输入约 {plan.estimated_input_tokens:,} token，"
        f"超过当前每分钟 token 限额（TPM）{tpm_limit:,}。"
        "该请求永远无法取得发送许可。请调小“单次上下文大小”，"
        "或在队列设置中提高 TPM 限额。"
    )


def _truncated_result_message(finish_reason: str | None) -> str:
    """输出被 provider 截断时的可操作错误。"""
    detail = f"（finish_reason={finish_reason}）" if finish_reason else ""
    return (
        f"模型输出被截断{detail}，本批译文不完整。请调小“单次上下文大小”或更换支持更大输出的模型。"
    )


def _as_int(value: object, *, default: int, minimum: int | None = None) -> int:
    """Convert untrusted configuration or provider advice to a safe integer."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        result = default
    else:
        try:
            result = int(value)
        except ValueError:
            result = default
    return max(minimum, result) if minimum is not None else result


def _numeric_metrics(value: object) -> dict[str, int | float]:
    """Accept only numeric counters from an optional provider metrics hook."""
    if not isinstance(value, Mapping):
        return {}
    return {
        key: metric
        for key, metric in value.items()
        if isinstance(key, str) and isinstance(metric, int | float) and not isinstance(metric, bool)
    }


def _small_model_terminal_error_message(error: TranslationRequestError) -> str:
    """Translate stream/protocol rejection details into an actionable UI message.

    HTTP/2 ``ConnectionTerminated`` (and similar stream-reset errors) happen
    even at concurrency 1, typically because the provider closed a short
    sequential stream.  Mapping that to "并发批次过大" tells the user to
    lower a concurrency that is already 1, which is both wrong and
    unactionable.
    """
    message = str(error)
    lowered = message.lower()
    stream_closed_markers = (
        "connectionterminated",
        "end_stream",
        "pseudo-header in trailer",
        "winerror 10038",
        "在一个非套接字上尝试了一个操作",
    )
    if any(marker in lowered or marker in message for marker in stream_closed_markers):
        return _SMALL_MODEL_STREAM_CLOSED_MESSAGE
    if error.status_code == 429:
        return "小模型逐行翻译请求被服务商限流（HTTP 429）。翻译已停止；当前已按单并发逐行运行。"
    return message or "小模型逐行翻译失败，翻译已停止。"


@dataclass(frozen=True, slots=True)
class TranslationRunContext:
    """PERF §9.5：单次翻译运行的不可变上下文。

    在 ``_translate`` 开始时一次构造，传给所有 ``_translate_batch``，
    避免每批重复读取配置、构造术语提示和判断模型类型。

    - 不携带 API Key（API 实例已持有鉴权信息）。
    - 不进入 ``repr``/日志/缓存键的敏感字段。
    - PERF §9.5 D2/D3：``temperature`` / ``prompt_version`` / ``glossary_version``
      在运行开始时一次计算并复用，避免每批重复读取 API 配置和重算 SHA256。
      新字段带默认值，保证旧调用方（如单测）的向后兼容。
    """

    provider: str
    model_name: str
    target_language: str
    base_prompt: str
    glossary_prompt: str
    system_prompt: str | None
    is_hunyuan: bool
    # PERF §9.5 D2：temperature 一次读取，避免每批重读 api_config
    temperature: float = 0.3
    # PERF §9.5 D3：提示词/术语哈希一次计算，避免每批重算 SHA256
    prompt_version: str = ""
    prompt_schema_version: int = 1
    glossary_version: str = ""
    # 超长上下文翻译：任务级不可变预算快照。运行中的批次只读这里，
    # 不读取可变的 UI 或共享配置，因此设置改动只影响新开始的运行。
    # 普通模式为 ``None``（按原策略解析）。
    budget: ResolvedContextBudget | None = None

    @property
    def context_mode(self) -> ContextMode:
        return self.budget.mode if self.budget is not None else ContextMode.STANDARD


@dataclass(frozen=True, slots=True)
class TranslationBatchPlan:
    """PERF §9.6：批次计划只读对象。

    取代散落的 ``(start, end)`` tuple，便于在性能日志中记录批次大小和
    token 估算。

    超长上下文翻译（LONG）下 ``max_batch_lines`` 不生效，批次只受预算约束；
    此时 ``output_budget_tokens`` 是公共规划器算出的输出预留，用于在发送前
    判定"输出能否容纳本批"。普通模式沿用旧的固定预留语义。
    """

    batch_id: int
    start: int
    end: int
    estimated_input_tokens: int
    #: 该批允许的输出 token 额度；``None`` 表示按旧的固定预留处理。
    output_budget_tokens: int | None = None


def _clean_stream_line(line: str) -> str:
    """隐藏尚未接收完整的行号标记，避免协议文本闪现在 UI 中。"""
    match = _LINE_MARKER_PARSE_RE.match(line)
    if match:
        return match.group("content")

    # 流式响应可能把标记拆在任意 chunk 边界（例如先收到 ``[LINE-``）。
    # 只隐藏仍有可能组成行首标记的片段；一旦内容不再匹配 LINE 前缀，
    # 就原样展示，避免吞掉普通的方括号正文。
    candidate = line.lstrip("\ufeff \t")
    if candidate[:1] in {"-", "*", "+", ">"}:
        candidate = candidate[1:].lstrip(" \t")
    if candidate.startswith(("**", "__")):
        candidate = candidate[2:]
    if candidate[:1] in {"[", "［", "【"}:
        marker_body = candidate[1:].lstrip().upper()
        if "LINE".startswith(marker_body) or marker_body.startswith("LINE"):
            return ""
    if not candidate or candidate in {"[", "［", "【"}:
        return ""
    return line


class TranslatorEngine:
    def __init__(
        self,
        config_manager,
        *,
        limiter_registry: ProviderLimiterRegistry | None = None,
        cache_dir: Path | str | None = None,
    ):
        self.config_manager = config_manager
        self.api = None
        self.is_stopped = False
        # 仅主编辑器注入。队列引擎由 Coordinator 在派发前直接取得同一 Limiter，
        # 因而不会在这里重复取槽。
        self._limiter_registry = limiter_registry
        # STORAGE-7：解析后的缓存目录仅作为运行时注入，不写入 API 配置 JSON。
        self._cache_dir = Path(cache_dir).resolve() if cache_dir is not None else None

        # 暂停/恢复支持
        self.pause_event = threading.Event()
        self.pause_event.set()  # 默认不暂停

        # 配置变更不得拔掉在途请求的 HTTP 客户端。运行期间收到的
        # refresh_api() 只置位，等本次运行结束再真正换实例。
        self._run_lock = threading.Lock()
        self._run_active = False
        self._api_refresh_pending = False

        # 延迟初始化API与正则（按需构建）
        self._re_many_newlines = None
        # 不在构造时初始化 API，首次使用时再构建

    def _init_api(self):
        """初始化API客户端。

        BUG-005：覆盖旧实例前先关闭，避免连接和心跳线程累积。
        旧实例走 ``retire()`` 而非 ``close()``：仍在运行的批次线程持有它，
        必须让它们立刻失败，而不是重建连接跑完一个 UI 已丢弃的运行。
        """
        # 先退役旧实例（如果有）
        if self.api is not None:
            try:
                retire = getattr(self.api, "retire", None)
                if callable(retire):
                    retire()
                else:
                    self.api.close()
            except Exception as e:
                logger.warning("关闭旧API实例失败: %s", e)
            self.api = None

        api_config = apply_text_translation_profile(self.config_manager.get_api_config())
        cache_dir = getattr(self, "_cache_dir", None)
        if cache_dir is not None:
            cache_config = dict(api_config.get("cache_config", {}) or {})
            cache_config["cache_dir"] = str(cache_dir)
            api_config["cache_config"] = cache_config
        provider = api_config.get("provider", "siliconflow")

        if provider == "deepseek":
            self.api = DeepseekAPI(api_config)
        elif provider == "siliconflow":
            self.api = SiliconFlowAPI(api_config)
        elif provider == "openai_compatible":
            self.api = OpenAICompatibleAPI(api_config)
        else:
            # 默认使用 SiliconFlow
            self.api = SiliconFlowAPI(api_config)

    def _ensure_api(self):
        """惰性初始化API客户端"""
        if self.api is None:
            self._init_api()

    def build_run_context(
        self, *, budget: ResolvedContextBudget | None = None
    ) -> TranslationRunContext:
        """PERF §9.5 + 队列并发优化阶段 2：构造不可变运行上下文。

        供 ``QueueTranslationCoordinator`` 在每次任务尝试开始时调用一次，
        复用给该任务的所有 ``BatchJob``，避免逐批重复读取配置和构造提示词。
        必须在 ``_ensure_api`` 之后调用。

        ``budget`` 是本次运行解析好的上下文预算快照；普通模式传 ``None``。
        """
        app_config = self.config_manager.get_app_config()
        api_config = self.config_manager.get_api_config()
        target_language = app_config.get("target_language", "中文")
        base_prompt = app_config.get("translation_prompt", "")
        glossary_prompt = self.config_manager.get_glossary_prompt()
        model_name = api_config.get("model_name", "")
        is_hunyuan = "Hunyuan-MT" in model_name or "hunyuan-mt" in model_name.lower()
        system_prompt: str | None = None
        if not is_hunyuan:
            system_prompt = self._translation_system_prompt(
                target_language, base_prompt, glossary_prompt
            )
        run_temperature = float(api_config.get("temperature", 0.3))
        prompt_schema_version = int(app_config.get("prompt_schema_version", 1))
        prompt_version_input = f"schema:{prompt_schema_version}\n{base_prompt}"
        run_prompt_version = hashlib.sha256(prompt_version_input.encode("utf-8")).hexdigest()[:16]
        run_glossary_version = hashlib.sha256(glossary_prompt.encode("utf-8")).hexdigest()[:16]
        return TranslationRunContext(
            provider=api_config.get("provider", ""),
            model_name=model_name,
            target_language=target_language,
            base_prompt=base_prompt,
            glossary_prompt=glossary_prompt,
            system_prompt=system_prompt,
            is_hunyuan=is_hunyuan,
            temperature=run_temperature,
            prompt_version=run_prompt_version,
            prompt_schema_version=prompt_schema_version,
            glossary_version=run_glossary_version,
            budget=budget,
        )

    def _provider_input_budget_hint(self, configured: int) -> int | None:
        """读取 provider 的输入预算建议（限流压力下的动态建议）。

        返回 ``None`` 表示 provider 没有给出建议。该值只作为**本次规划**的
        额外输入上限，绝不覆盖用户保存的预算。
        """
        budget_recommendation = getattr(self.api, "recommended_input_budget", None)
        if not callable(budget_recommendation):
            return None
        hinted = _as_int(budget_recommendation(configured), default=configured, minimum=0)
        if hinted <= 0 or hinted >= configured:
            return None
        return hinted

    def resolve_run_budget(
        self,
        context_policy: TranslationContextPolicy,
        *,
        configured_input_budget: int | None = None,
        max_batch_lines: int | None = None,
        provider_input_budget_hint: bool = True,
    ) -> ResolvedContextBudget:
        """解析本次运行的有效预算。

        普通模式与超长模式共用同一个入口，调用方（主界面、队列、预检）不得
        自行实现第二套公式。必须在 ``_ensure_api`` 之后调用。

        Args:
            context_policy: 本次运行的上下文策略。
            configured_input_budget: 普通模式的配置输入预算；``None`` 时按默认值。
            max_batch_lines: 普通模式仍然生效的行数上限（超长模式忽略本参数）。
            provider_input_budget_hint: 是否把 provider 的动态预算建议作为本次
                规划的额外输入上限。预检/预览可传 ``False`` 以展示稳定的配置值。

        Raises:
            ValueError: 超长模式下预算无法解析（如设置预算小于安全余量）。
        """
        from ..application.context_budget import (
            resolve_context_budget,
            resolve_model_context_tokens,
            resolve_model_max_output_tokens,
        )

        api_config = self.config_manager.get_api_config()
        model_context = resolve_model_context_tokens(api_config)
        if context_policy.mode is ContextMode.STANDARD:
            configured = (
                configured_input_budget
                if configured_input_budget is not None
                else self.compute_input_token_budget(DEFAULT_TRANSLATION_INPUT_TOKENS)
            )
            recommendation = (
                self._provider_input_budget_hint(configured) if provider_input_budget_hint else None
            )
            return resolve_context_budget(
                context_policy,
                model_context_tokens=model_context,
                configured_input_budget=configured,
                max_batch_lines=max_batch_lines,
                provider_recommended_input_budget=recommendation,
            )
        return resolve_context_budget(
            context_policy,
            model_context_tokens=model_context,
            model_max_output_tokens=resolve_model_max_output_tokens(api_config),
        )

    def compute_input_token_budget(self, configured_budget: int) -> int:
        """计算普通模式的输入 token 预算（队列并发优化阶段 2）。

        供 ``BatchPlanner`` 使用：受模型 context window、output reserve 和
        API 实例的 ``recommended_input_budget`` 共同约束。

        超长上下文翻译**不**使用本方法（它的输入额度由
        :meth:`resolve_run_budget` 按用户总预算解析），以免旧的
        ``max(512, ...)`` 下限暗中恢复小批次限制。
        """
        api_config = self.config_manager.get_api_config()
        configured = max(512, int(configured_budget))
        recommendation = self._provider_input_budget_hint(configured)
        if recommendation is not None:
            configured = recommendation
        context_window = max(4096, int(api_config.get("context_window_tokens", 32768)))
        context_safe_budget = max(512, context_window - DEFAULT_OUTPUT_TOKEN_RESERVE - 1024)
        return min(configured, context_safe_budget)

    def _provider_tpm_limit(self) -> int:
        """读取当前 provider 的 TPM 限额（0 表示不限流）。"""
        if self._limiter_registry is None:
            return 0
        from ..config.translation_profile import build_queue_policy_from_app_config

        policy = build_queue_policy_from_app_config(self.config_manager.get_app_config())
        return max(0, int(policy.tpm_limit))

    def _get_shared_limiter(self, api_config: dict) -> ProviderLimiter | None:
        """获取应用级 ProviderLimiter；未注入注册表时保持原有独立行为。"""
        if self._limiter_registry is None:
            return None
        from ..config.translation_profile import build_queue_policy_from_app_config

        policy = build_queue_policy_from_app_config(self.config_manager.get_app_config())
        return self._limiter_registry.get_or_create(
            provider_runtime_key_for(api_config),
            configured_max=policy.max_in_flight_requests,
            hard_cap=policy.hard_request_cap,
            rpm_limit=policy.rpm_limit,
            tpm_limit=policy.tpm_limit,
        )

    def _get_re_many_newlines(self):
        """按需预编译：匹配≥2个换行"""
        if self._re_many_newlines is None:
            self._re_many_newlines = re.compile(r"\n{2,}")
        return self._re_many_newlines

    def refresh_api(self):
        """刷新API配置。

        BUG-005：先关闭旧实例，再创建新实例（_init_api 已内置关闭逻辑）。

        翻译进行中不立即应用：``_init_api`` 会退役批次线程正在读取的 HTTP
        客户端，在途的流会全部以 ``WinError 10038`` 断掉，而 UI 只看到进度条
        永久静止。此时只置位，等 ``_translate`` 结束再应用。
        """
        with self._run_lock:
            if self._run_active:
                self._api_refresh_pending = True
                logger.info("翻译进行中，API 配置变更将在本次运行结束后生效")
                return
            self._api_refresh_pending = False
        self._init_api()

    def _begin_run(self) -> None:
        """标记运行开始，让期间的 ``refresh_api`` 推迟到运行结束。"""
        with self._run_lock:
            self._run_active = True

    def _end_run(self) -> None:
        """解除运行标记并应用运行期间挂起的配置变更。

        必须在 ``_translate`` 的 ``finally`` 中调用：失败路径若不解除，
        后续所有 ``refresh_api`` 都会被永久推迟。
        """
        with self._run_lock:
            self._run_active = False
            pending = self._api_refresh_pending
            self._api_refresh_pending = False
        if pending:
            self._init_api()

    def get_usage_snapshot(self) -> dict[str, int | float]:
        """Return cumulative provider counters without request text or credentials."""
        if self.api is None:
            return {}
        metrics_getter = getattr(self.api, "get_performance_metrics", None)
        cache_getter = getattr(self.api, "get_cache_stats", None)
        metrics = _numeric_metrics(metrics_getter()) if callable(metrics_getter) else {}
        cache_stats = _numeric_metrics(cache_getter()) if callable(cache_getter) else {}
        metrics["cache_hits"] = _as_int(cache_stats.get("hits", 0), default=0, minimum=0)
        return metrics

    def close(self):
        """BUG-005：关闭翻译引擎持有的 API 资源，幂等可安全多次调用。"""
        if self.api is not None:
            try:
                self.api.close()
            except Exception as e:
                logger.warning("关闭翻译引擎API失败: %s", e)
            self.api = None

    def translate_fast_mode(
        self, content: str, progress_callback: Callable, complete_callback: Callable
    ):
        """统一翻译模式（BUG-008：合并逐行/快速两种无差异模式）。

        PERF §9.2：已移除固定 ``batch_delay=0.1`` 等待。完成循环不再无条件
        ``sleep``，避免长文档累计人为延迟（1,000 行约 100 批即累计 10 秒）。
        服务端限流退避仍由 HTTP 层 429 重试统一负责。
        """
        self._translate(content, progress_callback, complete_callback)

    def translate_long_context_mode(
        self,
        content: str,
        progress_callback: Callable,
        complete_callback: Callable,
        *,
        wallet: TranslationContextPolicy | None = None,
    ):
        """超长上下文翻译入口（普通模式之外的**显式**运行选项）。

        只做两件事：把任务级策略快照传给统一的 ``_translate``，其余分批、执行、
        流式解析、行映射、保存和导出全部复用现有管道。不得在此复制
        ``_translate_run`` / ``_translate_batch``。

        小模型模式的互斥校验在 ``_translate_run`` 里统一执行，因此本入口和
        队列路径得到同一条明确错误。
        """
        policy = wallet or TranslationContextPolicy(ContextMode.LONG)
        self._translate(
            content,
            progress_callback,
            complete_callback,
            context_policy=policy,
        )

    def translate_bulk_mode(
        self,
        content: str,
        progress_callback: Callable,
        complete_callback: Callable,
    ):
        """队列专用吞吐模式：大批次、无批次延迟、不发送流式 UI 进度。"""
        app_config = self.config_manager.get_app_config()
        batch_lines = min(
            MAX_QUEUE_TRANSLATION_BATCH_LINES,
            max(1, int(app_config.get("queue_batch_lines", DEFAULT_QUEUE_TRANSLATION_BATCH_LINES))),
        )
        if app_config.get(SMALL_MODEL_MODE_CONFIG_KEY, False):
            batch_lines = 1
        input_token_budget = min(
            MAX_QUEUE_TRANSLATION_INPUT_TOKENS,
            max(
                512,
                int(
                    app_config.get(
                        "queue_batch_max_input_tokens",
                        DEFAULT_QUEUE_TRANSLATION_INPUT_TOKENS,
                    )
                ),
            ),
        )
        concurrency = min(
            MAX_QUEUE_TRANSLATION_CONCURRENCY,
            max(
                1,
                int(
                    app_config.get(
                        "queue_translation_concurrency",
                        DEFAULT_QUEUE_TRANSLATION_CONCURRENCY,
                    )
                ),
            ),
        )
        self._translate(
            content,
            progress_callback,
            complete_callback,
            batch_lines_override=batch_lines,
            input_token_budget_override=input_token_budget,
            concurrency_override=concurrency,
            emit_stream_progress=False,
        )

    def _translate(
        self,
        content: str,
        progress_callback: Callable,
        complete_callback: Callable,
        *,
        batch_lines_override: int | None = None,
        input_token_budget_override: int | None = None,
        concurrency_override: int | None = None,
        emit_stream_progress: bool = True,
        context_policy: TranslationContextPolicy | None = None,
    ):
        """统一翻译流程：分批翻译 + 流式输出

        核心原则：
        1. 按 token 预算和最大行数拆分原文
        2. 每批使用流式翻译，实时显示翻译进度
        3. 每批翻译完成后，准确写入对应行号位置
        4. 绝对不依赖换行符拆分，只按行号对应
        5. BUG-004：区分成功/部分成功/取消/失败，不再以空列表表示失败

        PERF §9.2：已移除 ``batch_delay`` 参数和完成循环中的无条件 ``sleep``。
        PERF §9.5：在方法开始处一次构造 ``TranslationRunContext``，
        传给所有 ``_translate_batch``，避免每批重复读取配置和构造提示词。
        """
        self.reset()
        self._ensure_api()
        self._begin_run()
        try:
            self._translate_run(
                content,
                progress_callback,
                complete_callback,
                batch_lines_override=batch_lines_override,
                input_token_budget_override=input_token_budget_override,
                concurrency_override=concurrency_override,
                emit_stream_progress=emit_stream_progress,
                context_policy=context_policy,
            )
        finally:
            self._end_run()

    def _translate_run(
        self,
        content: str,
        progress_callback: Callable,
        complete_callback: Callable,
        *,
        batch_lines_override: int | None = None,
        input_token_budget_override: int | None = None,
        concurrency_override: int | None = None,
        emit_stream_progress: bool = True,
        context_policy: TranslationContextPolicy | None = None,
    ):
        """``_translate`` 的实现主体。运行标记由 ``_translate`` 负责。"""
        lines = content.split("\n")
        total_lines = len(lines)
        app_config = self.config_manager.get_app_config()
        api_config = self.config_manager.get_api_config()
        small_model_mode = bool(app_config.get(SMALL_MODEL_MODE_CONFIG_KEY, False))
        context_policy = context_policy or TranslationContextPolicy()
        if context_policy.mode is ContextMode.LONG and small_model_mode:
            # 小模型模式强制逐行，与超长批次语义互斥。程序调用入口返回明确
            # 配置错误，不静默覆盖任一方设置（实现指南 §3.2）。
            raise TranslationRequestError(
                _SMALL_MODEL_LONG_CONTEXT_CONFLICT_MESSAGE,
                failed_indices=list(range(total_lines)),
                status_code=None,
            )
        if batch_lines_override is None:
            batch_lines = min(
                MAX_STABLE_TRANSLATION_BATCH_LINES,
                max(1, int(app_config.get("batch_lines", DEFAULT_TRANSLATION_BATCH_LINES))),
            )
        else:
            batch_lines = max(1, int(batch_lines_override))
        if small_model_mode:
            batch_lines = 1
        if input_token_budget_override is None:
            configured_input_budget = min(
                MAX_STABLE_TRANSLATION_INPUT_TOKENS,
                max(
                    512,
                    int(app_config.get("batch_max_input_tokens", DEFAULT_TRANSLATION_INPUT_TOKENS)),
                ),
            )
        else:
            configured_input_budget = max(512, int(input_token_budget_override))

        # 预算解析集中在 resolve_run_budget（普通与超长共用），批次规划集中在
        # application.batch_planner。两者都只读本次运行的不可变快照。
        # 预算无法解析（如超长模式预算低于安全余量）必须变成可操作错误，
        # 而不是让 ValueError 逃逸到工作线程。
        try:
            resolved_budget = self.resolve_run_budget(
                context_policy,
                configured_input_budget=configured_input_budget,
                max_batch_lines=batch_lines,
            )
        except ValueError as exc:
            logger.error("上下文预算无法解析: %s", exc)
            complete_callback(
                BatchTranslationResult(
                    status=TranslationStatus.FAILED,
                    lines=[""] * total_lines,
                    failed_indices=list(range(total_lines)),
                    error_message=str(exc),
                )
            )
            return
        # 超长模式不受旧的行数/token 上限约束：预算只来自用户设置、模型容量与
        # 输出预留。``max_batch_lines`` 为 ``None`` 表示"应用不设上限"。
        if resolved_budget.mode is ContextMode.LONG:
            batch_lines = resolved_budget.max_batch_lines
        input_token_budget = resolved_budget.batch_budget_tokens
        if small_model_mode:
            from ..config.translation_profile import build_queue_policy_from_app_config

            concurrency = build_queue_policy_from_app_config(app_config).max_in_flight_requests
        elif concurrency_override is None:
            concurrency = max(
                1,
                min(
                    8,
                    int(
                        app_config.get(
                            "translation_concurrency",
                            DEFAULT_TRANSLATION_CONCURRENCY,
                        )
                    ),
                ),
            )
        else:
            concurrency = max(1, min(8, int(concurrency_override)))

        # 逐行模式在打包后的 Windows exe 上不能走 HTTP/2：连续开关大量极短
        # 的流会让 h2 以 ConnectionTerminated / WinError 10038 掉掉整条连接，
        # 表现为翻译中途长时间停顿。让 provider 自己决定如何降级。
        serial_transport = small_model_mode
        configure_serial = getattr(self.api, "configure_serial_transport", None)
        if callable(configure_serial):
            try:
                configure_serial(serial_transport)
            except Exception as exc:  # noqa: BLE001
                logger.warning("切换 HTTP 传输协议失败: %s", exc)

        # PERF §9.5：一次构造不可变运行上下文。
        # 后续所有批次复用同一份配置/提示词，避免逐批重复读取和构造。
        target_language = app_config.get("target_language", "中文")
        run_context = self.build_run_context(budget=resolved_budget)
        if target_language != run_context.target_language:
            logger.debug(
                "translation settings changed while starting a run; using the latest snapshot"
            )

        # 主编辑器与后台队列共用同一个 Provider 请求额度。主编辑器仍保留
        # translation_concurrency 作为本次任务的局部并发上限，但实际网络请求
        # 还必须通过应用级 Limiter，因此两条路径的在途总数不会相加失控。
        shared_limiter = self._get_shared_limiter(api_config)

        try:
            batch_ranges = self._build_batch_ranges(
                lines,
                batch_lines,
                input_token_budget,
                resolved_budget=resolved_budget,
                run_context=run_context,
            )
        except BatchPlanningError as exc:
            # 预算不足必须是可操作错误，而不是空批次或无限循环。
            logger.error("批次规划失败: %s", exc)
            complete_callback(
                BatchTranslationResult(
                    status=TranslationStatus.FAILED,
                    lines=[""] * total_lines,
                    failed_indices=list(range(total_lines)),
                    error_message=str(exc),
                )
            )
            return

        # 发送前检查不可满足的 TPM 请求：单个请求的输入估算若超过非零 TPM
        # 上限，它永远等不到许可。此处直接报错并提示调整，不永久挂起。
        tpm_limit = self._provider_tpm_limit()
        if tpm_limit > 0:
            oversized = [
                plan
                for plan in batch_ranges
                if is_unsatisfiable_for_provider(
                    estimated_input_tokens=plan.estimated_input_tokens, tpm_limit=tpm_limit
                )
            ]
            if oversized:
                plan = oversized[0]
                complete_callback(
                    BatchTranslationResult(
                        status=TranslationStatus.FAILED,
                        lines=[""] * total_lines,
                        failed_indices=list(range(plan.start, plan.end)),
                        error_message=_unsatisfiable_tpm_message(plan, tpm_limit),
                    )
                )
                return
        limiter_consumer_id: str | None = None
        if shared_limiter is not None:
            limiter_consumer_id = f"main:{uuid.uuid4().hex}"
            shared_limiter.register_consumer(limiter_consumer_id, priority=0)
        limiter_demand_lock = threading.Lock()
        remaining_limiter_requests = len(batch_ranges)
        fatal_error_event = threading.Event()
        fatal_error_lock = threading.Lock()
        fatal_error_message: str | None = None

        def mark_limiter_request_sent() -> None:
            """Release the main run's reserved share once every request is sent."""
            nonlocal remaining_limiter_requests
            if shared_limiter is None or limiter_consumer_id is None:
                return
            with limiter_demand_lock:
                remaining_limiter_requests = max(0, remaining_limiter_requests - 1)
                has_no_more_requests = remaining_limiter_requests == 0
            if has_no_more_requests:
                shared_limiter.set_consumer_demand(limiter_consumer_id, False)

        def abort_small_model_translation(error: TranslationRequestError) -> None:
            """Stop dispatch and cancel sibling streams after the first batch error."""
            nonlocal fatal_error_message
            if not small_model_mode:
                return
            should_cancel = False
            with fatal_error_lock:
                if fatal_error_message is None:
                    fatal_error_message = _small_model_terminal_error_message(error)
                    fatal_error_event.set()
                    should_cancel = True
            if should_cancel and self.api is not None:
                cancel_requests = getattr(self.api, "cancel_requests", None)
                if callable(cancel_requests):
                    cancel_requests()

        # 结果容器：与原文行数对齐，失败行保持空字符串
        all_translated_lines: List[str] = [""] * total_lines
        failed_indices: List[int] = []
        last_error: str | None = None
        any_success = False
        # 截断是运行级的结构性信息：任一批被截断即意味着整批输出不可信。
        output_truncated = False
        truncation_reason: str | None = None

        progress_lock = threading.Lock()
        # PERF §9.6：以 TranslationBatchPlan.start 作为批次键（保持与原 tuple
        # 语义一致：start 单调递增且唯一）。
        completed_by_batch = {plan.start: 0 for plan in batch_ranges}
        completed_line_count = 0
        batch_starts = tuple(plan.start for plan in batch_ranges)
        finished_batches: set[int] = set()
        next_unfinished_batch_index = 0

        def display_batch_start() -> int | None:
            """Return the earliest unfinished batch in O(1) while holding the lock."""
            if next_unfinished_batch_index >= len(batch_starts):
                return None
            return batch_starts[next_unfinished_batch_index]

        def mark_batch_finished(batch_start: int) -> int | None:
            """Advance the unfinished-batch pointer without rescanning all batches."""
            nonlocal next_unfinished_batch_index
            finished_batches.add(batch_start)
            while (
                next_unfinished_batch_index < len(batch_starts)
                and batch_starts[next_unfinished_batch_index] in finished_batches
            ):
                next_unfinished_batch_index += 1
            return display_batch_start()

        def batch_progress(progress, data):
            nonlocal completed_line_count
            if not data:
                return
            batch_start = data.get("batch_start", 0)
            completed = int(data.get("completed_lines", 0))
            # PERF：只读/写共享状态时持锁，外部 callback 必须在锁外调用，
            # 避免 UI 繁忙时阻塞其他批次的进度更新。
            with progress_lock:
                if data.get("streaming"):
                    previous_completed = completed_by_batch.get(batch_start, 0)
                    if completed > previous_completed:
                        completed_by_batch[batch_start] = completed
                        completed_line_count += completed - previous_completed
                overall = completed_line_count / total_lines * 100 if total_lines else 100.0
                # Later concurrent batches may update rows, but only the earliest
                # unfinished batch is allowed to control the UI viewport.
                display_start = display_batch_start()
            event_data = dict(data)
            event_data["display_batch_start"] = display_start
            progress_callback(min(overall, 100.0), event_data)

        def submit_batch(executor, plan: TranslationBatchPlan):
            batch_source_lines = lines[plan.start : plan.end]

            def _run_batch():
                limiter_acquired = False
                if shared_limiter is not None:
                    limiter_acquired = shared_limiter.acquire(
                        estimated_tokens=plan.estimated_input_tokens,
                        consumer_id=limiter_consumer_id,
                        cancelled=lambda: self.is_stopped or fatal_error_event.is_set(),
                    )
                    if not limiter_acquired:
                        raise TranslationCancelled()
                    mark_limiter_request_sent()
                try:
                    result = self._translate_batch(
                        batch_source_lines,
                        batch_progress,
                        plan.start,
                        total_lines,
                        emit_stream_progress,
                        run_context,
                    )
                    if small_model_mode and result.status != TranslationStatus.SUCCEEDED:
                        raise TranslationRequestError(
                            result.error_message or "小模型逐行翻译未返回有效译文",
                            failed_indices=list(range(len(batch_source_lines))),
                        )
                except TranslationRequestError as exc:
                    abort_small_model_translation(exc)
                    if shared_limiter is not None:
                        if exc.status_code == 429:
                            shared_limiter.record_rate_limited(
                                retry_after_seconds=exc.retry_after_seconds
                            )
                        elif exc.status_code in (408, 504) or "timeout" in str(exc).lower():
                            shared_limiter.record_timeout()
                    raise
                except TimeoutError:
                    if shared_limiter is not None:
                        shared_limiter.record_timeout()
                    raise
                else:
                    if shared_limiter is not None:
                        shared_limiter.record_success()
                    return result
                finally:
                    if shared_limiter is not None and limiter_acquired:
                        shared_limiter.release(consumer_id=limiter_consumer_id)

            return executor.submit(
                _run_batch,
            )

        next_batch = 0
        # PERF §9.6：in_flight 值由 tuple[int, int] 改为 TranslationBatchPlan，
        # 便于完成后记录批次元信息（如 estimated_input_tokens）到日志。
        in_flight: dict[Future, TranslationBatchPlan] = {}
        executor = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="translation-batch"
        )
        try:
            while (
                (next_batch < len(batch_ranges) or in_flight)
                and not self.is_stopped
                and not fatal_error_event.is_set()
            ):
                self.pause_event.wait()
                if self.is_stopped:
                    break

                recommended = concurrency
                recommendation = getattr(self.api, "recommended_concurrency", None)
                if callable(recommendation):
                    provider_recommendation = _as_int(
                        recommendation(concurrency), default=concurrency, minimum=1
                    )
                    recommended = min(concurrency, provider_recommendation)

                while (
                    next_batch < len(batch_ranges)
                    and len(in_flight) < recommended
                    and not self.is_stopped
                    and not fatal_error_event.is_set()
                ):
                    plan = batch_ranges[next_batch]
                    in_flight[submit_batch(executor, plan)] = plan
                    next_batch += 1

                if not in_flight:
                    continue

                done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    plan = in_flight.pop(future)
                    batch_start = plan.start
                    batch_end = plan.end
                    try:
                        batch_result = future.result()
                    except TranslationRequestError as exc:
                        with progress_lock:
                            mark_batch_finished(batch_start)
                        failed_indices.extend(range(batch_start, batch_end))
                        last_error = str(exc)
                        logger.error(
                            "批次 %s-%s 翻译失败: %s",
                            batch_start,
                            batch_end - 1,
                            exc,
                        )
                        if fatal_error_event.is_set():
                            break
                        continue
                    except TranslationCancelled:
                        if not fatal_error_event.is_set():
                            self.is_stopped = True
                        break

                    if batch_result.status == TranslationStatus.CANCELLED:
                        if not fatal_error_event.is_set():
                            self.is_stopped = True
                        break

                    for index, translated in enumerate(batch_result.lines):
                        all_translated_lines[batch_start + index] = translated
                    failed_indices.extend(
                        batch_start + index for index in batch_result.failed_indices
                    )

                    if batch_result.status in (
                        TranslationStatus.SUCCEEDED,
                        TranslationStatus.PARTIAL,
                    ):
                        any_success = True
                    if batch_result.error_message:
                        last_error = batch_result.error_message
                    # 任一批次被 provider 明确截断时，本次运行必须把该结构性
                    # 信息带到顶层结果，调用方才能区分"HTTP 成功"与"输出完整"。
                    if batch_result.output_truncated:
                        output_truncated = True
                        truncation_reason = batch_result.finish_reason or truncation_reason

                    with progress_lock:
                        previous_completed = completed_by_batch.get(batch_start, 0)
                        batch_completed = batch_end - batch_start
                        if batch_completed > previous_completed:
                            completed_by_batch[batch_start] = batch_completed
                            completed_line_count += batch_completed - previous_completed
                        overall_progress = (
                            completed_line_count / total_lines * 100 if total_lines else 100.0
                        )
                        display_start = mark_batch_finished(batch_start)
                    # PERF：在锁外调用外部 callback，避免 UI 繁忙时阻塞其他批次。
                    progress_callback(
                        overall_progress,
                        {
                            "batch_start": batch_start,
                            "translated_lines": batch_result.lines,
                            "streaming": False,
                            "display_batch_start": display_start,
                        },
                    )

        except Exception as e:
            # 非预期异常：构造失败结果并回调
            logger.exception("翻译流程发生未预期异常")
            result = BatchTranslationResult(
                status=TranslationStatus.FAILED,
                lines=all_translated_lines,
                failed_indices=failed_indices or list(range(total_lines)),
                error_message=str(e),
            )
            complete_callback(result)
            return
        finally:
            for future in in_flight:
                future.cancel()
            executor.shutdown(
                wait=not (self.is_stopped or fatal_error_event.is_set()),
                cancel_futures=True,
            )
            if shared_limiter is not None and limiter_consumer_id is not None:
                shared_limiter.unregister_consumer(limiter_consumer_id)

        # 根据停止状态和成功情况决定最终状态
        if fatal_error_message is not None:
            status = TranslationStatus.FAILED
            last_error = fatal_error_message
            failed_indices = sorted(
                set(failed_indices).union(
                    index
                    for index, translated in enumerate(all_translated_lines)
                    if not translated.strip()
                )
            )
        elif self.is_stopped:
            status = TranslationStatus.CANCELLED
        elif not failed_indices:
            status = TranslationStatus.SUCCEEDED
        elif any_success:
            status = TranslationStatus.PARTIAL
        else:
            status = TranslationStatus.FAILED

        if output_truncated and last_error is None:
            last_error = _truncated_result_message(truncation_reason)

        result = BatchTranslationResult(
            status=status,
            lines=all_translated_lines,
            failed_indices=failed_indices,
            error_message=last_error,
            output_truncated=output_truncated,
            finish_reason=truncation_reason,
        )
        complete_callback(result)

    def _resolve_protocol_text(self, run_context: TranslationRunContext | None) -> str:
        """返回该运行每批都要发送的固定协议文本。

        Hunyuan-MT 分支不使用 system 提示词，协议只有 ``将以下文本翻译为X：``
        这一行；其余模型使用 system prompt。
        """
        if run_context is not None:
            if run_context.is_hunyuan:
                return ""
            return run_context.system_prompt or ""
        app_config = self.config_manager.get_app_config()
        target_language = app_config.get("target_language", "中文")
        if self._is_hunyuan_mt():
            return ""
        base_prompt = app_config.get("translation_prompt", "")
        glossary_prompt = self.config_manager.get_glossary_prompt()
        return self._translation_system_prompt(target_language, base_prompt, glossary_prompt)

    def _build_batch_ranges(
        self,
        lines: List[str],
        max_lines: int | None,
        input_token_budget: int,
        *,
        resolved_budget: ResolvedContextBudget,
        run_context: TranslationRunContext | None = None,
    ) -> List[TranslationBatchPlan]:
        """PERF §9.6：构造批次计划只读对象列表，取代散落的 ``(start, end)`` tuple。

        计划算法来自 ``application.batch_planner``——主界面、队列和引擎共用
        同一份 token 分批逻辑，避免复制第二套提示词开销算法。

        ``max_lines`` 为 ``None`` 表示应用不设行数上限（超长模式的
        ``ResolvedContextBudget.max_batch_lines``）。

        Raises:
            BatchPlanningError: 超长模式下单行（或协议开销本身）无法满足预算。
        """
        protocol = compute_protocol_overhead(self._resolve_protocol_text(run_context))
        planned = plan_batches(
            lines,
            range(len(lines)),
            budget=resolved_budget,
            protocol=protocol,
        )
        return [
            TranslationBatchPlan(
                batch_id=batch.batch_id,
                start=batch.source_indices[0],
                end=batch.source_indices[-1] + 1,
                estimated_input_tokens=batch.estimated_input_tokens,
                output_budget_tokens=batch.output_budget_tokens,
            )
            for batch in planned
        ]

    @staticmethod
    def _translation_system_prompt(
        target_language: str, base_prompt: str, glossary_prompt: str
    ) -> str:
        parts = [part.strip() for part in (base_prompt, glossary_prompt) if part.strip()]
        parts.append(
            f"将用户文本翻译为{target_language}。保留每个 [LINE_XXX] 标记，"
            "逐行对应输出，除译文外不要添加说明。"
        )
        return "\n\n".join(parts)

    def _last_finish_reason(self) -> str | None:
        """读取**本线程**上次流式请求的结束原因。

        按线程保存（见 ``BaseAPI.last_finish_reason``）：同一 API 实例被多个
        批次线程并发使用，共享一个 ``last_finish_reason`` 会让并发请求互相
        读取对方的状态。不支持该接口的 provider 返回 ``None``（能力未知时
        明确是估算，不编造截断信号）。
        """
        if self.api is None:
            return None
        getter = getattr(self.api, "last_finish_reason", None)
        if not callable(getter):
            return None
        try:
            reason = getter()
        except Exception as exc:  # noqa: BLE001 - 诊断信息不应中断翻译
            logger.debug("读取 finish_reason 失败: %s", exc)
            return None
        return reason if isinstance(reason, str) and reason else None

    def _is_hunyuan_mt(self) -> bool:
        """判断当前模型是否为混元翻译模型"""
        api_config = self.config_manager.get_api_config()
        model_name = api_config.get("model_name", "")
        return "Hunyuan-MT" in model_name or "hunyuan-mt" in model_name.lower()

    def _clean_hunyuan_response(self, response: str, expected_lines: int) -> List[str]:
        """清理 Hunyuan-MT 的翻译响应，确保行数对齐"""
        # 移除可能残留的指令性文本
        cleaned = response.strip()
        for noise in ["将以下文本翻译为", "翻译结果", "翻译如下", "以下是翻译"]:
            if cleaned.startswith(noise):
                cleaned = cleaned[len(noise) :].lstrip("：:\n ")

        lines = cleaned.split("\n")
        # 移除首尾空白行
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        # 确保行数与原文对齐
        if len(lines) < expected_lines:
            lines.extend([""] * (expected_lines - len(lines)))
        elif len(lines) > expected_lines:
            lines = lines[:expected_lines]

        return lines

    def _translate_batch(
        self,
        batch_lines: List[str],
        progress_callback: Callable,
        batch_start: int,
        total_lines: int | None = None,
        emit_stream_progress: bool = True,
        run_context: TranslationRunContext | None = None,
    ) -> BatchTranslationResult:
        """翻译一批原文行，使用流式输出提升体验（增强：行号标记机制）

        R2-BUG-009 / R2-BUG-010 修复要点：
        - 取消时抛出 TranslationCancelled，不再返回伪造的空译文列表。
        - 解析后对每个非空原文验证对应译文是否非空，缺失计入 failed_indices。
        - 返回 BatchTranslationResult，状态为 SUCCEEDED / PARTIAL。

        PERF §9.5：``run_context`` 由 ``_translate`` 一次构造后传入，
        避免每批重复读取配置、构造术语提示和判断模型类型。为兼容旧调用方
        保留 ``None`` 回退路径（按需重新构造）。

        核心逻辑：
        1. 为每行原文添加行号标记，确保API返回时能正确对齐
        2. 使用流式翻译，实时显示结果
        3. 翻译完成后，解析行号标记并按序排列译文
        4. 确保返回的译文行数 = 原文行数
        5. API 请求重试只由 API 层统一负责
        """
        if not batch_lines:
            return BatchTranslationResult(
                status=TranslationStatus.SUCCEEDED,
                lines=[],
            )

        expected_lines = len(batch_lines)
        # PERF §9.5：优先复用运行上下文，避免逐批重复读取配置。
        if run_context is not None:
            target_language = run_context.target_language
            is_hunyuan = run_context.is_hunyuan
            system_prompt = run_context.system_prompt
        else:
            app_config = self.config_manager.get_app_config()
            target_language = app_config.get("target_language", "中文")
            is_hunyuan = self._is_hunyuan_mt()
            if not is_hunyuan:
                base_prompt = app_config.get("translation_prompt", "")
                glossary_prompt = self.config_manager.get_glossary_prompt()
                system_prompt = self._translation_system_prompt(
                    target_language, base_prompt, glossary_prompt
                )
            else:
                system_prompt = None

        if is_hunyuan:
            # Hunyuan-MT 专用：不加行号标记，不加复杂提示词
            batch_content = "\n".join(batch_lines)
            user_prompt = f"将以下文本翻译为{target_language}：\n{batch_content}"
        else:
            # 通用 LLM：稳定指令放到 system，便于 provider 复用前缀缓存。
            marked_lines = []
            for i, line in enumerate(batch_lines):
                line_marker = f"[LINE_{i + 1:03d}]"
                marked_lines.append(f"{line_marker}{line}")

            batch_content = "\n".join(marked_lines)
            user_prompt = batch_content

        # 网络与 HTTP 重试统一由 API 层负责，翻译层只解析一次响应。
        max_retries = 1
        for _retry_count in range(max_retries):
            try:
                # PERF-001：增量行解析，避免每个 chunk 全量拼接 O(n²)
                stream_buffer = []  # 仅用于响应为空时的回退（一次性 join）
                # 未形成完整行的片段列表。不能使用 ``pending += chunk``：
                # 单一超长行跨越大量分片时那仍会产生 O(n^2) 复制。
                pending_parts: List[str] = []
                preview_lines: List[str] = []
                # 通用 LLM 的预览必须与最终解析使用同一套行号协议。
                # 若直接按物理换行累加，模型用于排版的空白行会暂时占用
                # 源文行位，直到批次完成后才被最终解析结果纠正。
                marked_preview_lines: dict[int, str] = {}
                published_prefix_count = 0
                completed_count = 0

                def stream_callback(chunk):
                    """流式回调：增量解析完整行，避免全量拼接"""
                    nonlocal completed_count, published_prefix_count
                    stream_buffer.append(chunk)

                    # 队列翻译只关心整批结果。跳过分片解析和 UI 回调可显著
                    # 降低大批次响应期间的 Python/Tk 调度开销。
                    if not emit_stream_progress:
                        return

                    stream_start_line = completed_count if is_hunyuan else published_prefix_count
                    chunk_parts = chunk.split("\n")
                    if len(chunk_parts) == 1:
                        pending_parts.append(chunk)
                        new_lines = []
                    else:
                        # 遇到换行时完成当前行，最后一个片段留作实时预览。
                        first_line = "".join(pending_parts) + chunk_parts[0]
                        pending_parts.clear()
                        parts = [first_line, *chunk_parts[1:-1]]
                        pending_parts.append(chunk_parts[-1])

                        if is_hunyuan:
                            new_lines = parts
                            stream_start_line = completed_count
                            preview_lines.extend(new_lines)
                            completed_count += len(new_lines)
                        else:
                            # 只接受能够确认目标行号的完整行。空白分隔行、
                            # Markdown 说明和其他未标记文本不会推进预览位置。
                            for line in parts:
                                match = _LINE_MARKER_PARSE_RE.match(line)
                                if not match:
                                    continue
                                line_index = int(match.group("line_number")) - 1
                                if 0 <= line_index < expected_lines:
                                    marked_preview_lines[line_index] = match.group("content")

                            completed_count = len(marked_preview_lines)
                            completed_prefix_count = 0
                            while completed_prefix_count in marked_preview_lines:
                                completed_prefix_count += 1

                            stream_start_line = published_prefix_count
                            new_lines = [
                                marked_preview_lines[index]
                                for index in range(published_prefix_count, completed_prefix_count)
                            ]
                            published_prefix_count = completed_prefix_count

                    partial_line = "".join(pending_parts)
                    if is_hunyuan:
                        current_preview = list(preview_lines)
                        if partial_line:
                            current_preview.append(partial_line)
                    else:
                        # 当前未换行的内容也只有在完整行号标记已出现后才展示。
                        # 标记本身已完整但正文仍为空时不提前清空对应单元格。
                        current_values = dict(marked_preview_lines)
                        partial_match = _LINE_MARKER_PARSE_RE.match(partial_line)
                        if partial_match and partial_match.group("content"):
                            line_index = int(partial_match.group("line_number")) - 1
                            if 0 <= line_index < expected_lines:
                                current_values[line_index] = partial_match.group("content")

                        current_preview = []
                        while len(current_preview) in current_values:
                            current_preview.append(current_values[len(current_preview)])

                    # 计算流式阶段的进度
                    if total_lines and total_lines > 0:
                        base_progress = (batch_start / total_lines) * 100
                        batch_weight = (expected_lines / total_lines) * 100
                        line_progress = (
                            completed_count / expected_lines if expected_lines > 0 else 0
                        )
                        streaming_progress = base_progress + (batch_weight * line_progress)
                        streaming_progress = min(streaming_progress, 100.0)
                    else:
                        streaming_progress = 0

                    progress_callback(
                        streaming_progress,
                        {
                            "batch_start": batch_start,
                            "streaming": True,
                            # 只传递本次新增的完整行。UI 直接更新对应行，
                            # 不需要接收并重新拆分全部历史流式文本。
                            "stream_lines": new_lines,
                            "stream_start_line": stream_start_line,
                            # 完整快照可被 UI 安全合并，并实时显示尚未换行的当前行。
                            "preview_lines": current_preview,
                            "expected_lines": expected_lines,
                            "completed_lines": completed_count,
                        },
                    )

                # 调用流式翻译API
                if self.api is None:
                    raise Exception("API client not initialized")
                # UXF-012：缓存命中必须与模型、目标语言和提示词/术语上下文
                # 语义一致。旧 provider 仍可只实现 translate_stream。
                enhanced_stream = getattr(self.api, "translate_stream_enhanced", None)
                if callable(enhanced_stream):
                    # PERF §9.5 D2/D3：优先复用运行上下文中的 temperature 和
                    # 提示词/术语哈希，避免每批重读 api_config 与重算 SHA256。
                    if run_context is not None:
                        cache_provider = run_context.provider
                        cache_model_name = run_context.model_name
                        cache_temperature = run_context.temperature
                        cache_prompt_version = run_context.prompt_version
                        cache_prompt_schema_version = run_context.prompt_schema_version
                        cache_glossary_version = run_context.glossary_version
                    else:
                        api_cfg = self.config_manager.get_api_config()
                        cache_provider = api_cfg.get("provider", "")
                        cache_model_name = api_cfg.get("model_name", "")
                        cache_base_prompt = app_config.get("translation_prompt", "")
                        cache_prompt_schema_version = int(
                            app_config.get("prompt_schema_version", 1)
                        )
                        cache_glossary_prompt = self.config_manager.get_glossary_prompt()
                        cache_temperature = float(api_cfg.get("temperature", 0.3))
                        cache_prompt_version = hashlib.sha256(
                            f"schema:{cache_prompt_schema_version}\n{cache_base_prompt}".encode()
                        ).hexdigest()[:16]
                        cache_glossary_version = hashlib.sha256(
                            cache_glossary_prompt.encode("utf-8")
                        ).hexdigest()[:16]
                    cache_context = {
                        "provider": cache_provider,
                        "target_language": target_language,
                        "model_name": cache_model_name,
                        "temperature": cache_temperature,
                        "prompt_version": cache_prompt_version,
                        "prompt_schema_version": cache_prompt_schema_version,
                        "glossary_version": cache_glossary_version,
                        "normalization_version": "line-marker-v2",
                    }
                    response = enhanced_stream(
                        user_prompt,
                        stream_callback,
                        cache_context,
                        system_prompt=system_prompt,
                    )
                else:
                    # 旧 provider 不支持 system_prompt 时保持单字符串兼容。
                    legacy_prompt = (
                        f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
                    )
                    response = self.api.translate_stream(legacy_prompt, stream_callback)

                # 检查翻译结果是否有效
                response_text = response if isinstance(response, str) else ""
                if not response_text or not response_text.strip():
                    # R2-BUG-009：用户取消时抛出 TranslationCancelled，不再返回空列表
                    if self.is_stopped:
                        raise TranslationCancelled(
                            partial_lines=[""] * expected_lines,
                        )
                    # 没有翻译结果，使用流式缓冲区的内容（可能为空）
                    translated_content = "".join(stream_buffer).strip()
                    if not translated_content:
                        raise TranslationRequestError(
                            "翻译失败：API 未返回任何内容",
                            failed_indices=list(range(expected_lines)),
                        )
                else:
                    translated_content = response_text.strip()

                # 解析翻译结果
                if is_hunyuan:
                    # Hunyuan-MT：直接按换行拆分，清理响应
                    translated_lines = self._clean_hunyuan_response(
                        translated_content, expected_lines
                    )
                else:
                    # 通用 LLM：解析行号标记并按序排列译文
                    all_lines = translated_content.split("\n")

                    line_mapping = {}
                    unmarked_lines = []

                    for line in all_lines:
                        match = _LINE_MARKER_PARSE_RE.match(line)
                        if match:
                            line_num = int(match.group("line_number"))
                            content = match.group("content")
                            line_mapping[line_num] = content
                        else:
                            if line.strip():
                                unmarked_lines.append(line)

                    translated_lines = []
                    for i in range(1, expected_lines + 1):
                        if i in line_mapping:
                            translated_lines.append(line_mapping[i])
                        else:
                            if unmarked_lines:
                                translated_lines.append(unmarked_lines.pop(0))
                            else:
                                translated_lines.append("")

                # 关键：确保译文行数 = 原文行数
                if len(translated_lines) < expected_lines:
                    translated_lines.extend([""] * (expected_lines - len(translated_lines)))
                elif len(translated_lines) > expected_lines:
                    translated_lines = translated_lines[:expected_lines]

                # R2-BUG-010：检测缺失译文
                # 对每个非空原文验证对应译文是否非空；
                # 缺失、重复、越界和无法解析的行号都形成失败索引。
                # 原文本身为空的行不计为失败。
                batch_failed: List[int] = []
                for i, (src, tgt) in enumerate(zip(batch_lines, translated_lines, strict=False)):
                    if src.strip() and not tgt.strip():
                        batch_failed.append(i)

                # 截断是**结构化**信息，不能仅凭 HTTP 成功宣告整批成功。
                # provider 返回 ``finish_reason=length`` 时输出必然不完整，
                # 缺失的行保持失败并进入现有补漏流程，而不是被补齐成假成功。
                finish_reason = self._last_finish_reason()
                truncated = finish_reason == "length"

                if truncated and not batch_failed:
                    # 解析结果看似完整但被明确截断：整批不可信，全部重试/补漏。
                    batch_failed = list(range(expected_lines))

                if batch_failed:
                    error_message = (
                        _truncated_result_message(finish_reason)
                        if truncated
                        else f"批次内 {len(batch_failed)} 行译文缺失"
                    )
                    # 存在缺失译文：返回 PARTIAL，不写入成功缓存。
                    return BatchTranslationResult(
                        status=TranslationStatus.PARTIAL,
                        lines=translated_lines,
                        failed_indices=batch_failed,
                        error_message=error_message,
                        output_truncated=truncated,
                        finish_reason=finish_reason,
                    )

                # 全部必需行都有译文：返回 SUCCEEDED
                return BatchTranslationResult(
                    status=TranslationStatus.SUCCEEDED,
                    lines=translated_lines,
                    finish_reason=finish_reason,
                )

            except TranslationRequestError as exc:
                if not exc.failed_indices:
                    exc.failed_indices = list(range(expected_lines))
                raise
            except TranslationCancelled:
                # R2-BUG-009：取消异常直接向上传播
                raise
            except Exception as e:
                # R2-BUG-009：用户取消时抛出 TranslationCancelled，不再返回空列表
                if self.is_stopped:
                    raise TranslationCancelled(
                        partial_lines=[""] * expected_lines,
                    )
                logger.error("翻译请求失败: %s", e)
                # 队列并发优化（§8.2）：透传 retry_after_seconds 给共享 ProviderLimiter。
                raise TranslationRequestError(
                    f"翻译失败：{e}",
                    failed_indices=list(range(expected_lines)),
                    status_code=getattr(e, "status_code", None),
                    retry_after_seconds=getattr(e, "retry_after_seconds", None),
                ) from e

        # 兜底：所有重试均未返回（理论上不会到达），视为失败
        raise TranslationRequestError(
            "翻译失败：未获取到任何结果",
            failed_indices=list(range(expected_lines)),
        )

    def stop(self):
        """停止翻译"""
        self.is_stopped = True
        self.pause_event.set()  # 确保不卡在暂停状态
        # 取消所有正在进行的API请求
        if self.api:
            self.api.cancel_requests()

    def pause(self):
        """暂停翻译（在批次间生效）"""
        self.pause_event.clear()

    def resume(self):
        """恢复翻译"""
        self.pause_event.set()

    def reset(self):
        """重置状态"""
        self.is_stopped = False
        self.pause_event.set()  # 重置暂停状态
        # 重置API取消状态
        if self.api:
            self.api.reset_cancel()
