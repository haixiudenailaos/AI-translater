#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火山引擎图像翻译API封装
"""

from typing import Dict, Any, Optional
import requests
from openai import OpenAI


class VolcengineImageAPI:
    def __init__(self, config: Dict[str, Any]):
        self.api_key = (config.get("api_key") or "").strip()
        self.base_url = config.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
        self.model_name = config.get("model_name", "doubao-seedream-4-5-251128")
        self.response_format = config.get("response_format", "url")
        self.size = config.get("size", "2K")

    def is_enabled(self) -> bool:
        return bool(self.api_key)

    def translate_image(self, image_data_url: str, prompt: str) -> Optional[bytes]:
        """调用火山引擎图片生成接口，返回翻译后图片二进制内容。"""
        if not self.api_key:
            return None

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        resp = client.images.generate(
            model=self.model_name,
            prompt=prompt,
            size=self.size,
            response_format=self.response_format,
            extra_body={
                "image": image_data_url,
                "watermark": False,
            },
        )

        image_url = resp.data[0].url if resp and resp.data else None
        if not image_url:
            return None

        download_resp = requests.get(image_url, timeout=60)
        download_resp.raise_for_status()
        return download_resp.content
