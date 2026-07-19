#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""

from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
_manga_root = _project_root / "third_party" / "manga-image-translator" / "manga_translator"
PyInstaller hook: manga-image-translator

收集 manga_translator 包及其运行时所需的数据文件（YAML 配置、tokenizer 资源等）。
重依赖（torch/onnxruntime/cv2）由各自的官方 hook 处理，本 hook 仅负责本包自身资源。

设计原则：
- 不在 hook 中 import torch / manga_translator，避免打包期触发重依赖加载。
- 仅声明 datas 与 hiddenimports，PyInstaller 会在打包期惰性收集。
- 字体资源由本项目的字体目录单独处理（不依赖 manga_translator 内置字体）。
"""

# ── manga_translator 包内数据文件 ─────────────────
# inpainting 模块的 SD 配置 YAML（guided_ldm_inpaint4_v15.yaml / guided_ldm_inpaint9_v15.yaml）
datas = [
    (
        str(
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "third_party"
            / "manga-image-translator"
            / "manga_translator"
            / "inpainting"
            / "guided_ldm_inpaint4_v15.yaml"
        ),
        "manga_translator/inpainting",
    ),
    (
        str(
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "third_party"
            / "manga-image-translator"
            / "manga_translator"
            / "inpainting"
            / "guided_ldm_inpaint9_v15.yaml"
        ),
        "manga_translator/inpainting",
    ),
]

# translators/tokenizers 下的预置 tokenizer 配置（deepseek 等）
datas += [
    (
        str(
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "third_party"
            / "manga-image-translator"
            / "manga_translator"
            / "translators"
            / "tokenizers"
            / "deepseek"
            / "tokenizer.json"
        ),
        "manga_translator/translators/tokenizers/deepseek",
    ),
    (
        str(
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "third_party"
            / "manga-image-translator"
            / "manga_translator"
            / "translators"
            / "tokenizers"
            / "deepseek"
            / "tokenizer_config.json"
        ),
        "manga_translator/translators/tokenizers/deepseek",
    ),
]

# ── 隐藏导入：子模块被动态加载，PyInstaller 静态分析无法识别 ──
hiddenimports = [
    # 主模块
    'manga_translator',
    'manga_translator.manga_translator',
    'manga_translator.config',
    'manga_translator.args',
    'manga_translator.save',

    # 检测器
    'manga_translator.detection',
    'manga_translator.detection.common',
    'manga_translator.detection.default',
    'manga_translator.detection.dbnet_convnext',
    'manga_translator.detection.craft',
    'manga_translator.detection.ctd',
    'manga_translator.detection.none',
    'manga_translator.detection.paddle_rust',
    'manga_translator.detection.default_utils.CRAFT_resnet34',
    'manga_translator.detection.default_utils.DBHead',
    'manga_translator.detection.default_utils.DBNet_resnet101',
    'manga_translator.detection.default_utils.DBNet_resnet34',

    # OCR
    'manga_translator.ocr',
    'manga_translator.ocr.common',
    'manga_translator.ocr.model_32px',
    'manga_translator.ocr.model_48px',
    'manga_translator.ocr.model_48px_ctc',
    'manga_translator.ocr.model_manga_ocr',
    'manga_translator.ocr.model_ocr_large',
    'manga_translator.ocr.xpos_relative_position',

    # 翻译器
    'manga_translator.translators',
    'manga_translator.translators.common',
    'manga_translator.translators.external_llm',
    'manga_translator.translators.keys',
    'manga_translator.translators.none',
    'manga_translator.translators.original',

    # Inpainting
    'manga_translator.inpainting',
    'manga_translator.inpainting.common',
    'manga_translator.inpainting.none',
    'manga_translator.inpainting.original',
    'manga_translator.inpainting.inpainting_lama',
    'manga_translator.inpainting.inpainting_lama_mpe',
    'manga_translator.inpainting.inpainting_aot',
    'manga_translator.inpainting.inpainting_attn',
    'manga_translator.inpainting.inpainting_sd',
    'manga_translator.inpainting.guided_ldm_inpainting',
    'manga_translator.inpainting.sd_hack',

    # 渲染
    'manga_translator.rendering',
    'manga_translator.rendering.text_render',
    'manga_translator.rendering.text_render_eng',
    'manga_translator.rendering.text_render_pillow_eng',

    # 上采样
    'manga_translator.upscaling',
    'manga_translator.upscaling.common',
    'manga_translator.upscaling.esrgan',
    'manga_translator.upscaling.esrgan_pytorch',
    'manga_translator.upscaling.waifu2x',

    # 文本行合并、mask 精化、气泡提取
    'manga_translator.textline_merge',
    'manga_translator.mask_refinement',
    'manga_translator.mask_refinement.text_mask_utils',
    'manga_translator.rendering.ballon_extractor',

    # 工具
    'manga_translator.utils',
    'manga_translator.utils.generic',
    'manga_translator.utils.generic2',
    'manga_translator.utils.inference',
    'manga_translator.utils.sort',
    'manga_translator.utils.textblock',
    'manga_translator.utils.threading',
    'manga_translator.utils.bubble',

    # LDM 子模块（inpainting_sd 使用）
    'manga_translator.inpainting.ldm.util',
    'manga_translator.inpainting.ldm.models.diffusion.ddpm',
    'manga_translator.inpainting.ldm.models.diffusion.ddim',
    'manga_translator.inpainting.ldm.models.diffusion.plms',
    'manga_translator.inpainting.ldm.models.diffusion.dpm_solver',
    'manga_translator.inpainting.ldm.modules.diffusionmodules.openaimodel',
    'manga_translator.inpainting.ldm.modules.encoders.modules',
    'manga_translator.inpainting.ldm.modules.distributions.distributions',

    # Colorization
    'manga_translator.colorization',
    'manga_translator.colorization.common',
    'manga_translator.colorization.manga_colorization_v2',
]
