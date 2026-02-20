#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片文字检测与插图翻译模块

流程：
1. 视觉模型（VL模型）检测图片中是否包含非中文文字
2. 对包含非中文文字的图片，调用火山引擎插图翻译模型生成翻译后的新图片
3. 导出EPUB时优先使用翻译后的新图片，没有则保留原图
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, Callable
import logging

logger = logging.getLogger(__name__)

# 匹配非中文字符的正则：英文字母、日文假名、韩文等
# 排除常见标点和数字，只关注真正的外语文字
_NON_CHINESE_TEXT_RE = re.compile(
    r'[A-Za-z\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]{2,}'
)


def _contains_non_chinese_text(text: str) -> bool:
    """判断文本中是否包含非中文的外语文字（至少2个连续字符）"""
    return bool(_NON_CHINESE_TEXT_RE.search(text))


class ImageTextTranslator:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self._api_client = None

    def _get_api_client(self):
        """获取API客户端实例（复用翻译API配置，用于视觉检测）"""
        if self._api_client is None:
            api_config = self.config_manager.get_api_config()
            provider = api_config.get("provider", "siliconflow")
            if provider == "deepseek":
                from ..api.deepseek_api import DeepseekAPI
                self._api_client = DeepseekAPI(api_config)
            else:
                from ..api.siliconflow_api import SiliconFlowAPI
                self._api_client = SiliconFlowAPI(api_config)
        return self._api_client

    def _reset_api_client(self):
        """重置API客户端，下次调用时重新创建"""
        self._api_client = None

    def _get_vision_model(self) -> str:
        """获取视觉模型名称 - 优先使用用户配置的视觉模型"""
        app_config = self.config_manager.get_app_config()
        configured = app_config.get("vision_model_name", "")
        if configured:
            return configured
        return "Qwen/Qwen2.5-VL-32B-Instruct"

    def detect_text_in_image(self, image_base64: str, mime_type: str) -> Dict[str, Any]:
        """使用视觉模型提取图片中的文字，然后程序判断是否包含非中文文字

        策略：让视觉模型提取图片中所有文字，再用正则判断是否含外语文字。
        这比让模型直接判断"是否有外文"更可靠。

        Returns:
            {"has_foreign_text": bool, "text_content": str}
        """
        prompt = (
            "请识别并提取这张图片中的所有文字内容，包括英文、日文、韩文、中文等所有语言的文字。\n"
            "直接输出识别到的文字内容，不要添加任何说明或格式。\n"
            "如果图片中没有任何文字，请回复：无文字"
        )

        api = self._get_api_client()
        vision_model = self._get_vision_model()
        response = api.vision_query(image_base64, mime_type, prompt, model_override=vision_model)

        if not response:
            return {"has_foreign_text": False, "text_content": ""}

        text = response.strip()

        # 如果模型回复"无文字"或类似内容，认为没有文字
        if text in ("无文字", "无", "没有文字", "图片中没有文字", "None", ""):
            return {"has_foreign_text": False, "text_content": ""}

        # 用正则判断提取到的文字中是否包含非中文外语文字
        has_foreign = _contains_non_chinese_text(text)
        return {"has_foreign_text": has_foreign, "text_content": text}

    def process_all_images(self, mapping_dir: str, target_lang: str,
                           progress_callback: Optional[Callable] = None) -> Dict[str, Any]:
        """处理mapping目录中所有图片：视觉检测 + 插图翻译

        流程：
        1. 遍历所有图片，用视觉模型检测是否含非中文文字
        2. 对含非中文文字的图片，调用火山引擎插图翻译模型生成翻译后的新图片
        3. 保存结果供EPUB导出使用
        """
        mapping_path = Path(mapping_dir)
        images_file = mapping_path / "images.json"

        if not images_file.exists():
            return {}

        images_data = json.loads(images_file.read_text(encoding="utf-8"))
        image_mappings = images_data.get("image_mappings", {})

        if not image_mappings:
            return {}

        # === 第一阶段：视觉模型检测所有图片 ===
        foreign_text_images = {}  # {image_path: image_info} 含非中文文字的图片
        total = len(image_mappings)
        skipped_count = 0
        error_count = 0

        for idx, (image_path, image_info) in enumerate(image_mappings.items()):
            if progress_callback:
                progress_callback(idx, total, f"[检测] {image_path}")

            b64_data = image_info.get("base64_data", "")
            mime_type = image_info.get("mime_type", "image/png")

            if b64_data.startswith("data:"):
                b64_data = b64_data.split(",", 1)[1] if "," in b64_data else b64_data

            if not b64_data:
                continue

            from .image_utils import convert_to_png
            converted_b64, converted_mime = convert_to_png(b64_data, mime_type)
            if converted_b64 is None:
                print(f"[图片检测] 跳过不支持的格式: {image_path} ({mime_type})")
                continue

            try:
                detection = self.detect_text_in_image(converted_b64, converted_mime)

                if detection.get("has_foreign_text"):
                    foreign_text_images[image_path] = image_info
                    print(f"  ✓ 检测到非中文文字: {image_path}")
                else:
                    skipped_count += 1
                    print(f"  - 跳过（无非中文文字）: {image_path}")
            except Exception as e:
                error_count += 1
                print(f"  ✗ 检测失败: {image_path} - {e}")
                self._reset_api_client()
                continue

        detected_count = len(foreign_text_images)
        print(f"\n📊 检测完成: 总计 {total} 张图片，"
              f"含非中文文字 {detected_count} 张，"
              f"跳过 {skipped_count} 张"
              + (f"，失败 {error_count} 张" if error_count else ""))

        if not foreign_text_images:
            if progress_callback:
                progress_callback(total, total, "完成（无需翻译）")
            return {}

        # === 第二阶段：对含非中文文字的图片调用插图翻译 ===
        volc_key = self.config_manager.get_volc_key()
        if not volc_key:
            print("⚠ 未配置火山引擎API Key，无法进行插图翻译")
            if progress_callback:
                progress_callback(total, total, "完成（缺少火山引擎Key）")
            return {}

        from .image_translator import ImageTranslator
        img_translator = ImageTranslator(self.config_manager)

        # 直接传入筛选后的图片映射，无需操作文件
        filtered_mappings = {k: v for k, v in image_mappings.items() if k in foreign_text_images}

        try:
            img2img_progress_base = total - detected_count

            def img2img_progress_cb(success, img_total, current):
                if progress_callback:
                    progress_callback(
                        img2img_progress_base + success, total,
                        f"[插图翻译] {current}"
                    )

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result_map = loop.run_until_complete(
                    img_translator.translate_images(
                        mapping_dir, target_lang, img2img_progress_cb,
                        image_mappings_override=filtered_mappings
                    )
                )
            finally:
                loop.close()

        except Exception as e:
            print(f"插图翻译出错: {e}")
            result_map = {}

        translated_count = len(result_map)
        print(f"\n📊 插图翻译完成: {translated_count}/{detected_count} 张图片翻译成功")

        if progress_callback:
            progress_callback(total, total, "完成")

        # 保存插图翻译结果到 image_translation_result.json（与原有格式兼容）
        if result_map:
            output_file = mapping_path / "image_translation_result.json"
            output_file.write_text(
                json.dumps(result_map, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        return result_map
