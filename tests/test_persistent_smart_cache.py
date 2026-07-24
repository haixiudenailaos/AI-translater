"""STORAGE-7：持久化翻译缓存与数据目录注入测试。"""

from pathlib import Path

from src.core.smart_cache import SmartCache
from src.core.translator import TranslatorEngine


def test_persistent_cache_reloads_a_previous_translation(tmp_path):
    cache_dir = tmp_path / "cache"
    context = {"provider": "example", "model": "test-model"}

    cache = SmartCache(
        max_entries=10,
        ttl_hours=24,
        cache_dir=cache_dir,
        namespace="example:test-model",
    )
    cache.set("原文", "译文", context)

    cache_file = cache.persistence_path
    assert cache_file is not None and cache_file.is_file()
    # 磁盘缓存不保存原文，避免目录中的缓存文件直接泄露输入文本。
    assert "原文" not in cache_file.read_text(encoding="utf-8")

    restored = SmartCache(
        max_entries=10,
        ttl_hours=24,
        cache_dir=cache_dir,
        namespace="example:test-model",
    )
    assert restored.get("原文", context) == "译文"
    assert restored.get_stats()["persistent"] is True


def test_clear_all_persists_an_empty_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = SmartCache(cache_dir=cache_dir, namespace="clear-test")
    cache.set("source", "target")
    cache.clear_all()

    restored = SmartCache(cache_dir=cache_dir, namespace="clear-test")
    assert restored.get("source") is None


def test_translator_engine_passes_resolved_cache_dir_to_api(tmp_path, monkeypatch):
    captured: dict = {}

    class _CapturingApi:
        def __init__(self, config):
            captured.update(config)

        def close(self):
            pass

    class _ConfigManager:
        def get_api_config(self):
            return {
                "provider": "openai_compatible",
                "model_name": "test-model",
                "enable_cache": True,
                "cache_config": {"max_entries": 10, "ttl_hours": 24},
            }

    monkeypatch.setattr("src.core.translator.OpenAICompatibleAPI", _CapturingApi)
    cache_dir = tmp_path / "user-selected-cache"
    engine = TranslatorEngine(_ConfigManager(), cache_dir=cache_dir)

    engine._init_api()

    assert captured["cache_config"]["cache_dir"] == str(cache_dir.resolve())
    assert Path(captured["cache_config"]["cache_dir"]).is_absolute()
