#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片翻译服务：本地OCR检测 + 火山引擎图片翻译
"""

from pathlib import Path
from typing import Dict, Any
import base64
import datetime
import io
import json

from PIL import Image

from ..api.volcengine_image_api import VolcengineImageAPI


class ImageTranslator:
    def __init__(self, api_config: Dict[str, Any], target_language: str = "中文"):
        image_cfg = api_config.get("image_translation", {})
        self.target_language = target_language
        self.volc_api = VolcengineImageAPI(image_cfg)

    def _extract_text_with_ocr(self, raw_image: bytes) -> str:
        """使用本地OCR识别图片文字，失败时返回空字符串。"""
        try:
            import pytesseract
        except ImportError:
            return ""

        try:
            image = Image.open(io.BytesIO(raw_image))
            text = pytesseract.image_to_string(image, lang="eng+chi_sim+jpn")
            return (text or "").strip()
        except Exception:
            return ""

    def translate_images_in_mapping(self, mapping_dir: str) -> Dict[str, int]:
        images_file = Path(mapping_dir) / "images.json"
        if not images_file.exists():
            return {"total": 0, "translated": 0, "skipped": 0, "failed": 0}

        payload = json.loads(images_file.read_text(encoding="utf-8"))
        image_mappings = payload.get("image_mappings", {})

        stats = {"total": len(image_mappings), "translated": 0, "skipped": 0, "failed": 0}
        now = datetime.datetime.now().isoformat()

        for _, item in image_mappings.items():
            data_url = item.get("base64_data", "")
            if "," not in data_url:
                item["translation_status"] = "skipped_invalid_image"
                stats["skipped"] += 1
                continue

            prefix, b64_data = data_url.split(",", 1)
            mime = item.get("mime_type", "image/png")
            try:
                raw_image = base64.b64decode(b64_data)
            except Exception:
                item["translation_status"] = "skipped_invalid_base64"
                stats["skipped"] += 1
                continue

            detected_text = self._extract_text_with_ocr(raw_image)
            item["ocr_text"] = detected_text

            if not detected_text:
                item["translation_status"] = "skipped_no_text"
                stats["skipped"] += 1
                continue

            if not self.volc_api.is_enabled():
                item["translation_status"] = "skipped_no_image_api_key"
                stats["skipped"] += 1
                continue

            prompt = (
                f"将图中所有可见文字翻译为{self.target_language}，"
                "保持人物、背景、排版结构和画风不变，只替换文字内容。"
            )

            try:
                translated_bytes = self.volc_api.translate_image(data_url, prompt)
                if not translated_bytes:
                    item["translation_status"] = "failed_empty_result"
                    stats["failed"] += 1
                    continue

                translated_b64 = base64.b64encode(translated_bytes).decode("ascii")
                item["translated_base64_data"] = f"data:{mime};base64,{translated_b64}"
                item["translation_status"] = "translated"
                item["translated_at"] = now
                stats["translated"] += 1
            except Exception as exc:
                item["translation_status"] = f"failed:{str(exc)}"
                stats["failed"] += 1

        images_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return stats
