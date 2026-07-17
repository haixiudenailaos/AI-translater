#!/usr/bin/env python3
"""
翻译引擎模块
负责协调翻译流程，分批翻译 + 流式输出
"""

import hashlib
import re
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, List

from ..api.deepseek_api import DeepseekAPI
from ..api.openai_compatible_api import OpenAICompatibleAPI
from ..api.siliconflow_api import SiliconFlowAPI
from ..config.translation_profile import (
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
    apply_text_translation_profile,
)
from ..utils.logger import get_logger
from ..utils.token_estimator import estimate_tokens
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

# PERF-001：预编译行号标记正则，避免在热路径重复编译
_LINE_MARKER_RE = re.compile(r"\[LINE_\d+\]")
_LINE_MARKER_PARSE_RE = re.compile(r"^\[LINE_(\d+)\](.*)$")


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
    glossary_version: str = ""


@dataclass(frozen=True, slots=True)
class TranslationBatchPlan:
    """PERF §9.6：批次计划只读对象。

    取代散落的 ``(start, end)`` tuple，便于在性能日志中记录批次大小和
    token 估算。
    """

    batch_id: int
    start: int
    end: int
    estimated_input_tokens: int


def _clean_stream_line(line: str) -> str:
    """隐藏尚未接收完整的行号标记，避免协议文本闪现在 UI 中。"""
    cleaned = _LINE_MARKER_RE.sub("", line)
    if cleaned != line:
        return cleaned
    if "[LINE_".startswith(line) or (line.startswith("[LINE_") and "]" not in line):
        return ""
    return line


