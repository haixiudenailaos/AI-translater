#!/usr/bin/env python3
"""
配置管理模块
负责API密钥、术语库等配置的本地存储和管理
"""

import copy
import hashlib
import hmac
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

from ..domain.secret import ConfigSaveResult, SecretSaveResult, StorageStatus
from ..utils.file_handler import write_json_atomic
from ..utils.logger import get_logger

# P1-4：移除全局 get_key/store_key/delete_key 导入，密钥读写全部走注入的 SecretStore。
# 这样测试可通过注入 FakeSecretStore 验证密钥操作，不依赖 keyring 后端。
from .translation_profile import (
    DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY,
    DEFAULT_QUEUE_HARD_REQUEST_CAP,
    DEFAULT_QUEUE_MAX_ACTIVE_TASKS,
    DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS,
    DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT,
    DEFAULT_QUEUE_RPM_LIMIT,
    DEFAULT_QUEUE_TPM_LIMIT,
    DEFAULT_QUEUE_TRANSLATION_BATCH_LINES,
    DEFAULT_QUEUE_TRANSLATION_CONCURRENCY,
    DEFAULT_TRANSLATION_BATCH_LINES,
    DEFAULT_TRANSLATION_CONCURRENCY,
    OPENAI_COMPATIBLE_PROVIDER,
    SILICONFLOW_DEEPSEEK_V32_MODEL,
    apply_text_translation_profile,
    normalize_openai_base_url,
)
from .volcengine_image import (
    VOLCENGINE_IMAGE_DEFAULT_BASE_URL,
    VOLCENGINE_IMAGE_DEFAULT_MODEL,
)

logger = get_logger(__name__)

DEFAULT_PROMPT_SCHEMA_VERSION = 2
_LEGACY_DEFAULT_PROMPT_HASHES = {
    "de50a67353d835b344f651912300ae93015f4b2f14d2f600e4712d4c6e475f2d",
}
_ACTIVE_SECRET_REF_KEY = "active_secret_ref"
_PROVIDER_SECRET_REFS_KEY = "provider_secret_refs"


