#!/usr/bin/env python3
"""
图片文字检测与插图翻译模块

流程：
1. 视觉模型（VL模型）检测图片中是否包含非中文文字
2. 对包含非中文文字的图片，调用火山引擎插图翻译模型生成翻译后的新图片
3. 导出EPUB时优先使用翻译后的新图片，没有则保留原图
"""

import base64
import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict

from ..infrastructure.image_asset_store import load_image_bytes
from ..infrastructure.mapping_repository import resolve_mapping_file
from ..utils.file_handler import write_json_atomic

logger = logging.getLogger(__name__)

# 匹配非中文字符的正则：英文字母、日文假名、韩文等
# 排除常见标点和数字，只关注真正的外语文字
_NON_CHINESE_TEXT_RE = re.compile(r"[A-Za-z\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]{2,}")


# R2-BUG-017：图片文字检测状态
DETECTION_NO_TEXT = "no_text"  # 图片中确实没有外文文字
DETECTION_FOREIGN_TEXT = "foreign_text"  # 检测到非中文文字
DETECTION_FAILED = "failed"  # 检测请求失败（鉴权、超时、服务端错误等）


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
        """重置API客户端，下次调用时重新创建。

        BUG-005：重置前先关闭旧实例，避免连接和心跳线程累积。
        """
        if self._api_client is not None:
            try:
                self._api_client.close()
            except Exception as exc:
                logger.warning("关闭 API 客户端失败: %s", exc)
            self._api_client = None

    def close(self):
        """BUG-005：关闭持有的 API 客户端，幂等可安全多次调用。"""
        self._reset_api_client()

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

        R2-BUG-017：区分"无外文"和"检测失败"，返回 status 字段：
        - DETECTION_NO_TEXT: 图片中确实没有外文文字
        - DETECTION_FOREIGN_TEXT: 检测到非中文文字
        - DETECTION_FAILED: 检测请求失败（鉴权、超时、服务端错误等）

        Returns:
            {"status": str, "has_foreign_text": bool, "text_content": str}
        """
        prompt = (
            "请识别并提取这张图片中的所有文字内容，包括英文、日文、韩文、中文等所有语言的文字。\n"
            "直接输出识别到的文字内容，不要添加任何说明或格式。\n"
            "如果图片中没有任何文字，请回复：无文字"
        )

        api = self._get_api_client()
        vision_model = self._get_vision_model()
        response = api.vision_query(image_base64, mime_type, prompt, model_override=vision_model)

        # R2-BUG-017：空响应代表检测失败，不是"无文字"
        if not response:
            return {
                "status": DETECTION_FAILED,
                "has_foreign_text": False,
                "text_content": "",
            }

        text = response.strip()

        # 如果模型回复"无文字"或类似内容，认为没有文字
        if text in ("无文字", "无", "没有文字", "图片中没有文字", "None", ""):
            return {
                "status": DETECTION_NO_TEXT,
                "has_foreign_text": False,
                "text_content": "",
            }

        # 用正则判断提取到的文字中是否包含非中文外语文字
        has_foreign = _contains_non_chinese_text(text)
        return {
            "status": DETECTION_FOREIGN_TEXT if has_foreign else DETECTION_NO_TEXT,
            "has_foreign_text": has_foreign,
            "text_content": text,
        }

    def process_all_images(
        self, mapping_dir: str, target_lang: str, progress_callback: Callable | None = None
    ) -> Dict[str, Any]:
        """处理mapping目录中所有图片：视觉检测 + 插图翻译

        流程：
        1. 遍历所有图片，用视觉模型检测是否含非中文文字
        2. 对含非中文文字的图片，调用火山引擎插图翻译模型生成翻译后的新图片
        3. 保存结果供EPUB导出使用

        R2-BUG-017：区分检测失败和无需翻译，全部失败时不显示成功。
        R2-BUG-018：始终写入 image_translation_result.json，即使结果为空，
                    避免旧文件被误用。
        """
        mapping_path = Path(mapping_dir)
        images_file = resolve_mapping_file(mapping_path, "images.json")

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

            # PERF-6d：内部管道统一传 bytes，避免冗余 base64 编解码循环。
            raw_bytes = load_image_bytes(mapping_path, image_info)
            mime_type = image_info.get("mime_type", "image/png")

            if not raw_bytes:
                continue

            from .image_utils import convert_to_png_bytes

            converted_bytes, converted_mime = convert_to_png_bytes(raw_bytes, mime_type)
            if converted_bytes is None:
                print(f"[图片检测] 跳过不支持的格式: {image_path} ({mime_type})")
                continue

            # PERF-6d：只在 HTTP JSON 边界编码一次 base64
            converted_b64 = base64.b64encode(converted_bytes).decode("ascii")

            try:
                detection = self.detect_text_in_image(converted_b64, converted_mime)
                status = detection.get("status", DETECTION_FAILED)

                if status == DETECTION_FOREIGN_TEXT:
                    foreign_text_images[image_path] = image_info
                    print(f"  ✓ 检测到非中文文字: {image_path}")
                elif status == DETECTION_NO_TEXT:
                    skipped_count += 1
                    print(f"  - 跳过（无非中文文字）: {image_path}")
                else:
                    # R2-BUG-017：检测失败不计入"无需翻译"
                    error_count += 1
                    print(f"  ✗ 检测失败: {image_path}")
            except Exception as e:
                error_count += 1
                print(f"  ✗ 检测失败: {image_path} - {e}")
                self._reset_api_client()
                continue

        detected_count = len(foreign_text_images)
        print(
            f"\n📊 检测完成: 总计 {total} 张图片，"
            f"含非中文文字 {detected_count} 张，"
            f"跳过 {skipped_count} 张" + (f"，失败 {error_count} 张" if error_count else "")
        )

        # R2-BUG-017：全部检测失败时不显示"无需翻译"的成功提示
        if not foreign_text_images:
            if error_count > 0 and error_count + skipped_count == total - (
                total - error_count - skipped_count
            ):
                # 有失败项，不显示普通完成
                msg = f"完成（{error_count} 张检测失败）" if error_count else "完成（无需翻译）"
            else:
                msg = "完成（无需翻译）"
            if progress_callback:
                progress_callback(total, total, msg)
            # R2-BUG-018：即使没有需要翻译的图片，也写入空结果文件
            # 避免旧翻译结果被误用
            self._write_image_translation_result(mapping_path, {})
            return {}

        # === 第二阶段：对含非中文文字的图片调用插图翻译 ===
        volc_key = self.config_manager.get_volc_key()
        if not volc_key:
            print("⚠ 未配置火山引擎API Key，无法进行插图翻译")
            if progress_callback:
                progress_callback(total, total, "完成（缺少火山引擎Key）")
            # R2-BUG-018：写入空结果
            self._write_image_translation_result(mapping_path, {})
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
                        img2img_progress_base + success, total, f"[插图翻译] {current}"
                    )

            result_map = img_translator.translate_images(
                mapping_dir,
                target_lang,
                img2img_progress_cb,
                image_mappings_override=filtered_mappings,
            )

        except Exception as e:
            print(f"插图翻译出错: {e}")
            result_map = {}

        translated_count = len(result_map)
        print(f"\n📊 插图翻译完成: {translated_count}/{detected_count} 张图片翻译成功")

        if progress_callback:
            progress_callback(total, total, "完成")

        # R2-BUG-018：始终写入结果文件，包含运行元数据
        self._write_image_translation_result(mapping_path, result_map)

        return result_map

    def _write_image_translation_result(self, mapping_path: Path, result_map: Dict[str, str]):
        """写入图片翻译结果文件，包含运行元数据。

        R2-BUG-018：始终写入，即使 result_map 为空。
        旧文件会被覆盖，避免导出时使用过期的翻译结果。
        """
        output_file = mapping_path / "image_translation_result.json"
        payload = {
            "result_map": result_map,
            "run_at": datetime.datetime.now().isoformat(),
            "result_count": len(result_map),
        }
        # BUG-006：使用原子写入，失败时旧文件保持不变
        write_json_atomic(output_file, payload)
