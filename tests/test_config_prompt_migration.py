from __future__ import annotations

import json

from src.config.config_manager import DEFAULT_PROMPT_SCHEMA_VERSION, ConfigManager


def test_known_legacy_prompt_is_replaced_but_custom_prompt_is_preserved(tmp_app_paths, monkeypatch):
    manager = ConfigManager(app_paths=tmp_app_paths)
    legacy_prompt = "historical bundled default"
    monkeypatch.setattr(
        manager,
        "_is_known_legacy_prompt",
        lambda prompt: prompt == legacy_prompt,
    )
    manager.app_config_file.write_text(
        json.dumps({"translation_prompt": legacy_prompt}, ensure_ascii=False),
        encoding="utf-8",
    )

    migrated = manager.load_app_config()

    assert migrated["translation_prompt"] == manager._get_default_prompt()
    assert migrated["prompt_schema_version"] == DEFAULT_PROMPT_SCHEMA_VERSION

    custom_prompt = "Keep this carefully customized translation instruction."
    manager.app_config_file.write_text(
        json.dumps({"translation_prompt": custom_prompt}, ensure_ascii=False),
        encoding="utf-8",
    )

    preserved = manager.load_app_config()

    assert preserved["translation_prompt"] == custom_prompt
    assert preserved["prompt_schema_version"] == DEFAULT_PROMPT_SCHEMA_VERSION
