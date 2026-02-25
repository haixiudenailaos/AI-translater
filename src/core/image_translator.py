#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
插图翻译模块
使用火山引擎插图翻译服务翻译EPUB中的插图
"""

import asyncio
import json
import base64
from pathlib import Path
from typing import Dict, Optional, Callable
import logging
import requests
from openai import OpenAI


logging.basicConfig(level=logging.DEBUG, format='[DEBUG] %(message)s')
logger = logging.getLogger(__name__)


class ImageTranslator:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.semaphore = asyncio.Semaphore(8)
        self.max_retries = 3

        # 火山引擎配置
        self.volc_base_url = "https://ark.cn-beijing.volces.com/api/v3"
        self.volc_model = "doubao-seedream-4-5-251128"

    def _get_api_key(self) -> str:
        return self.config_manager.get_volc_key()

    async def test_connection(self) -> bool:
        """测试火山引擎连接"""
        api_key = self._get_api_key()
        if not api_key:
            return False

        try:
            dummy_b64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAACQd1PeAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAAMSURBVFhH7cExAQAAAMKg9U9tCy8gAAAAAAAAAAAAAAAAAD4MCwABhsjxcAAAAABJRU5ErkJggg=="

            client = OpenAI(
                base_url=self.volc_base_url,
                api_key=api_key,
            )
            imagesResponse = client.images.generate(
                model=self.volc_model,
                prompt="Test connection",
                size="2K",
                response_format="url",
                extra_body={
                    "image": dummy_b64,
                    "watermark": True
                }
            )
            return bool(imagesResponse and imagesResponse.data)

        except Exception as e:
            print(f"Connection test exception: {e}")
            return False

    async def translate_images(self, mapping_dir: str, target_lang: str,
                               progress_callback: Callable[[int, int, str], None] = None,
                               image_mappings_override: Dict = None,
                               image_translations: Dict[str, Dict] = None) -> Dict[str, str]:
        """
        并发翻译图片 - 使用用户指定的代码结构
        :param mapping_dir: 映射目录路径
        :param target_lang: 目标语言
        :param progress_callback: 进度回调 (success_count, total, current_file)
        :param image_mappings_override: 可选，直接传入要翻译的图片映射，跳过读取images.json
        :return: 映射字典 {原图相对路径: 新图相对路径}
        """
        api_key = self._get_api_key()
        if not api_key:
            print("No 火山引擎 API Key found.")
            return {}

        mapping_path = Path(mapping_dir)

        if image_mappings_override is not None:
            image_mappings = image_mappings_override
        else:
            images_json_path = mapping_path / "images.json"
            if not images_json_path.exists():
                print("No images.json found.")
                return {}

            try:
                with open(images_json_path, 'r', encoding='utf-8') as f:
                    images_data = json.load(f)
            except Exception as e:
                print(f"Failed to load images.json: {e}")
                return {}

            image_mappings = images_data.get("image_mappings", {})

        if not image_mappings:
            return {}

        output_dir = mapping_path / "images"
        output_dir.mkdir(exist_ok=True)
        logger.debug(f"[translate_images] 输出目录: {output_dir}")

        result_mapping = {}
        total_images = len(image_mappings)
        success_count = 0
        processed_count = 0

        # 火山引擎使用 OpenAI client
        client = OpenAI(
            base_url=self.volc_base_url,
            api_key=api_key,
        )
        logger.debug(f"[translate_images] model={self.volc_model}")

        for idx, (name, info) in enumerate(image_mappings.items()):
            logger.debug(f"\n[translate_images] ====== 处理第 {idx+1}/{total_images} 张图片: {name} ======")
            original_path = info.get("original_path")
            base64_data = info.get("base64_data", "")
            logger.debug(f"[translate_images] 图片信息: original_path={original_path}, base64长度={len(base64_data)}")

            if not original_path or not base64_data:
                logger.warning(f"[translate_images] 缺少必要信息，跳过: original_path={bool(original_path)}, base64_data={bool(base64_data)}")
                continue

            if "," in base64_data:
                b64_str = base64_data.split(",", 1)[1]
                logger.debug(f"[translate_images] 移除data:前缀，长度={len(b64_str)}")
            else:
                b64_str = base64_data

            mime_type = info.get("mime_type", "image/png")
            from .image_utils import convert_to_png
            logger.debug(f"[translate_images] 开始格式转换，mime_type={mime_type}")
            converted_b64, converted_mime = convert_to_png(b64_str, mime_type)
            if converted_b64 is None:
                logger.warning(f"[translate_images] 格式转换失败，跳过: {name} ({mime_type})")
                print(f"[插图翻译] 跳过不支持的图片格式: {name} ({mime_type})")
                continue
            b64_str = converted_b64
            logger.debug(f"[translate_images] 格式转换成功，mime_type={converted_mime}, base64长度={len(b64_str)}")

            processed_count += 1
            if progress_callback:
                progress_callback(success_count, total_images, name)

            logger.debug(f"[translate_images] 调用_process_single_image处理图片...")
            # 获取该图片的文字翻译结果（如果有的话）
            image_translation = None
            if image_translations and name in image_translations:
                image_translation = image_translations[name]
                logger.debug(f"[translate_images] 找到文字翻译结果: {image_translation}")

            res = await self._process_single_image(
                client, name, b64_str, target_lang, output_dir, image_translation,
                mime_type=converted_mime
            )

            if res:
                orig_name, new_filename = res
                result_mapping[orig_name] = new_filename
                success_count += 1
                logger.debug(f"[translate_images] 处理成功: {orig_name} -> {new_filename}")
            else:
                logger.warning(f"[translate_images] 处理失败，无结果返回: {name}")

        if progress_callback:
            progress_callback(success_count, total_images, "完成")

        logger.debug(f"\n[translate_images] ====== 插图翻译统计 ======")
        logger.debug(f"[translate_images] 总计: {total_images} 张")
        logger.debug(f"[translate_images] 处理: {processed_count} 张")
        logger.debug(f"[translate_images] 成功: {success_count} 张")

        return result_mapping

    async def _process_single_image(self, client: OpenAI,
                                   original_name: str, b64_str: str, target_lang: str,
                                   output_dir: Path, image_translation: Dict = None,
                                   mime_type: str = "image/png") -> Optional[tuple]:
        """处理单张图片：上传 -> 生成 -> 下载 -> 保存
        
        Args:
            image_translation: 可选，文字翻译结果，格式: {"original_text": ..., "translated_text": ...}
        """
        logger.debug(f"[_process_single_image] 开始处理: {original_name}")
        logger.debug(f"[_process_single_image] image_translation: {image_translation}")
        
        # 构建提示词 - 精确指导模型只翻译文字，不改变图片其他内容
        base_prompt = (
            f"这是一张包含外文文字的图片。请仅将图片中的所有外文文字翻译为{target_lang}，"
            f"严格保持图片的原始构图、背景、色彩、画风和所有非文字元素完全不变。"
            f"只替换文字内容，文字的位置、大小、排版风格应与原图一致。"
            f"不要添加、删除或修改任何非文字的图像元素。"
        )

        # 如果有文字翻译结果，将其作为精确参考加入提示词
        if image_translation and image_translation.get("translated_text"):
            ref_text = image_translation.get("translated_text", "")
            orig_text = image_translation.get("original_text", "")
            prompt = (
                f"{base_prompt}\n\n"
                f"【精确翻译对照】\n"
                f"原文：{orig_text}\n"
                f"译文：{ref_text}\n\n"
                f"请严格按照上述译文替换图片中对应的原文文字，不要自行翻译或修改译文内容。"
            )
            logger.debug(f"[_process_single_image] 使用包含参考翻译的prompt")
        else:
            prompt = base_prompt
            logger.debug(f"[_process_single_image] 使用基础prompt: {prompt}")
        
        # 构造符合火山引擎要求的 data URI 格式
        # 格式: data:image/<格式>;base64,<base64字符串>
        # 注意：图片格式需要小写
        if mime_type.startswith('image/'):
            image_format = mime_type.split('/')[1].lower()
        else:
            image_format = 'png'
        data_uri = f"data:image/{image_format};base64,{b64_str}"
        logger.debug(f"[_process_single_image] 构造的 data URI: {data_uri[:80]}...")

        for attempt in range(self.max_retries):
            logger.debug(f"[_process_single_image] 尝试第 {attempt + 1}/{self.max_retries} 次...")
            try:
                logger.debug(f"[_process_single_image] 调用火山引擎API生成图片...")
                imagesResponse = client.images.generate(
                    model=self.volc_model,
                    prompt=prompt,
                    size="4K",
                    response_format="url",
                    extra_body={
                        "image": data_uri,
                        "watermark": True
                    }
                )
                logger.debug(f"[_process_single_image] API响应类型: {type(imagesResponse)}")

                if not imagesResponse or not imagesResponse.data:
                    logger.error(f"[_process_single_image] 响应中没有数据: {original_name}")
                    print(f"No data in response for {original_name}")
                    return None

                image_url = imagesResponse.data[0].url
                logger.debug(f"[_process_single_image] 获取图片URL: {image_url[:80]}...")

                logger.debug(f"[_process_single_image] 开始下载图片...")
                img_resp = requests.get(image_url, timeout=60)
                logger.debug(f"[_process_single_image] 下载响应码: {img_resp.status_code}")
                if img_resp.status_code != 200:
                    raise Exception(f"Download failed {img_resp.status_code}")
                new_img_data = img_resp.content
                logger.debug(f"[_process_single_image] 下载完成，图片大小: {len(new_img_data)} bytes")

                orig_size = len(base64.b64decode(b64_str))
                logger.debug(f"[_process_single_image] 原图大小: {orig_size} bytes, 新图大小: {len(new_img_data)} bytes")
                if len(new_img_data) < orig_size * 0.9:
                    logger.warning(f"[_process_single_image] 生成图片太小: {original_name}: {len(new_img_data)} < {orig_size * 0.9}")
                    print(f"Generated image too small for {original_name}: {len(new_img_data)} < {orig_size * 0.9}")
                    return None

                p = Path(original_name)
                new_filename = f"{p.stem}_translated{p.suffix}"
                save_path = output_dir / new_filename
                logger.debug(f"[_process_single_image] 保存路径: {save_path}")

                with open(save_path, "wb") as f:
                    f.write(new_img_data)
                logger.debug(f"[_process_single_image] 图片保存成功")

                return original_name, new_filename

            except Exception as e:
                logger.error(f"[_process_single_image] 异常 (尝试 {attempt + 1}/{self.max_retries}): {type(e).__name__}: {e}", exc_info=True)
                if attempt == self.max_retries - 1:
                    print(f"Failed to process {original_name} after {self.max_retries} attempts: {e}")
                    return None

                wait_time = 2 ** attempt
                logger.debug(f"[_process_single_image] 等待 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)

        logger.error(f"[_process_single_image] 所有重试失败")
        return None
