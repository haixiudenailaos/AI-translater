#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
目标语言映射

将本项目内部使用的人类可读语言值（如「中文」「英文」）映射为
manga-image-translator 流水线识别的 VALID_LANGUAGES 代码。

未映射语言在执行前报配置错误，不得静默回退为中文或英文。
"""

from typing import Optional


# 本项目值 -> Manga 代码
LANGUAGE_TO_MANGA_CODE: dict[str, str] = {
    "中文": "CHS",
    "简体中文": "CHS",
    "繁體中文": "CHT",
    "繁体中文": "CHT",
    "英文": "ENG",
    "英语": "ENG",
    "日文": "JPN",
    "日语": "JPN",
    "韩文": "KOR",
    "韩语": "KOR",
    "法文": "FRA",
    "法语": "FRA",
    "德文": "DEU",
    "德语": "DEU",
    "西班牙文": "ESP",
    "西班牙语": "ESP",
    "俄文": "RUS",
    "俄语": "RUS",
    "葡萄牙文": "POR",
    "葡萄牙语": "POR",
    "意大利文": "ITA",
    "意大利语": "ITA",
}

# UI 文案映射：外部流水线状态 -> 中文阶段名
STAGE_LABELS: dict[str, str] = {
    "detection": "检测文字",
    "ocr": "识别文字",
    "translating": "翻译文字",
    "mask-generation": "生成去字区域",
    "inpainting": "修复原图",
    "rendering": "渲染译文",
    "upscaling": "放大图片",
    "colorizing": "上色",
    "running_pre_translation_hooks": "准备中",
}


def to_manga_lang(project_lang: str) -> Optional[str]:
    """将本项目语言值映射为 Manga 代码，未映射返回 None。"""
    if not project_lang:
        return None
    # 精确匹配
    if project_lang in LANGUAGE_TO_MANGA_CODE:
        return LANGUAGE_TO_MANGA_CODE[project_lang]
    # 去空白后小写匹配
    key = project_lang.strip()
    return LANGUAGE_TO_MANGA_CODE.get(key)


def stage_label(state: str) -> str:
    """将外部流水线状态映射为 UI 文案，未知状态原样返回。"""
    return STAGE_LABELS.get(state, state)
