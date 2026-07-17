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