class TranslatorEngine:
    def __init__(
        self,
        config_manager,
        *,
        limiter_registry: ProviderLimiterRegistry | None = None,
    ):
        self.config_manager = config_manager
        self.api = None
        self.is_stopped = False
        # 仅主编辑器注入。队列引擎由 Coordinator 在派发前直接取得同一 Limiter，
        # 因而不会在这里重复取槽。
        self._limiter_registry = limiter_registry

        # 暂停/恢复支持
        self.pause_event = threading.Event()
        self.pause_event.set()  # 默认不暂停

        # 延迟初始化API与正则（按需构建）
        self._re_many_newlines = None
        # 不在构造时初始化 API，首次使用时再构建

    def _init_api(self):
        """初始化API客户端。

        BUG-005：覆盖旧实例前先关闭，避免连接和心跳线程累积。
        """
        # 先关闭旧实例（如果有）
        if self.api is not None:
            try:
                self.api.close()
            except Exception as e:
                logger.warning("关闭旧API实例失败: %s", e)
            self.api = None

        api_config = apply_text_translation_profile(self.config_manager.get_api_config())
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

    def build_run_context(self) -> TranslationRunContext:
        """PERF §9.5 + 队列并发优化阶段 2：构造不可变运行上下文。

        供 ``QueueTranslationCoordinator`` 在每次任务尝试开始时调用一次，
        复用给该任务的所有 ``BatchJob``，避免逐批重复读取配置和构造提示词。
        必须在 ``_ensure_api`` 之后调用。
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
        run_prompt_version = hashlib.sha256(base_prompt.encode("utf-8")).hexdigest()[:16]
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
            glossary_version=run_glossary_version,
        )

    def compute_input_token_budget(self, configured_budget: int) -> int:
        """计算上下文安全的输入 token 预算（队列并发优化阶段 2）。

        供 ``BatchPlanner`` 使用：受模型 context window、output reserve 和
        API 实例的 ``recommended_input_budget`` 共同约束。
        """
        api_config = self.config_manager.get_api_config()
        configured = max(512, int(configured_budget))
        budget_recommendation = getattr(self.api, "recommended_input_budget", None)
        if callable(budget_recommendation):
            configured = budget_recommendation(configured)
        context_window = max(4096, int(api_config.get("context_window_tokens", 32768)))
        output_reserve = max(256, int(api_config.get("max_tokens", 4000)))
        context_safe_budget = max(512, context_window - output_reserve - 1024)
        return min(configured, context_safe_budget)

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
        """
        self._init_api()

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

        lines = content.split("\n")
        total_lines = len(lines)
        app_config = self.config_manager.get_app_config()
        api_config = self.config_manager.get_api_config()
        if batch_lines_override is None:
            batch_lines = min(
                MAX_STABLE_TRANSLATION_BATCH_LINES,
                max(1, int(app_config.get("batch_lines", DEFAULT_TRANSLATION_BATCH_LINES))),
            )
        else:
            batch_lines = max(1, int(batch_lines_override))
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
        budget_recommendation = getattr(self.api, "recommended_input_budget", None)
        if callable(budget_recommendation):
            configured_input_budget = budget_recommendation(configured_input_budget)
        context_window = max(4096, int(api_config.get("context_window_tokens", 32768)))
        output_reserve = max(256, int(api_config.get("max_tokens", 4000)))
        context_safe_budget = max(512, context_window - output_reserve - 1024)
        input_token_budget = min(configured_input_budget, context_safe_budget)
        if concurrency_override is None:
            concurrency = max(
                1,
                min(
                    8,
                    int(app_config.get("translation_concurrency", DEFAULT_TRANSLATION_CONCURRENCY)),
                ),
            )
        else:
            concurrency = max(1, min(8, int(concurrency_override)))

        # PERF §9.5：一次构造不可变运行上下文。
        # 后续所有批次复用同一份配置/提示词，避免逐批重复读取和构造。
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
        # PERF §9.5 D2/D3：temperature 与提示词/术语哈希一次计算，
        # 避免 _translate_batch 在每个批次的 cache_context 块重读 api_config
        # 并重算 SHA256（热路径哈希开销显著）。
        run_temperature = float(api_config.get("temperature", 0.3))
        run_prompt_version = hashlib.sha256(base_prompt.encode("utf-8")).hexdigest()[:16]
        run_glossary_version = hashlib.sha256(glossary_prompt.encode("utf-8")).hexdigest()[:16]
        run_context = TranslationRunContext(
            provider=api_config.get("provider", ""),
            model_name=model_name,
            target_language=target_language,
            base_prompt=base_prompt,
            glossary_prompt=glossary_prompt,
            system_prompt=system_prompt,
            is_hunyuan=is_hunyuan,
            temperature=run_temperature,
            prompt_version=run_prompt_version,
            glossary_version=run_glossary_version,
        )

        # 主编辑器与后台队列共用同一个 Provider 请求额度。主编辑器仍保留
        # translation_concurrency 作为本次任务的局部并发上限，但实际网络请求
        # 还必须通过应用级 Limiter，因此两条路径的在途总数不会相加失控。
        shared_limiter = self._get_shared_limiter(api_config)

        batch_ranges = self._build_batch_ranges(lines, batch_lines, input_token_budget)

        # 结果容器：与原文行数对齐，失败行保持空字符串
        all_translated_lines: List[str] = [""] * total_lines
        failed_indices: List[int] = []
        last_error: str | None = None
        any_success = False

        progress_lock = threading.Lock()
        # PERF §9.6：以 TranslationBatchPlan.start 作为批次键（保持与原 tuple
        # 语义一致：start 单调递增且唯一）。
        completed_by_batch = {plan.start: 0 for plan in batch_ranges}
        batch_starts = tuple(plan.start for plan in batch_ranges)
        finished_batches: set[int] = set()

        def display_batch_start():
            """Return the earliest unfinished batch while holding progress_lock."""
            return next(
                (start for start in batch_starts if start not in finished_batches),
                None,
            )

        def batch_progress(progress, data):
            if not data:
                return
            batch_start = data.get("batch_start", 0)
            completed = int(data.get("completed_lines", 0))
            # PERF：只读/写共享状态时持锁，外部 callback 必须在锁外调用，
            # 避免 UI 繁忙时阻塞其他批次的进度更新。
            with progress_lock:
                if data.get("streaming"):
                    completed_by_batch[batch_start] = max(
                        completed_by_batch.get(batch_start, 0), completed
                    )
                overall = (
                    sum(completed_by_batch.values()) / total_lines * 100 if total_lines else 100.0
                )
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
                        cancelled=lambda: self.is_stopped,
                    )
                    if not limiter_acquired:
                        raise TranslationCancelled()
                try:
                    result = self._translate_batch(
                        batch_source_lines,
                        batch_progress,
                        plan.start,
                        total_lines,
                        emit_stream_progress,
                        run_context,
                    )
                except TranslationRequestError as exc:
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
                        shared_limiter.release()

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
            while (next_batch < len(batch_ranges) or in_flight) and not self.is_stopped:
                self.pause_event.wait()
                if self.is_stopped:
                    break

                recommended = concurrency
                recommendation = getattr(self.api, "recommended_concurrency", None)
                if callable(recommendation):
                    recommended = max(1, min(concurrency, recommendation(concurrency)))

                while (
                    next_batch < len(batch_ranges)
                    and len(in_flight) < recommended
                    and not self.is_stopped
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
                            finished_batches.add(batch_start)
                        failed_indices.extend(range(batch_start, batch_end))
                        last_error = str(exc)
                        logger.error(
                            "批次 %s-%s 翻译失败: %s",
                            batch_start,
                            batch_end - 1,
                            exc,
                        )
                        continue
                    except TranslationCancelled:
                        self.is_stopped = True
                        break

                    if batch_result.status == TranslationStatus.CANCELLED:
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

                    with progress_lock:
                        finished_batches.add(batch_start)
                        completed_by_batch[batch_start] = batch_end - batch_start
                        overall_progress = (
                            sum(completed_by_batch.values()) / total_lines * 100
                            if total_lines
                            else 100.0
                        )
                        display_start = display_batch_start()
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
            executor.shutdown(wait=not self.is_stopped, cancel_futures=True)

        # 根据停止状态和成功情况决定最终状态
        if self.is_stopped:
            status = TranslationStatus.CANCELLED
        elif not failed_indices:
            status = TranslationStatus.SUCCEEDED
        elif any_success:
            status = TranslationStatus.PARTIAL
        else:
            status = TranslationStatus.FAILED

        result = BatchTranslationResult(
            status=status,
            lines=all_translated_lines,
            failed_indices=failed_indices,
            error_message=last_error,
        )
        complete_callback(result)

    def _build_batch_ranges(
        self, lines: List[str], max_lines: int, input_token_budget: int
    ) -> List[TranslationBatchPlan]:
        """PERF §9.6：构造批次计划只读对象列表，取代散落的 ``(start, end)`` tuple。

        便于在性能日志中记录批次大小和 token 估算，并避免调用方解包 tuple
        时出错。每个 ``TranslationBatchPlan`` 携带 ``batch_id``（按序递增）、
        ``start`` / ``end``（行区间，左闭右开）和 ``estimated_input_tokens``
        （该批 token 估算，含协议开销）。
        """
        app_config = self.config_manager.get_app_config()
        target_language = app_config.get("target_language", "中文")
        base_prompt = app_config.get("translation_prompt", "")
        glossary_prompt = self.config_manager.get_glossary_prompt()
        protocol = self._translation_system_prompt(target_language, base_prompt, glossary_prompt)
        fixed_tokens = estimate_tokens(protocol)
        plans: List[TranslationBatchPlan] = []
        start = 0
        batch_id = 0
        while start < len(lines):
            end = start
            used_tokens = fixed_tokens
            while end < len(lines) and end - start < max_lines:
                marker = f"[LINE_{end - start + 1:03d}]"
                line_tokens = estimate_tokens(marker + lines[end] + "\n")
                if end > start and used_tokens + line_tokens > input_token_budget:
                    break
                used_tokens += line_tokens
                end += 1
            batch_end = max(start + 1, end)
            plans.append(
                TranslationBatchPlan(
                    batch_id=batch_id,
                    start=start,
                    end=batch_end,
                    estimated_input_tokens=used_tokens,
                )
            )
            batch_id += 1
            start = batch_end
        return plans

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
                completed_count = 0

                def stream_callback(chunk):
                    """流式回调：增量解析完整行，避免全量拼接"""
                    nonlocal completed_count
                    stream_buffer.append(chunk)

                    # 队列翻译只关心整批结果。跳过分片解析和 UI 回调可显著
                    # 降低大批次响应期间的 Python/Tk 调度开销。
                    if not emit_stream_progress:
                        return

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
                        else:
                            new_lines = [_clean_stream_line(line) for line in parts]
                        preview_lines.extend(new_lines)

                    stream_start_line = completed_count
                    completed_count += len(new_lines)

                    partial_line = "".join(pending_parts)
                    if not is_hunyuan:
                        partial_line = _clean_stream_line(partial_line)
                    current_preview = list(preview_lines)
                    if partial_line:
                        current_preview.append(partial_line)

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
                        cache_glossary_version = run_context.glossary_version
                    else:
                        api_cfg = self.config_manager.get_api_config()
                        cache_provider = api_cfg.get("provider", "")
                        cache_model_name = api_cfg.get("model_name", "")
                        cache_base_prompt = app_config.get("translation_prompt", "")
                        cache_glossary_prompt = self.config_manager.get_glossary_prompt()
                        cache_temperature = float(api_cfg.get("temperature", 0.3))
                        cache_prompt_version = hashlib.sha256(
                            cache_base_prompt.encode("utf-8")
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
                if not response or not response.strip():
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
                    translated_content = response.strip()

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
                            line_num = int(match.group(1))
                            content = match.group(2)
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

                if batch_failed:
                    # 存在缺失译文：返回 PARTIAL
                    return BatchTranslationResult(
                        status=TranslationStatus.PARTIAL,
                        lines=translated_lines,
                        failed_indices=batch_failed,
                        error_message=f"批次内 {len(batch_failed)} 行译文缺失",
                    )

                # 全部必需行都有译文：返回 SUCCEEDED
                return BatchTranslationResult(
                    status=TranslationStatus.SUCCEEDED,
                    lines=translated_lines,
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
