#!/usr/bin/env python3
"""
图片格式转换工具模块
将非标准格式图片（SVG、GIF、WebP、BMP、TIFF等）转换为PNG，
以确保视觉模型能正确识别。
"""

import base64
import io
import logging

logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg"}
_ARK_MAX_INPUT_BYTES = 30 * 1024 * 1024
_ARK_MAX_INPUT_PIXELS = 36_000_000
_ARK_MIN_EDGE_EXCLUSIVE = 14
_ARK_MAX_ASPECT_RATIO = 16


def convert_to_png(b64_data: str, mime_type: str) -> tuple:
    """将非标准格式图片转换为PNG。

    Args:
        b64_data: base64编码的图片数据
        mime_type: 图片MIME类型

    Returns:
        (new_b64_data, new_mime_type) 转换成功时返回PNG数据，
        失败时返回 (None, None)。
        如果原格式已支持，直接返回原数据。
    """
    if mime_type in SUPPORTED_MIME_TYPES:
        return b64_data, mime_type

    try:
        raw = base64.b64decode(b64_data)
    except Exception as e:
        logger.warning("base64解码失败: %s", e)
        return None, None

    png_data = _convert_svg(raw) if mime_type == "image/svg+xml" else _convert_with_pillow(raw)

    if png_data is None:
        return None, None

    new_b64 = base64.b64encode(png_data).decode("ascii")
    return new_b64, "image/png"


def convert_to_png_bytes(raw: bytes, mime_type: str) -> tuple:
    """PERF-6d：以 bytes 为输入输出进行格式转换，避免多余 base64 编解码。

    内部管道统一传 bytes，只在 HTTP JSON 边界编码一次 base64。

    Args:
        raw: 原始图片二进制数据
        mime_type: 图片MIME类型

    Returns:
        (new_raw_bytes, new_mime_type) 转换成功时返回PNG二进制数据，
        失败时返回 (None, None)。如果原格式已支持，直接返回原数据。
    """
    if mime_type in SUPPORTED_MIME_TYPES:
        return raw, mime_type

    png_data = _convert_svg(raw) if mime_type == "image/svg+xml" else _convert_with_pillow(raw)

    if png_data is None:
        return None, None

    return png_data, "image/png"


def canonicalize_for_ark_image_generation(raw: bytes, mime_type: str) -> tuple:
    """Decode and re-encode an Ark reference image as a canonical PNG.

    EPUB JPEG files can be accepted by Pillow and browsers while still being
    rejected by Ark's stricter image decoder. Re-encoding at the API boundary
    also strips metadata and guarantees that the data URI MIME matches its
    bytes. The original EPUB asset is never modified.
    """
    if not raw:
        logger.warning("Ark 参考图片为空")
        return None, None
    if len(raw) > _ARK_MAX_INPUT_BYTES:
        logger.warning("Ark 参考图片超过 30MB: %d bytes", len(raw))
        return None, None

    source = _convert_svg(raw) if mime_type == "image/svg+xml" else raw
    if source is None:
        return None, None

    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(source)) as opened:
            if getattr(opened, "n_frames", 1) > 1:
                opened.seek(0)

            width, height = opened.size
            if width <= _ARK_MIN_EDGE_EXCLUSIVE or height <= _ARK_MIN_EDGE_EXCLUSIVE:
                logger.warning("Ark 参考图片边长必须大于 14px: %dx%d", width, height)
                return None, None
            if width * height > _ARK_MAX_INPUT_PIXELS:
                logger.warning("Ark 参考图片总像素超过限制: %dx%d", width, height)
                return None, None
            ratio = width / height
            if ratio > _ARK_MAX_ASPECT_RATIO or ratio < 1 / _ARK_MAX_ASPECT_RATIO:
                logger.warning("Ark 参考图片宽高比超过限制: %dx%d", width, height)
                return None, None

            image = ImageOps.exif_transpose(opened)
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            image.load()

            output = io.BytesIO()
            image.save(output, format="PNG")
            png_data = output.getvalue()
    except ImportError:
        logger.warning("Pillow 未安装，无法规范化 Ark 参考图片")
        return None, None
    except Exception as e:
        logger.warning("Ark 参考图片解码或重编码失败: %s", e)
        return None, None

    if len(png_data) > _ARK_MAX_INPUT_BYTES:
        logger.warning("规范化后的 Ark 参考图片超过 30MB: %d bytes", len(png_data))
        return None, None
    return png_data, "image/png"


def _convert_svg(raw: bytes) -> bytes | None:
    """SVG转PNG，需要cairosvg库。"""
    try:
        import cairosvg

        return cairosvg.svg2png(bytestring=raw)
    except ImportError:
        logger.warning("cairosvg未安装，无法转换SVG图片。可通过 pip install cairosvg 安装。")
        return None
    except Exception as e:
        logger.warning("SVG转PNG失败: %s", e)
        return None


def _convert_with_pillow(raw: bytes) -> bytes | None:
    """使用Pillow将GIF/WebP/BMP/TIFF等格式转为PNG。"""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))

        # GIF取第一帧
        if hasattr(img, "n_frames") and img.n_frames > 1:
            img.seek(0)

        # 处理透明通道
        img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P", "PA") else img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        logger.warning("Pillow未安装，无法转换图片格式。")
        return None
    except Exception as e:
        logger.warning("图片格式转换失败: %s", e)
        return None
