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
        path = path[:-len(completion_path)].rstrip("/")

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