class ConfigManager:
    def __init__(self, app_paths=None, secret_store=None):
        # BUG-001：通过 AppPaths 接收统一配置目录，避免依赖当前工作目录
        if app_paths is not None:
            self.config_dir = Path(app_paths.config_dir)
        else:
            self.config_dir = Path("config")
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # P1-2：注入 SecretStore 协议，未注入时延迟创建默认实现（兼容层）
        self._secret_store = secret_store

        # 配置文件路径
        self.api_config_file = self.config_dir / "api_config.json"
        self.glossary_file = self.config_dir / "glossary.json"
        self.app_config_file = self.config_dir / "app_config.json"
        self.volc_key_file = self.config_dir / "volc_ark_key.json"

        # 默认配置（BUG-009：API Key 不再写入 JSON，改用系统密钥环存储）
        self.default_api_config = {
            "provider": "siliconflow",
            "model_name": SILICONFLOW_DEEPSEEK_V32_MODEL,
            "base_url": "https://api.siliconflow.cn/v1",
            "temperature": 0.3,
            "context_window_tokens": 32768,
            "api_max_attempts": 3,
            "http_connect_timeout": 10.0,
            "http_read_timeout": 180.0,
            "http_write_timeout": 60.0,
            "http_pool_timeout": 10.0,
            "enable_stream": True,
            "enable_cache": True,
            "cache_config": {"max_entries": 1000, "ttl_hours": 24},
        }

        self.default_app_config = {
            "target_language": "中文",
            "auto_save": True,
            "translation_prompt": self._get_default_prompt(),
            "prompt_schema_version": DEFAULT_PROMPT_SCHEMA_VERSION,
            "vision_model_name": "Pro/Qwen/Qwen2.5-VL-7B-Instruct",
            "image_text_translation_enabled": True,
            "image_gen_provider": "volcengine",
            "image_translation": {
                "default_provider": "manga",
                "manga": {
                    "quality_preset": "standard",
                    "device": "auto",
                    "model_dir": "",
                    "python_executable": "",
                    "batch_size": 1,
                },
                "ai_volcengine": {
                    "provider": "volcengine",
                    "base_url": VOLCENGINE_IMAGE_DEFAULT_BASE_URL,
                    "model": VOLCENGINE_IMAGE_DEFAULT_MODEL,
                },
            },
            "batch_max_input_characters": 8000,
            "batch_lines": DEFAULT_TRANSLATION_BATCH_LINES,
            "translation_concurrency": DEFAULT_TRANSLATION_CONCURRENCY,
            "queue_batch_lines": DEFAULT_QUEUE_TRANSLATION_BATCH_LINES,
            "queue_translation_concurrency": DEFAULT_QUEUE_TRANSLATION_CONCURRENCY,
            # 队列翻译并发优化阶段 1（QUEUE_TRANSLATION_CONCURRENCY_OPTIMIZATION_PLAN.md §3）：
            # 全局公平调度器策略参数。详细说明见 translation_profile.py。
            "queue_max_in_flight_requests": DEFAULT_QUEUE_MAX_IN_FLIGHT_REQUESTS,
            "queue_hard_request_cap": DEFAULT_QUEUE_HARD_REQUEST_CAP,
            "queue_max_active_tasks": DEFAULT_QUEUE_MAX_ACTIVE_TASKS,
            "queue_per_task_soft_limit": DEFAULT_QUEUE_PER_TASK_SOFT_LIMIT,
            "queue_adaptive_concurrency": DEFAULT_QUEUE_ADAPTIVE_CONCURRENCY,
            "queue_rpm_limit": DEFAULT_QUEUE_RPM_LIMIT,
            "queue_tpm_limit": DEFAULT_QUEUE_TPM_LIMIT,
            "ui_font_size": 10,
            "ui_font_family": "TkDefaultFont",
            "recent_files": [],
            "window_state": {},
            "onboarding": {
                "schema_version": 1,
                "status": "not_started",
                "current_step": "welcome",
                "completed_steps": [],
                "auto_show": True,
            },
        }

        self.default_glossary = {"terms": [], "categories": ["通用", "技术", "专业"]}

        # Load non-sensitive API metadata synchronously. Accessing the Windows
        # credential backend is deferred until a caller actually needs the key.
        # API 运行时配置、已加载密钥和 provider marker 必须作为同一快照
        # 读写。保存路径会重入读取/规范化逻辑，因此使用 RLock。
        self._api_key_lock = threading.RLock()
        self._app_config_lock = threading.RLock()
        self._glossary_lock = threading.RLock()
        self._api_key_provider = None
        self._api_key_reference = None
        self.api_config = self.load_api_config(load_secret=False)
        self.app_config = self.load_app_config()
        self.glossary = self.load_glossary()

    def _get_default_prompt(self):
        """获取默认翻译提示词"""
        return """你是文学翻译助手，请遵守以下规则：
1. 翻译为用户指定的目标语言，忠实保留原文含义、语气和人名术语。
2. 保留每行的行号标记、换行、标点和特殊格式，不增删或合并行。
3. 优先使用术语表指定译法；无术语时使用自然、通顺的文学表达。
4. 只输出带行号标记的译文，不要添加解释、标题或免责声明。"""

    def _get_secret_store(self):
        """取得 SecretStore 实例（P1-2）。

        优先使用注入的实例；未注入时延迟创建 KeyringSecretStore（兼容层）。
        bootstrap 完成后所有调用方应注入，移除此处对 infrastructure 的依赖。
        """
        if self._secret_store is not None:
            return self._secret_store
        from ..infrastructure.keyring_secret_store import KeyringSecretStore

        self._secret_store = KeyringSecretStore()
        return self._secret_store

    @staticmethod
    def _legacy_secret_reference(provider: str) -> str:
        """Return the pre-versioning key name used by existing installations."""
        return f"provider:{provider}"

    @classmethod
    def _secret_reference_for_config(cls, config: Dict[str, Any]) -> str:
        """Return the active opaque secret reference or the legacy fallback."""
        reference = config.get(_ACTIVE_SECRET_REF_KEY)
        if isinstance(reference, str) and reference:
            return reference

        provider = str(config.get("provider", "siliconflow"))
        references = config.get(_PROVIDER_SECRET_REFS_KEY)
        if isinstance(references, dict):
            provider_reference = references.get(provider)
            if isinstance(provider_reference, str) and provider_reference:
                return provider_reference
        return cls._legacy_secret_reference(provider)

    @classmethod
    def _secret_reference_for_provider(cls, config: Dict[str, Any], provider: str) -> str:
        """Return a saved provider reference without exposing the secret itself."""
        if config.get("provider") == provider:
            return cls._secret_reference_for_config(config)

        references = config.get(_PROVIDER_SECRET_REFS_KEY)
        if isinstance(references, dict):
            reference = references.get(provider)
            if isinstance(reference, str) and reference:
                return reference
        return cls._legacy_secret_reference(provider)

    @staticmethod
    def _new_secret_reference(provider: str) -> str:
        """Create a write-once secret key for a metadata generation."""
        return f"provider:{provider}:v{uuid.uuid4().hex}"

    def load_api_config(self, *, load_secret: bool = True) -> Dict[str, Any]:
        """加载API配置（BUG-009：API Key 从密钥环读取，不持久化到 JSON）"""
        try:
            if self.api_config_file.exists():
                with open(self.api_config_file, encoding="utf-8") as f:
                    config = json.load(f)

                # BUG-009：迁移旧明文密钥到密钥环
                config = self._migrate_plaintext_keys(config)
                # 旧版本允许用户设置 max_tokens；当前版本交由模型/provider 决定。
                config.pop("max_tokens", None)

                # 合并默认配置
                merged_config = self.default_api_config.copy()
                merged_config.update(config)

                # 从密钥环读取当前提供商的密钥，注入到运行时配置（不写回 JSON）
                secret_reference = self._secret_reference_for_config(merged_config)
                # P1-4：通过注入的 SecretStore 读取，不调用全局 get_key
                merged_config["api_key"] = (
                    self._get_secret_store().retrieve(secret_reference) if load_secret else ""
                )

                merged_config = apply_text_translation_profile(merged_config)
                if merged_config.get("provider") == OPENAI_COMPATIBLE_PROVIDER:
                    try:
                        merged_config["base_url"] = normalize_openai_base_url(
                            merged_config.get("base_url", "")
                        )
                    except ValueError as exc:
                        logger.error("API 配置中的自定义 endpoint 无效: %s", exc)
                        merged_config["base_url"] = ""
                return merged_config
        except (OSError, json.JSONDecodeError) as e:
            logger.error("加载API配置失败: %s", e)

        # 即使配置文件不存在，也尝试从密钥环读取默认提供商的密钥
        result = self.default_api_config.copy()
        secret_reference = self._secret_reference_for_config(result)
        # P1-4：通过注入的 SecretStore 读取
        result["api_key"] = (
            self._get_secret_store().retrieve(secret_reference) if load_secret else ""
        )
        return apply_text_translation_profile(result)

    def _ensure_api_key_loaded(self) -> None:
        """Lazily load a key without holding the state lock during keyring I/O."""
        while True:
            with self._api_key_lock:
                config = self.api_config
                provider = str(config.get("provider", "siliconflow"))
                reference = self._secret_reference_for_config(config)
                if self._api_key_reference == reference:
                    return
                # Compatibility for tests and callers that injected a complete
                # legacy in-memory snapshot before versioned refs existed.
                if self._api_key_provider == provider and config.get("api_key"):
                    self._api_key_reference = reference
                    return

            key = self._get_secret_store().retrieve(reference)

            with self._api_key_lock:
                if self._secret_reference_for_config(self.api_config) != reference:
                    # A writer published a newer generation while keyring I/O
                    # was in flight; retry against the new immutable snapshot.
                    continue
                updated = copy.deepcopy(self.api_config)
                updated["api_key"] = key
                self.api_config = updated
                self._api_key_provider = provider
                self._api_key_reference = reference
                return

    def _migrate_plaintext_keys(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """BUG-009 / R2-BUG-002：将旧版本 JSON 中的明文密钥迁移到密钥环，并从配置中删除密钥字段。

        R2-BUG-002 修复要点：
        - 只有持久化写入成功（PERSISTED）后才删除 JSON 中的明文密钥
        - 会话级降级（SESSION_ONLY）保留旧明文 Key，下次启动再次尝试迁移
        - 不在日志中写入 Key 明文
        - 返回清理后的 config（不含 api_key / provider_keys）

        返回：清理后的 config（不含密钥字段）。
        注意：即使持久化失败，返回的 config 也不含明文密钥；
        调用方应通过 SecretStore.retrieve() 重新读取（失败时会返回空字符串）。
        磁盘上的 JSON 在持久化失败时保持原样，下次启动会再次尝试迁移。
        """
        # P1-4：统一通过注入的 SecretStore 操作密钥
        secret_store = self._get_secret_store()
        # 收集待迁移的密钥（不立即 pop，先尝试迁移）
        provider_keys = config.get("provider_keys")
        api_key = config.get("api_key")

        pending_migrations: list[tuple[str, str]] = []  # [(identifier, key), ...]
        if provider_keys and isinstance(provider_keys, dict):
            for prov, key in provider_keys.items():
                key = (key or "").strip()
                if key:
                    pending_migrations.append((f"provider:{prov}", key))

        if api_key and api_key.strip():
            provider = config.get("provider", "siliconflow")
            # 仅在密钥环中尚无该提供商密钥时迁移（避免覆盖已从 provider_keys 迁移的值）
            if not secret_store.retrieve(f"provider:{provider}"):
                pending_migrations.append((f"provider:{provider}", api_key.strip()))

        if not pending_migrations:
            # 没有待迁移的密钥，但仍要清理 config 中的密钥字段
            config.pop("provider_keys", None)
            config.pop("api_key", None)
            return config

        # 逐个迁移，统计持久化成功数量
        all_persisted = True
        for identifier, key in pending_migrations:
            status = secret_store.store(identifier, key)
            if status == StorageStatus.PERSISTED:
                logger.info(
                    "已迁移 %s 的密钥到密钥环（已持久化）",
                    identifier,
                )
            elif status == StorageStatus.SESSION_ONLY:
                # 仅会话级：保留旧明文，下次启动再次尝试
                all_persisted = False
                logger.warning(
                    "密钥 %s 仅写入环境变量（重启后失效），保留旧明文以便下次启动再次迁移",
                    identifier,
                )
            else:
                all_persisted = False
                logger.error(
                    "密钥 %s 迁移失败，保留旧明文以便下次启动再次迁移",
                    identifier,
                )

        # R2-BUG-002：只有所有密钥都持久化成功后才回写清理后的配置
        if all_persisted:
            # 从 config 中移除密钥字段
            config.pop("provider_keys", None)
            config.pop("api_key", None)
            try:
                write_json_atomic(self.api_config_file, config)
                logger.info("旧明文密钥已从配置文件中删除")
            except OSError as e:
                logger.warning(
                    "回写清理后的配置失败（密钥已迁移，下次启动会再次清理）: %s",
                    e,
                )
        else:
            # 持久化失败：磁盘保持原样，但内存中仍移除密钥字段
            # 调用方会通过 SecretStore.retrieve() 读取（SESSION_ONLY 时能从环境变量读到）
            config.pop("provider_keys", None)
            config.pop("api_key", None)
            logger.warning("部分密钥未持久化成功，磁盘保留明文，下次启动再次尝试迁移")

        return config

    def save_api_config(self, config: Dict[str, Any]) -> SecretSaveResult:
        """事务式保存并原子发布 API 运行时快照。"""
        with self._api_key_lock:
            return self._save_api_config_locked(config)

    def _save_api_config_locked(self, config: Dict[str, Any]) -> SecretSaveResult:
        """Persist a versioned key, metadata reference, then runtime snapshot.

        A key is never overwritten in place. If metadata persistence fails, the
        new key has no reference and is discarded best-effort; the old metadata
        and its key therefore remain a valid pair after a restart.
        """
        config = copy.deepcopy(config)
        config.pop("max_tokens", None)
        provider = str(config.get("provider", "siliconflow"))
        api_key = str(config.get("api_key", "") or "").strip()
        old_provider = str(self.api_config.get("provider", "siliconflow"))
        old_secret_reference = self._secret_reference_for_config(self.api_config)
        if provider == OPENAI_COMPATIBLE_PROVIDER:
            try:
                config["base_url"] = normalize_openai_base_url(
                    config.get("base_url") or self.api_config.get("base_url", "")
                )
            except ValueError as exc:
                return SecretSaveResult(
                    secret_status=StorageStatus.FAILED,
                    config_saved=False,
                    error_message=str(exc),
                    provider=provider,
                )

        # Build a candidate before mutating any durable state. The reference is
        # opaque metadata, never the secret value itself.
        config = apply_text_translation_profile(config)
        merged_config = copy.deepcopy(self.default_api_config)
        merged_config.update(copy.deepcopy(self.api_config))
        merged_config.update(config)
        merged_config.pop("max_tokens", None)

        if provider == OPENAI_COMPATIBLE_PROVIDER:
            provider_configs = copy.deepcopy(merged_config.get("provider_configs") or {})
            provider_configs[provider] = {
                "base_url": merged_config.get("base_url", ""),
                "model_name": merged_config.get("model_name", ""),
            }
            merged_config["provider_configs"] = provider_configs

        secret_reference = self._new_secret_reference(provider)
        provider_references = copy.deepcopy(merged_config.get(_PROVIDER_SECRET_REFS_KEY) or {})
        provider_references[provider] = secret_reference
        merged_config[_PROVIDER_SECRET_REFS_KEY] = provider_references
        merged_config[_ACTIVE_SECRET_REF_KEY] = secret_reference

        secret_store = self._get_secret_store()
        try:
            secret_status = secret_store.store(secret_reference, api_key)
        except Exception as e:
            logger.error("存储密钥失败 [%s]: %s", provider, e)
            return SecretSaveResult(
                secret_status=StorageStatus.FAILED,
                config_saved=False,
                error_message=f"密钥存储异常: {e}",
                provider=provider,
            )

        if secret_status == StorageStatus.FAILED:
            logger.error("密钥存储失败 [%s]，配置保存中止", provider)
            return SecretSaveResult(
                secret_status=StorageStatus.FAILED,
                config_saved=False,
                error_message="密钥存储失败（环境变量写入失败）",
                provider=provider,
            )

        safe_config = {
            key: value
            for key, value in merged_config.items()
            if key not in ("api_key", "provider_keys")
        }
        try:
            write_json_atomic(self.api_config_file, safe_config)
        except OSError as e:
            logger.error("保存API配置失败: %s", e)
            try:
                secret_store.delete(secret_reference)
            except Exception as cleanup_error:  # noqa: BLE001
                logger.warning("清理未发布密钥版本失败 [%s]: %s", provider, cleanup_error)
            return SecretSaveResult(
                secret_status=secret_status,
                config_saved=False,
                error_message=f"配置文件写入失败: {e}",
                provider=provider,
            )

        published_config = copy.deepcopy(merged_config)
        published_config["api_key"] = api_key
        self.api_config = published_config
        self._api_key_provider = provider
        self._api_key_reference = secret_reference

        # Replacing or clearing a key for the active provider leaves the old
        # version unreachable. Delete it only after the new metadata has been
        # atomically published; a cleanup failure cannot invalidate the pair.
        if old_provider == provider and old_secret_reference != secret_reference:
            try:
                secret_store.delete(old_secret_reference)
            except Exception as cleanup_error:  # noqa: BLE001
                logger.warning("清理旧密钥版本失败 [%s]: %s", provider, cleanup_error)

        if secret_status == StorageStatus.SESSION_ONLY:
            logger.warning("密钥 [%s] 仅会话级保存，重启后需重新输入", provider)
        return SecretSaveResult(
            secret_status=secret_status,
            config_saved=True,
            provider=provider,
        )

    def load_app_config(self) -> Dict[str, Any]:
        """加载应用配置"""
        try:
            if self.app_config_file.exists():
                with open(self.app_config_file, encoding="utf-8") as f:
                    config = json.load(f)
                    merged_config = self.default_app_config.copy()
                    merged_config.update(config)
                    # token 预算改为内部自动策略，不再保留用户侧上限字段。
                    merged_config.pop("batch_max_input_tokens", None)
                    merged_config.pop("queue_batch_max_input_tokens", None)
                    # 图片翻译配置迁移：缺 image_translation 时补齐，
                    # default_provider 固定为 manga
                    merged_config["image_translation"] = self._migrate_image_translation_config(
                        merged_config.get("image_translation")
                    )
                    # 新手指导配置归一化：顶层浅合并不会处理子字段，
                    # 这里对 onboarding 段再做一次默认值合并。
                    merged_config["onboarding"] = self._normalize_onboarding_config(
                        merged_config.get("onboarding")
                    )
                    self._migrate_translation_prompt(config, merged_config)
                    return merged_config
        except (OSError, json.JSONDecodeError) as e:
            logger.error("加载应用配置失败: %s", e)

        result = self.default_app_config.copy()
        result["image_translation"] = self._migrate_image_translation_config(None)
        result["onboarding"] = self._normalize_onboarding_config(None)
        result["prompt_schema_version"] = DEFAULT_PROMPT_SCHEMA_VERSION
        return result

    def _migrate_translation_prompt(
        self,
        raw_config: Dict[str, Any],
        merged_config: Dict[str, Any],
    ) -> None:
        """Replace only the exact historical default prompt with the current one."""
        prompt = raw_config.get("translation_prompt")
        schema_version = raw_config.get("prompt_schema_version")
        if isinstance(prompt, str) and self._is_known_legacy_prompt(prompt):
            merged_config["translation_prompt"] = self._get_default_prompt()
            merged_config["prompt_schema_version"] = DEFAULT_PROMPT_SCHEMA_VERSION
            return
        if not isinstance(schema_version, int):
            merged_config["prompt_schema_version"] = DEFAULT_PROMPT_SCHEMA_VERSION

    @staticmethod
    def _is_known_legacy_prompt(prompt: str) -> bool:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        return digest in _LEGACY_DEFAULT_PROMPT_HASHES

    def _normalize_onboarding_config(self, existing: Dict[str, Any] | None) -> Dict[str, Any]:
        """归一化新手指导配置段。

        顶层 ``load_app_config`` 只对顶层做浅合并，因此引导代码必须对
        ``onboarding`` 子字段再做一次默认值合并，避免缺失字段或类型异常
        导致主窗口启动失败。
        """
        default = self.default_app_config["onboarding"]
        if not existing or not isinstance(existing, dict):
            return default.copy()

        merged = default.copy()
        merged.update(existing)
        # 类型校正：保证关键字段类型合法，配置损坏时回退到默认值
        if not isinstance(merged.get("schema_version"), int):
            merged["schema_version"] = default["schema_version"]
        if merged.get("status") not in (
            "not_started",
            "in_progress",
            "completed",
            "dismissed",
        ):
            merged["status"] = default["status"]
        if not isinstance(merged.get("current_step"), str) or not merged["current_step"]:
            merged["current_step"] = default["current_step"]
        if not isinstance(merged.get("completed_steps"), list):
            merged["completed_steps"] = []
        else:
            # 去重，保留顺序
            seen = set()
            deduped = []
            for step_id in merged["completed_steps"]:
                if isinstance(step_id, str) and step_id not in seen:
                    seen.add(step_id)
                    deduped.append(step_id)
            merged["completed_steps"] = deduped
        if not isinstance(merged.get("auto_show"), bool):
            merged["auto_show"] = default["auto_show"]
        return merged

    def _migrate_image_translation_config(self, existing: Dict[str, Any] | None) -> Dict[str, Any]:
        """迁移图片翻译配置。

        规则：
        1. 配置缺少 image_translation 时自动补齐，default_provider 固定为 manga。
        2. 保留旧 image_gen_provider 和密钥环中的 volc:ark_api_key。
        3. 旧 image_text_translation_enabled 不再控制默认模块。
        4. 用户单次选择 AI 不写回 default_provider（由调用方保证）。
        """
        default = self.default_app_config["image_translation"]
        if not existing or not isinstance(existing, dict):
            return default.copy()

        merged = default.copy()
        # 深合并子段
        for section in ("manga", "ai_volcengine"):
            if section in existing and isinstance(existing[section], dict):
                merged_section = default.get(section, {}).copy()
                merged_section.update(existing[section])
                merged[section] = merged_section
        # default_provider 强制为 manga（不信任旧值，避免被篡改为 AI）
        merged["default_provider"] = "manga"
        # 保留用户在 manga 段的自定义字段
        if "manga" in existing:
            manga_existing = existing["manga"]
            if isinstance(manga_existing, dict):
                manga_merged = merged.get("manga", {})
                manga_merged.update(manga_existing)
                merged["manga"] = manga_merged
        if "ai_volcengine" in existing:
            ai_existing = existing["ai_volcengine"]
            if isinstance(ai_existing, dict):
                ai_merged = merged.get("ai_volcengine", {})
                ai_merged.update(ai_existing)
                merged["ai_volcengine"] = ai_merged
        return merged

    def get_image_translation_config(self) -> Dict[str, Any]:
        """获取图片翻译配置段。"""
        with self._app_config_lock:
            return copy.deepcopy(self.app_config.get("image_translation", {}))

    def get_default_image_translation_provider(self) -> str:
        """获取默认图片翻译 Provider id（始终为 manga）。"""
        return "manga"

    def save_app_config(self, config: Dict[str, Any]) -> bool:
        """保存应用配置"""
        candidate = copy.deepcopy(config)
        candidate.pop("batch_max_input_tokens", None)
        candidate.pop("queue_batch_max_input_tokens", None)
        try:
            with self._app_config_lock:
                # BUG-006：使用原子写入，失败时旧文件保持不变
                write_json_atomic(self.app_config_file, candidate)
                self.app_config = candidate
            return True
        except OSError as e:
            logger.error("保存应用配置失败: %s", e)
            return False

    def load_glossary(self) -> Dict[str, Any]:
        """加载术语库"""
        try:
            if self.glossary_file.exists():
                with open(self.glossary_file, encoding="utf-8") as f:
                    glossary = json.load(f)
                    merged_glossary = self.default_glossary.copy()
                    merged_glossary.update(glossary)
                    return merged_glossary
        except (OSError, json.JSONDecodeError) as e:
            logger.error("加载术语库失败: %s", e)

        return self.default_glossary.copy()

    def save_glossary(self, glossary: Dict[str, Any]) -> bool:
        """保存术语库"""
        candidate = copy.deepcopy(glossary)
        try:
            with self._glossary_lock:
                # BUG-006：使用原子写入，失败时旧文件保持不变
                write_json_atomic(self.glossary_file, candidate)
                self.glossary = candidate
            return True
        except OSError as e:
            logger.error("保存术语库失败: %s", e)
            return False

    def save_config(self) -> ConfigSaveResult:
        """保存所有配置（ENG-1：聚合各部分结果）

        分别调用 API 配置、应用配置和术语表的保存方法，聚合各自的
        状态和错误，返回 ``ConfigSaveResult``。关闭流程据此决定是否
        阻断退出、提示重试或继续不保存退出。

        为捕获写入失败的具体 OSError 消息（供 UI 显示可操作摘要），
        本方法直接调用 ``write_json_atomic`` 而非依赖 ``save_app_config``
        / ``save_glossary`` 的布尔返回值。各阶段独立 try/except，避免
        一个宽捕获覆盖全部阶段。
        """
        api_result = self.save_api_config(self.get_api_config(load_secret=True))

        # 应用配置：直接写入以捕获具体错误消息
        app_saved = True
        app_error = ""
        try:
            with self._app_config_lock:
                write_json_atomic(self.app_config_file, copy.deepcopy(self.app_config))
        except OSError as e:
            app_saved = False
            app_error = str(e)
            logger.error("保存应用配置失败: %s", e)

        # 术语表：直接写入以捕获具体错误消息
        glossary_saved = True
        glossary_error = ""
        try:
            with self._glossary_lock:
                write_json_atomic(self.glossary_file, copy.deepcopy(self.glossary))
        except OSError as e:
            glossary_saved = False
            glossary_error = str(e)
            logger.error("保存术语库失败: %s", e)

        return ConfigSaveResult(
            api=api_result,
            app_config_saved=app_saved,
            glossary_saved=glossary_saved,
            app_config_error=app_error,
            glossary_error=glossary_error,
        )

    def is_api_configured(self) -> bool:
        """检查API是否已配置"""
        return bool(self.get_api_config(load_secret=True).get("api_key", "").strip())

    def get_api_config(self, *, load_secret: bool = True) -> Dict[str, Any]:
        """获取API配置"""
        if load_secret:
            self._ensure_api_key_loaded()
        with self._api_key_lock:
            snapshot = copy.deepcopy(self.api_config)
            # 无密钥读取用于预检、日志和 UI 元数据；即使之前已在内存加载过
            # 密钥，也绝不能把它带入该公共快照。
            if not load_secret:
                snapshot["api_key"] = ""
            return snapshot

    def get_app_config(self) -> Dict[str, Any]:
        """获取应用配置"""
        with self._app_config_lock:
            return copy.deepcopy(self.app_config)

    def get_window_state(self, window_name: str) -> Dict[str, Any]:
        """读取已保存的窗口尺寸；无效或损坏的状态按未保存处理。"""
        with self._app_config_lock:
            all_states = self.app_config.get("window_state", {})
            state = all_states.get(window_name, {}) if isinstance(all_states, dict) else {}
            if not isinstance(state, dict):
                return {}

            width = state.get("width")
            height = state.get("height")
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or width <= 0
                or isinstance(height, bool)
                or not isinstance(height, int)
                or height <= 0
            ):
                return {}
            return {
                "width": width,
                "height": height,
                "maximized": state.get("maximized") is True,
            }

    def update_window_state(
        self,
        window_name: str,
        state: Dict[str, Any],
        *,
        persist: bool = False,
    ) -> bool:
        """更新窗口尺寸；设置窗口可选择立即持久化。"""
        if not isinstance(window_name, str) or not window_name:
            return False
        width = state.get("width") if isinstance(state, dict) else None
        height = state.get("height") if isinstance(state, dict) else None
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
        ):
            return False

        with self._app_config_lock:
            candidate = copy.deepcopy(self.app_config)
            all_states = candidate.get("window_state", {})
            all_states = {} if not isinstance(all_states, dict) else copy.deepcopy(all_states)
            all_states[window_name] = {
                "width": width,
                "height": height,
                "maximized": state.get("maximized") is True,
            }
            candidate["window_state"] = all_states
            self.app_config = candidate

            if not persist:
                return True
            try:
                write_json_atomic(self.app_config_file, copy.deepcopy(candidate))
                return True
            except OSError as exc:
                logger.error("保存窗口尺寸失败 [%s]: %s", window_name, exc)
                return False

    def get_glossary(self) -> Dict[str, Any]:
        """获取术语库"""
        with self._glossary_lock:
            return copy.deepcopy(self.glossary)

    def add_glossary_term(self, source_term: str, target_term: str, category: str = "通用") -> bool:
        """添加术语"""
        try:
            term = {
                "source": source_term.strip(),
                "target": target_term.strip(),
                "category": category,
            }

            with self._glossary_lock:
                glossary = copy.deepcopy(self.glossary)
                # 检查是否已存在
                for existing_term in glossary["terms"]:
                    if existing_term["source"] == term["source"]:
                        existing_term.update(term)
                        return self.save_glossary(glossary)

                # 添加新术语
                glossary["terms"].append(term)
                return self.save_glossary(glossary)

        except OSError as e:
            logger.error("添加术语失败: %s", e)
            return False

    def remove_glossary_term(self, source_term: str) -> bool:
        """删除术语"""
        try:
            with self._glossary_lock:
                glossary = copy.deepcopy(self.glossary)
                glossary["terms"] = [
                    term for term in glossary["terms"] if term["source"] != source_term
                ]
                return self.save_glossary(glossary)
        except OSError as e:
            logger.error("删除术语失败: %s", e)
            return False

    def get_glossary_prompt(self) -> str:
        """获取术语库提示词"""
        with self._glossary_lock:
            terms = copy.deepcopy(self.glossary["terms"])
        if not terms:
            return ""

        prompt = "\n\n【术语库】请在翻译时严格按照以下术语对照表进行翻译：\n"
        for term in terms:
            prompt += f"- {term['source']} → {term['target']}\n"

        return prompt

    def update_api_provider_config(self, provider: str, config: Dict[str, Any]):
        """Update a provider through the same snapshot transaction as the UI."""
        candidate = self.get_api_config(load_secret=True)
        candidate["provider"] = provider
        candidate.update(copy.deepcopy(config))
        if "api_key" not in config:
            candidate["api_key"] = self.get_provider_key(provider)
        return self.save_api_config(candidate)

    def get_provider_key(self, provider: str) -> str:
        """BUG-009：从密钥环读取指定提供商的 API Key

        P1-4：通过注入的 SecretStore 读取，不调用全局 get_key。
        """
        with self._api_key_lock:
            snapshot = copy.deepcopy(self.api_config)
            reference = self._secret_reference_for_provider(snapshot, provider)
            if snapshot.get("provider") == provider and snapshot.get("api_key"):
                return str(snapshot["api_key"])
        return self._get_secret_store().retrieve(reference)

    def get_provider_config(self, provider: str) -> Dict[str, Any]:
        """Return a provider's saved endpoint fields plus its runtime API key."""
        with self._api_key_lock:
            api_config = copy.deepcopy(self.api_config)
        provider_configs = api_config.get("provider_configs") or {}
        saved_config = provider_configs.get(provider, {})
        result = dict(saved_config) if isinstance(saved_config, dict) else {}
        if api_config.get("provider") == provider:
            for field in ("base_url", "model_name"):
                result.setdefault(field, api_config.get(field, ""))
        result["api_key"] = self.get_provider_key(provider)
        return result

    def save_api_and_model_preset(
        self, preset_name: str, api_key: str, model_name: str
    ) -> SecretSaveResult:
        """保存API和模型预设（ENG-2：返回 SecretSaveResult 区分三态）

        BUG-009：密钥存入密钥环，JSON 只保存模型名。
        ENG-2：与 ``save_api_config`` 复用 ``SecretSaveResult`` 结果类型，
        - FAILED：密钥存储失败，不写入 JSON
        - SESSION_ONLY：允许会话使用，但 UI 必须提示"重启后需重新输入"
        - PERSISTED：正常成功

        向后兼容：``SecretSaveResult.__bool__`` 使旧调用方
        ``if save_api_and_model_preset(...)`` 继续工作
        （PERSISTED / SESSION_ONLY → True，FAILED → False）。
        """
        provider_tag = f"preset:{preset_name}"
        presets_file = self.config_dir / "api_presets.json"

        # 加载现有预设
        presets: Dict[str, Any] = {}
        if presets_file.exists():
            try:
                with open(presets_file, encoding="utf-8") as f:
                    presets = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                # 旧预设损坏不阻断新预设保存，但需记录
                logger.warning("读取现有预设失败，将覆盖: %s", e)
                presets = {}

        # ENG-2：通过注入的 SecretStore 存储密钥
        secret_store = self._get_secret_store()
        try:
            secret_status = secret_store.store(provider_tag, api_key)
        except Exception as e:
            logger.error("存储预设密钥失败 [%s]: %s", preset_name, e)
            return SecretSaveResult(
                secret_status=StorageStatus.FAILED,
                config_saved=False,
                error_message=f"密钥存储异常: {e}",
                provider=provider_tag,
            )

        # ENG-2：FAILED 时不写入 JSON，返回失败结果
        if secret_status == StorageStatus.FAILED:
            logger.error("预设密钥存储失败 [%s]，配置保存中止", preset_name)
            return SecretSaveResult(
                secret_status=StorageStatus.FAILED,
                config_saved=False,
                error_message="密钥存储失败（环境变量写入失败）",
                provider=provider_tag,
            )

        # JSON 中只保存非敏感信息
        presets[preset_name] = {
            "model_name": model_name,
        }

        # BUG-006：使用原子写入
        try:
            write_json_atomic(presets_file, presets)
        except OSError as e:
            logger.error("保存API预设JSON失败: %s", e)
            # 密钥已存储但 JSON 写入失败
            return SecretSaveResult(
                secret_status=secret_status,
                config_saved=False,
                error_message=f"配置文件写入失败: {e}",
                provider=provider_tag,
            )

        # ENG-2：SESSION_ONLY 时仍返回成功（允许会话），但标记状态供 UI 提示
        if secret_status == StorageStatus.SESSION_ONLY:
            logger.warning(
                "预设密钥 [%s] 仅会话级保存，重启后需重新输入",
                preset_name,
            )
        return SecretSaveResult(
            secret_status=secret_status,
            config_saved=True,
            provider=provider_tag,
        )

    def load_api_presets(self) -> Dict[str, Dict[str, str]]:
        """加载API预设（BUG-009：从密钥环注入密钥；R2-BUG-003：不写回明文）

        R2-BUG-003 修复要点：
        - 持久化对象（写回磁盘）和返回给 UI 的运行时对象必须分离
        - 写回磁盘前重新构造只包含非敏感字段的对象
        - 密钥迁移持久化成功后才能移除旧字段
        """
        try:
            presets_file = self.config_dir / "api_presets.json"
            if not presets_file.exists():
                return {}

            with open(presets_file, encoding="utf-8") as f:
                disk_presets = json.load(f)

            # 运行时预设（注入密钥，仅用于 UI 显示）
            runtime_presets: Dict[str, Dict[str, str]] = {}
            # 需要写回磁盘的干净预设（不含 api_key）
            clean_presets: Dict[str, Dict[str, str]] = {}
            needs_rewrite = False

            for name, data in disk_presets.items():
                data_copy = dict(data) if isinstance(data, dict) else {}
                legacy_key = data_copy.pop("api_key", None)

                if legacy_key and legacy_key.strip():
                    # R2-BUG-003：迁移旧明文密钥
                    # P1-4：通过注入的 SecretStore 存储
                    status = self._get_secret_store().store(f"preset:{name}", legacy_key.strip())
                    if status == StorageStatus.PERSISTED:
                        needs_rewrite = True
                        logger.info(
                            "已迁移预设 %s 的密钥到密钥环（已持久化）",
                            name,
                        )
                    else:
                        # 迁移失败：保留旧明文以便下次启动再次尝试
                        # 但不写入 runtime_presets 的 api_key（避免泄漏到 UI 日志）
                        # 也不写回 clean_presets（保持磁盘原样）
                        logger.warning(
                            "预设 %s 密钥迁移未持久化成功，磁盘保留明文",
                            name,
                        )
                        # 注意：此处不设置 needs_rewrite，磁盘保持原样
                        # 但 legacy_key 仍可用于运行时（注入到 runtime_presets）
                        pass

                # 构造运行时对象（包含 api_key 供 UI 临时使用）
                runtime_data = {k: v for k, v in data_copy.items()}
                if legacy_key and legacy_key.strip():
                    # 迁移失败时仍使用 legacy_key，迁移成功时从密钥环读取
                    # P1-4：通过注入的 SecretStore 读取
                    runtime_data["api_key"] = (
                        self._get_secret_store().retrieve(f"preset:{name}") or legacy_key.strip()
                    )
                else:
                    runtime_data["api_key"] = self._get_secret_store().retrieve(f"preset:{name}")
                runtime_presets[name] = runtime_data

                # 构造持久化对象（不含 api_key）
                clean_presets[name] = data_copy

            # R2-BUG-003：只有所有迁移都持久化成功后才写回干净预设
            if needs_rewrite:
                try:
                    write_json_atomic(presets_file, clean_presets)
                    logger.info("旧预设明文密钥已从 JSON 中删除")
                except OSError as e:
                    logger.warning(
                        "回写清理后的预设失败（密钥已迁移，下次启动会再次清理）: %s",
                        e,
                    )

            return runtime_presets
        except (OSError, json.JSONDecodeError) as e:
            logger.error("加载API预设失败: %s", e)

        return {}

    def delete_api_preset(self, preset_name: str) -> bool:
        """删除API预设（BUG-009：同时删除密钥环中的密钥）"""
        try:
            presets_file = self.config_dir / "api_presets.json"
            if not presets_file.exists():
                return False

            with open(presets_file, encoding="utf-8") as f:
                presets = json.load(f)

            if preset_name in presets:
                del presets[preset_name]

                # BUG-009：删除密钥环中的密钥
                # P1-4：通过注入的 SecretStore 删除
                if not self._get_secret_store().delete(f"preset:{preset_name}"):
                    logger.error("删除 API 预设密钥失败: %s", preset_name)
                    return False

                # BUG-006：使用原子写入，失败时旧文件保持不变
                write_json_atomic(presets_file, presets)

                return True

        except Exception as e:
            logger.error("删除API预设失败: %s", e)

        return False

    def save_volc_key(self, api_key: str) -> SecretSaveResult:
        """保存火山引擎API密钥（BUG-009：存入密钥环，不再写入 JSON 文件）

        P1-2：返回 ``SecretSaveResult`` 区分三态，UI 据此精确提示。
        - PERSISTED：已写入密钥环
        - SESSION_ONLY：仅写入环境变量（重启后失效）
        - FAILED：写入失败
        向后兼容：``SecretSaveResult.__bool__`` 使旧 ``if save_volc_key(...)`` 继续工作。

        P1-2：统一使用注入的 ``SecretStore``，移除全局 ``store_key`` 混用，
        使测试可通过注入 FakeSecretStore 验证三态。
        """
        try:
            api_key = (api_key or "").strip()
            # P1-2：通过注入的 SecretStore 存储密钥（与 save_api_config 一致）
            secret_store = self._get_secret_store()
            try:
                secret_status = secret_store.store("volc:ark_api_key", api_key)
            except Exception as e:
                logger.error("存储火山引擎密钥失败: %s", e)
                return SecretSaveResult(
                    secret_status=StorageStatus.FAILED,
                    config_saved=False,
                    error_message=f"火山引擎密钥存储异常: {e}",
                    provider="volc",
                )

            if secret_status != StorageStatus.FAILED:
                try:
                    read_back = (secret_store.retrieve("volc:ark_api_key") or "").strip()
                except Exception as e:
                    logger.error("回读火山引擎密钥失败: %s", e)
                    return SecretSaveResult(
                        secret_status=StorageStatus.FAILED,
                        config_saved=False,
                        error_message="火山引擎密钥保存后无法回读",
                        provider="volc",
                    )
                if not hmac.compare_digest(read_back, api_key):
                    logger.error("火山引擎密钥保存后回读不一致")
                    return SecretSaveResult(
                        secret_status=StorageStatus.FAILED,
                        config_saved=False,
                        error_message="火山引擎密钥保存后校验失败",
                        provider="volc",
                    )

            # 兼容旧版：如果存在旧明文文件，删除它
            if self.volc_key_file.exists():
                try:
                    self.volc_key_file.unlink()
                except OSError:  # 最佳努力：旧密钥文件删除失败不影响新密钥设置
                    pass

            # 设置环境变量（部分模块可能直接读取）
            if api_key:
                os.environ["ARK_API_KEY"] = api_key
            else:
                os.environ.pop("ARK_API_KEY", None)

            if secret_status == StorageStatus.FAILED:
                logger.error("火山引擎密钥存储失败")
                return SecretSaveResult(
                    secret_status=StorageStatus.FAILED,
                    config_saved=False,
                    error_message="火山引擎密钥存储失败（环境变量写入失败）",
                    provider="volc",
                )

            if secret_status == StorageStatus.SESSION_ONLY:
                logger.warning("火山引擎密钥仅会话级保存，重启后需重新输入")

            return SecretSaveResult(
                secret_status=secret_status,
                config_saved=True,
                provider="volc",
            )
        except OSError as e:
            logger.error("保存火山引擎Key失败: %s", e)
            return SecretSaveResult(
                secret_status=StorageStatus.FAILED,
                config_saved=False,
                error_message=f"火山引擎密钥存储异常: {e}",
                provider="volc",
            )

    def get_volc_key(self) -> str:
        """获取火山引擎API密钥（BUG-009：密钥环 > 旧明文文件 > 环境变量）"""
        # 1. 通过与保存路径相同的 SecretStore 读取，避免注入存储与全局
        #    keyring 分裂后读到旧值。
        try:
            key = self._get_secret_store().retrieve("volc:ark_api_key")
        except Exception as e:
            logger.warning("读取火山引擎密钥失败: %s", e)
            key = ""
        if key:
            return key.strip()

        # 2. 兼容旧版：从旧明文文件读取并迁移
        if self.volc_key_file.exists():
            try:
                with open(self.volc_key_file, encoding="utf-8") as f:
                    data = json.load(f)
                    legacy_key = data.get("ark_api_key", "").strip()
                    if legacy_key:
                        # 迁移到密钥环并删除旧文件
                        migration_status = self._get_secret_store().store(
                            "volc:ark_api_key", legacy_key
                        )
                        if migration_status != StorageStatus.PERSISTED:
                            logger.warning("火山引擎旧密钥未持久化，保留明文迁移文件以便下次重试")
                            return legacy_key
                        try:
                            self.volc_key_file.unlink()
                        except OSError:  # 最佳努力：读取后删除旧密钥文件
                            pass
                        return legacy_key
            except Exception:
                pass

        # 3. 降级读取环境变量
        return os.environ.get("ARK_API_KEY", "").strip()
