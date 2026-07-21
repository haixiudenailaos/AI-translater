from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config.image_ocr import (
    SILICONFLOW_OCR_DEFAULT_BASE_URL,
    SILICONFLOW_OCR_DEFAULT_MODEL,
)
from src.core.image_text_translator import (
    DETECTION_FOREIGN_TEXT,
    DETECTION_NO_TEXT,
    ImageTextTranslator,
)
from src.domain.errors import ImageTranslationCancelled
from src.ui.image_translation_handler import ImageTranslationHandler


class _ModeDialog:
    def __init__(self, requested_width: int, requested_height: int) -> None:
        self.requested_width = requested_width
        self.requested_height = requested_height
        self.geometry_value = None
        self.updated = False

    def update_idletasks(self) -> None:
        self.updated = True

    def winfo_reqwidth(self) -> int:
        return self.requested_width

    def winfo_reqheight(self) -> int:
        return self.requested_height

    def geometry(self, value: str) -> None:
        self.geometry_value = value


class _Screen:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def winfo_screenwidth(self) -> int:
        return self.width

    def winfo_screenheight(self) -> int:
        return self.height


def _write_images(mapping_dir: Path) -> None:
    payload = {
        "image_mappings": {
            "images/foreign.png": {
                "original_path": "images/foreign.png",
                "base64_data": "aW1hZ2UtMQ==",
                "mime_type": "image/png",
            },
            "images/plain.png": {
                "original_path": "images/plain.png",
                "base64_data": "aW1hZ2UtMg==",
                "mime_type": "image/png",
            },
        }
    }
    (mapping_dir / "images.json").write_text(json.dumps(payload), encoding="utf-8")


def test_image_translation_mode_dialog_sizes_to_content_and_keeps_margin():
    handler = ImageTranslationHandler.__new__(ImageTranslationHandler)
    handler.root = _Screen(1920, 1080)
    dialog = _ModeDialog(requested_width=480, requested_height=348)

    handler._size_and_center_mode_dialog(dialog)

    assert dialog.updated is True
    assert dialog.geometry_value == "480x348+720+366"


def test_image_translation_mode_dialog_is_clamped_to_small_screen():
    handler = ImageTranslationHandler.__new__(ImageTranslationHandler)
    handler.root = _Screen(640, 480)
    dialog = _ModeDialog(requested_width=700, requested_height=520)

    handler._size_and_center_mode_dialog(dialog)

    assert dialog.geometry_value == "576x416+32+32"


def test_smart_flow_sends_only_detected_images_to_generation(tmp_path, monkeypatch):
    _write_images(tmp_path)
    config = MagicMock()
    config.get_volc_key.return_value = "test-key"
    config.get_api_config.return_value = {
        "provider": "siliconflow",
        "base_url": SILICONFLOW_OCR_DEFAULT_BASE_URL,
        "model_name": SILICONFLOW_OCR_DEFAULT_MODEL,
        "api_key": "ocr-key",
    }
    translator = ImageTextTranslator(config)

    detections = iter(
        [
            {
                "status": DETECTION_FOREIGN_TEXT,
                "has_foreign_text": True,
                "text_content": "English",
            },
            {
                "status": DETECTION_NO_TEXT,
                "has_foreign_text": False,
                "text_content": "",
            },
        ]
    )
    monkeypatch.setattr(translator, "detect_text_in_image", lambda *_args: next(detections))
    monkeypatch.setattr(
        "src.core.image_utils.convert_to_png_bytes",
        lambda data, _mime: (data, "image/png"),
    )

    captured = {}

    class FakeImageTranslator:
        def __init__(self, _config):
            pass

        def translate_images(self, _mapping_dir, _target, _progress, **kwargs):
            captured.update(kwargs["image_mappings_override"])
            return {"images/foreign.png": "images/foreign_translated.png"}

        def close(self):
            pass

    monkeypatch.setattr("src.core.image_translator.ImageTranslator", FakeImageTranslator)

    result = translator.process_all_images(str(tmp_path), "中文")

    assert set(captured) == {"images/foreign.png"}
    assert result == {"images/foreign.png": "images/foreign_translated.png"}
    assert translator.last_summary == {
        "total": 2,
        "detected": 1,
        "translated": 1,
        "skipped": 1,
        "failed": 0,
    }
    manifest = json.loads((tmp_path / "image_translation_result.json").read_text(encoding="utf-8"))
    assert manifest["provider"] == "ai_volcengine"
    assert manifest["result_map"] == result


def test_no_ocr_falls_back_to_translating_every_image(tmp_path, monkeypatch):
    _write_images(tmp_path)
    captured = {}

    class NoOcrConfig:
        def is_image_ocr_configured(self):
            return False

        def get_volc_key(self):
            return "volc-key"

    class FakeImageTranslator:
        def __init__(self, _config):
            pass

        def translate_images(self, _mapping_dir, _target, _progress, **kwargs):
            captured.update(kwargs["image_mappings_override"])
            return {name: f"{Path(name).stem}_translated.png" for name in captured}

        def close(self):
            pass

    monkeypatch.setattr("src.core.image_translator.ImageTranslator", FakeImageTranslator)

    translator = ImageTextTranslator(NoOcrConfig())
    result = translator.process_all_images(str(tmp_path), "中文")

    assert set(captured) == {"images/foreign.png", "images/plain.png"}
    assert set(result) == set(captured)
    assert translator.last_summary == {
        "total": 2,
        "detected": 2,
        "translated": 2,
        "skipped": 0,
        "failed": 0,
    }


