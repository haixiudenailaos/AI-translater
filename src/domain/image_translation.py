#!/usr/bin/env python3
"""
图片翻译领域模型

定义图片翻译操作的 Provider ID、请求、进度和结果。领域层不依赖任何
项目外模块（不导入 PIL、torch、tkinter），可独立单元测试。

设计要点：
- ImageTranslationProviderId 明确区分 Manga 默认模块和 AI 图生图 Provider。
- 请求、进度为不可变 dataclass，确保跨线程传递安全。
- 结果使用结构化字段（result_map / skipped_images / failed_images），
  不用空字典混淆「无需翻译」和「全部失败」。
- 错误信息由 Provider 脱敏后填入 failed_images，不得包含 API Key、
  完整 Base64、请求头或模型服务原始鉴权响应。
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Tuple

from .translation import OperationStatus


class ImageTranslationProviderId(str, Enum):
    """图片翻译 Provider 标识

    MANGA：manga-image-translator 确定性流水线（检测/OCR/翻译/擦除/渲染），
           是所有图片翻译入口的默认实现。
    AI_VOLCENGINE：火山引擎 Doubao-SeeDream 图生图，只能由用户显式选择。
    """

    MANGA = "manga"
    AI_VOLCENGINE = "ai_volcengine"


@dataclass(frozen=True)
class ImageTranslationRequest:
    """图片翻译请求（不可变）

    Attributes:
        mapping_dir: EPUB 映射目录，含 images.json 与 assets/。
        target_language: 目标语言（本项目值，如「中文」「英文」）。
        provider_id: 显式指定的 Provider，不允许由 Provider 自行切换。
        selected_images: 仅翻译指定图片（EPUB 原始路径元组）；
                         None 表示翻译 images.json 中的全部图片。
        source_fingerprint: 源 EPUB 内容指纹（SHA-256），用于 manifest 校验。
        config_fingerprint: 本次运行配置指纹，用于判断结果是否可复用。
    """

    mapping_dir: Path
    target_language: str
    provider_id: ImageTranslationProviderId
    selected_images: Tuple[str, ...] | None = None
    source_fingerprint: str = ""
    config_fingerprint: str = ""


@dataclass(frozen=True)
class ImageTranslationProgress:
    """图片翻译进度事件（不可变）

    Attributes:
        stage: 当前阶段（detection/ocr/translating/mask-generation/
               inpainting/rendering 等映射后的 UI 文案）。
        current: 当前已完成图片数。
        total: 总图片数。
        image_path: 当前处理的图片 EPUB 路径（可空）。
    """

    stage: str
    current: int
    total: int
    image_path: str = ""


@dataclass
class ImageTranslationResult:
    """图片翻译结果

    使用结构化字段区分成功、跳过（无需翻译）和失败，不混淆语义。

    Attributes:
        status: 操作结果状态。
        result_map: {original_epub_path: translated_relative_path} 成功映射。
        skipped_images: 无需翻译的图片列表（如未检测到文字）。
        failed_images: {original_epub_path: 脱敏错误原因} 失败映射。
        provider_id: 实际执行翻译的 Provider。
        run_id: 本次运行唯一标识（UUID）。
    """

    status: OperationStatus
    result_map: dict = field(default_factory=dict)
    skipped_images: list = field(default_factory=list)
    failed_images: dict = field(default_factory=dict)
    provider_id: ImageTranslationProviderId = ImageTranslationProviderId.MANGA
    run_id: str = ""

    @property
    def succeeded_count(self) -> int:
        return len(self.result_map)

    @property
    def is_empty_run(self) -> bool:
        """本次运行无任何成功、跳过或失败结果（如全部取消前未处理）"""
        return not self.result_map and not self.skipped_images and not self.failed_images