def test_missing_ocr_requires_cost_confirmation_before_full_translation(tmp_path):
    handler = ImageTranslationHandler.__new__(ImageTranslationHandler)
    handler.root = object()
    handler.config_manager = MagicMock()
    handler.config_manager.get_volc_key.return_value = "volc-key"
    handler.config_manager.is_image_ocr_configured.return_value = False
    handler._start_worker = MagicMock()

    with patch(
        "src.ui.image_translation_handler.messagebox.askyesno", return_value=True
    ) as confirm:
        handler._start_image_text_translation(tmp_path)

    confirm.assert_called_once()
    handler._start_worker.assert_called_once_with("all", tmp_path)


def test_siliconflow_key_automatically_enables_default_ocr(tmp_config_manager):
    result = tmp_config_manager.save_api_config(
        {
            "provider": "siliconflow",
            "api_key": "sf-ocr-key",
            "base_url": SILICONFLOW_OCR_DEFAULT_BASE_URL,
            "model_name": "text-model",
        }
    )
    assert result

    ocr = tmp_config_manager.get_image_ocr_runtime_config()

    assert ocr == {
        "configured": True,
        "source": "siliconflow",
        "base_url": SILICONFLOW_OCR_DEFAULT_BASE_URL,
        "model_name": SILICONFLOW_OCR_DEFAULT_MODEL,
        "api_key": "sf-ocr-key",
        "error": "",
    }


def test_custom_ocr_uses_dedicated_key_and_normalizes_endpoint(tmp_config_manager):
    app_config = tmp_config_manager.get_app_config()
    app_config["image_translation"]["ocr"] = {
        "base_url": "https://ocr.example.com/v1/chat/completions/",
        "model": "custom-ocr-model",
    }
    assert tmp_config_manager.save_app_config(app_config)
    assert tmp_config_manager.save_ocr_key("custom-ocr-key")

    ocr = tmp_config_manager.get_image_ocr_runtime_config()

    assert ocr["configured"] is True
    assert ocr["source"] == "custom"
    assert ocr["base_url"] == "https://ocr.example.com/v1"
    assert ocr["model_name"] == "custom-ocr-model"
    assert ocr["api_key"] == "custom-ocr-key"


def test_siliconflow_key_is_not_sent_to_custom_ocr_endpoint(tmp_config_manager):
    assert tmp_config_manager.save_api_config(
        {
            "provider": "siliconflow",
            "api_key": "sf-private-key",
            "base_url": SILICONFLOW_OCR_DEFAULT_BASE_URL,
            "model_name": "text-model",
        }
    )
    app_config = tmp_config_manager.get_app_config()
    app_config["image_translation"]["ocr"] = {
        "base_url": "https://ocr.example.com/v1",
        "model": "custom-ocr-model",
    }
    assert tmp_config_manager.save_app_config(app_config)

    ocr = tmp_config_manager.get_image_ocr_runtime_config()

    assert ocr["configured"] is False
    assert ocr["source"] == "custom"
    assert ocr["api_key"] == ""


def test_legacy_vision_model_is_migrated_to_ocr_config(tmp_config_manager):
    tmp_config_manager.app_config_file.write_text(
        json.dumps(
            {
                "vision_model_name": "legacy/custom-ocr-model",
                "image_text_translation_enabled": True,
            }
        ),
        encoding="utf-8",
    )

    loaded = tmp_config_manager.load_app_config()

    assert loaded["image_translation"]["ocr"] == {
        "base_url": "",
        "model": "legacy/custom-ocr-model",
    }


def test_smart_flow_honors_cancel_before_vision_request(tmp_path):
    _write_images(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()
    translator = ImageTextTranslator(MagicMock())

    with pytest.raises(ImageTranslationCancelled):
        translator.process_all_images(str(tmp_path), "中文", cancel_event=cancel_event)


def test_result_writer_clears_stale_mapping_on_empty_run(tmp_path):
    (tmp_path / "image_translation_result.json").write_text(
        json.dumps({"old.png": "old_translated.png"}), encoding="utf-8"
    )

    ImageTextTranslator.write_image_translation_result(tmp_path, {})

    manifest = json.loads((tmp_path / "image_translation_result.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "empty"
    assert manifest["result_map"] == {}


def test_v15_vision_model_is_the_fallback():
    config = MagicMock()
    config.get_app_config.return_value = {}

    translator = ImageTextTranslator(config)

    assert translator._get_vision_model() == "PaddlePaddle/PaddleOCR-VL-1.5"
