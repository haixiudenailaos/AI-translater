#!/usr/bin/env python3
"""
插图翻译模块
使用火山引擎插图翻译服务翻译EPUB中的插图
"""

import base64
import binascii
import hashlib
import json
import logging
import threading
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import urlparse

import httpx
import requests
from volcenginesdkarkruntime import Ark
from volcenginesdkarkruntime._exceptions import (
    ArkAPIConnectionError,
    ArkAPITimeoutError,
    ArkAuthenticationError,
    ArkBadRequestError,
)

from ..config.translation_profile import normalize_openai_base_url
from ..config.volcengine_image import (
    VOLCENGINE_IMAGE_DEFAULT_BASE_URL,
    VOLCENGINE_IMAGE_DEFAULT_MODEL,
    VOLCENGINE_IMAGE_MODEL_ECONOMY,
    VOLCENGINE_IMAGE_MODEL_HIGH_QUALITY,
)
from ..domain.errors import ImageTranslationCancelled
from ..infrastructure.image_asset_store import load_image_bytes
from ..infrastructure.mapping_repository import resolve_mapping_file

# P2-7：移除模块导入期 logging.basicConfig() 副作用。
# 全局日志配置应由 src.utils.logger.setup_logging 在组合根中显式完成，
# 模块级 basicConfig 会抢先把 root logger 钉死在 DEBUG，污染所有调用方。
logger = logging.getLogger(__name__)

_IMAGE_API_TIMEOUT = httpx.Timeout(
    connect=20.0,
    read=300.0,
    write=60.0,
    pool=20.0,
)
_IMAGE_API_LIMITS = httpx.Limits(
    max_connections=5,
    # 图片生成耗时较长，Windows 上复用半失效连接容易触发 WinError 10053。
    max_keepalive_connections=0,
)

# All currently supported Seedream variants accept 2K. Seedream 5.0 pro does
# not accept 4K or the sequential_image_generation request field.
_IMAGE_GENERATION_SIZE = "2K"

# Valid 64x64 RGB PNG used by the paid connection test. Keep this above Ark's
# minimum 14px edge limit and verify it in a regression test before shipping.
_CONNECTION_TEST_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAS0lEQVR42u3PMQ0AAAwDoPo33UrY"
    "vQQckD4XAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAYHL"
    "AMpT0sIcNbcEAAAAAElFTkSuQmCC"
)


def _build_image_data_uri(b64_data: str, mime_type: str) -> tuple[str, str]:
    """Build a canonical data URI accepted by Ark image generation.

    Ark validates the complete ``data:image/...;base64,...`` value.  Re-encoding
    the decoded bytes removes whitespace or non-canonical padding left by old
    mapping files and normalizes the legacy ``image/jpg`` MIME spelling.
    """
    raw_b64 = (b64_data or "").strip()
    if "," in raw_b64 and raw_b64.lower().startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    raw_b64 = "".join(raw_b64.split())
    if not raw_b64:
        raise ValueError("图片 Base64 数据为空")

    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("图片 Base64 数据无效") from exc
    if not decoded:
        raise ValueError("图片 Base64 数据为空")

    normalized_mime = (mime_type or "image/png").strip().lower()
    if normalized_mime == "image/jpg":
        normalized_mime = "image/jpeg"
    if not normalized_mime.startswith("image/"):
        normalized_mime = "image/png"
    image_format = normalized_mime.split("/", 1)[1]
    canonical_b64 = base64.b64encode(decoded).decode("ascii")
    return f"data:image/{image_format};base64,{canonical_b64}", normalized_mime


# P1-9：远程下载信任边界配置
#
# 火山引擎 Ark 图片生成返回的 URL 通常位于 volces.com / bytedance 系 CDN。
# 只允许 HTTPS + 可信 host，拒绝内网地址和重定向到内网。
_ALLOWED_DOWNLOAD_HOST_SUFFIXES = (
    ".volces.com",
    ".bytedance.com",
    ".bytetcc.com",
    ".byteimg.com",
    ".bytecdn.cn",
)

# 允许的 Content-Type（下载返回的图片 MIME）
_ALLOWED_DOWNLOAD_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/svg+xml",
    "image/webp",
}

# 下载最大字节数（50MB，足够覆盖 4K 图片）
_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
_SVG_BLOCKED_ELEMENTS = {"script", "foreignobject"}
_SVG_HREF_ATTRIBUTES = {"href", "{http://www.w3.org/1999/xlink}href"}


class ImageDownloadError(Exception):
    """P1-9：图片下载信任边界校验失败。"""


def _rasterize_safe_svg(data: bytes) -> bytes:
    """Reject active SVG constructs and rasterize the remaining static image."""
    if b"<!doctype" in data.lower() or b"<!entity" in data.lower():
        raise ImageDownloadError("SVG 不允许声明外部实体")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ImageDownloadError("SVG XML 格式无效") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ImageDownloadError("下载内容不是 SVG 根元素")
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1].lower()
        if local_name in _SVG_BLOCKED_ELEMENTS:
            raise ImageDownloadError("SVG 包含不允许的主动内容")
        for attribute, value in element.attrib.items():
            if attribute.rsplit("}", 1)[-1].lower().startswith("on"):
                raise ImageDownloadError("SVG 包含不允许的事件属性")
            if attribute in _SVG_HREF_ATTRIBUTES and value and not value.startswith("#"):
                raise ImageDownloadError("SVG 不允许外部资源引用")
    try:
        import cairosvg

        return cairosvg.svg2png(bytestring=ElementTree.tostring(root), unsafe=False)
    except ImageDownloadError:
        raise
    except Exception as exc:
        raise ImageDownloadError("SVG 安全栅格化失败") from exc


# P1-9：严格的魔数白名单，用于下载内容验证。
#
# ``_detect_image_format`` 对未知内容默认返回 "png"，在安全上下文中
# 不可接受。这里用显式的前缀匹配，只接受已知图片格式。
_IMAGE_MAGIC_PREFIXES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),  # 还需检查偏移 8 处的 WEBP
)


def _validate_image_magic_bytes(data: bytes) -> str:
    """P1-9：严格验证下载内容的魔数，返回可信格式名。

    与 ``_detect_image_format`` 不同，此函数对未知内容抛出异常，
    不返回默认值。SVG 需要特殊处理（XML 文本）。
    """
    if not data:
        raise ImageDownloadError("下载内容为空")

    # PNG / JPEG / GIF / WebP 的二进制魔数
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"

    # SVG：XML 文本（可能是 <?xml 或直接 <svg）
    stripped = data[:512].lstrip()
    if stripped.startswith(b"<?xml") or b"<svg" in stripped.lower():
        return "svg+xml"

    raise ImageDownloadError("下载内容无法识别为有效图片格式")


def _validate_download_url(url: str) -> None:
    """P1-9：校验下载 URL 的 scheme 和 host。

    - 必须 HTTPS
    - host 必须在可信后缀列表内
    - 拒绝内网地址（127.0.0.1、10.x、192.168.x、169.254.x 等）由 host 白名单隐式覆盖
    """
    if not url:
        raise ImageDownloadError("下载 URL 为空")

    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ImageDownloadError("下载 URL 必须使用 HTTPS")

    host = parsed.hostname or ""
    if not host:
        raise ImageDownloadError("下载 URL 缺少 host")

    # 拒绝 IP 字面量（避免 https://127.0.0.1 等内网地址）
    if host.isdigit() or ":" in host or host.startswith("["):
        raise ImageDownloadError("下载 URL 不得使用 IP 字面量")

    host_lower = host.lower()
    if not any(
        host_lower == suffix.lstrip(".") or host_lower.endswith(suffix)
        for suffix in _ALLOWED_DOWNLOAD_HOST_SUFFIXES
    ):
        raise ImageDownloadError("下载 URL host 不在可信列表内")


def _safe_download_image(url: str, *, timeout: int = 60) -> bytes:
    """P1-9：安全下载图片，应用信任边界。

    - 校验 URL scheme 和 host
    - 禁止自动重定向，手动验证重定向目标也在可信范围内
    - 流式读取，限制最大字节数
    - 验证 Content-Type 是允许的图片 MIME
    - 用魔数（_detect_image_format）二次验证真实格式

    Args:
        url: 待下载的图片 URL。
        timeout: 下载超时秒数。

    Returns:
        校验通过的图片二进制数据。

    Raises:
        ImageDownloadError: 任何校验失败。
    """
    _validate_download_url(url)

    # P1-3：所有 HTTP 状态/MIME/重定向/超限分支都必须关闭 response，
    # 避免连接泄漏。旧实现只在流式读取完成后 close，HTTP 错误和 MIME
    # 拒绝路径直接 raise，response 句柄丢失到 GC。
    response = None
    try:
        # 禁止自动重定向，手动验证 Location
        response = requests.get(
            url,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            # 递归校验重定向目标（限制深度由 requests 重试机制兜底）
            _validate_download_url(location)
            # 校验通过后跟随重定向（不再允许二次重定向）
            # P1-3：先关闭旧 response，再发起新请求
            response.close()
            response = requests.get(
                location,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )

        if response.status_code != 200:
            raise ImageDownloadError(f"下载失败: HTTP {response.status_code}")

        # Content-Type 校验
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and content_type not in _ALLOWED_DOWNLOAD_CONTENT_TYPES:
            raise ImageDownloadError(f"下载 Content-Type 不允许: {content_type}")

        # 流式读取，限制最大字节数
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            if total > _DOWNLOAD_MAX_BYTES:
                raise ImageDownloadError(f"下载超出最大字节数限制 ({_DOWNLOAD_MAX_BYTES} bytes)")
            chunks.append(chunk)

        data = b"".join(chunks)
    finally:
        # P1-3：所有分支统一在 finally 中关闭 response
        if response is not None:
            response.close()

    # P1-9：严格魔数验证（不盲信 Content-Type，不依赖默认返回值）
    image_format = _validate_image_magic_bytes(data)
    if image_format == "svg+xml":
        data = _rasterize_safe_svg(data)
        _validate_image_magic_bytes(data)

    return data


def _detect_image_format(data: bytes) -> str:
    """根据魔术字节检测图片真实格式。

    R2-BUG-016：不能盲信原图扩展名，必须根据下载内容的真实格式
    确定扩展名和 MIME。SVG 输入可能返回位图字节，反之亦然。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"\x89PNG" or data[:4] == b"PNG\r":
        return "png"
    # SVG 检测（XML 或 SVG 标签）
    stripped = data[:512].lstrip()
    if stripped.startswith(b"<?xml") or b"<svg" in stripped.lower():
        return "svg+xml"
    # WebP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    # 默认 PNG（最安全的通用格式）
    return "png"


_FORMAT_TO_EXT = {
    "png": ".png",
    "jpeg": ".jpg",
    "gif": ".gif",
    "svg+xml": ".svg",
    "webp": ".webp",
}

_FORMAT_TO_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg+xml": "image/svg+xml",
    "webp": "image/webp",
}


class ImageTranslator:
    DEFAULT_BASE_URL = VOLCENGINE_IMAGE_DEFAULT_BASE_URL
    # 常用模型保留为设置页候选项，用户也可以填写其他模型 ID。
    MODEL_HIGH_QUALITY = VOLCENGINE_IMAGE_MODEL_HIGH_QUALITY
    MODEL_ECONOMY = VOLCENGINE_IMAGE_MODEL_ECONOMY
    DEFAULT_MODEL = VOLCENGINE_IMAGE_DEFAULT_MODEL

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.max_retries = 3

        # 火山引擎配置
        self._configuration_error = ""
        self.volc_base_url, self.volc_model = self._load_api_config()

        # PERF-005：复用火山方舟官方 SDK 客户端，仅在 API Key 变化时重建
        self._client: Ark | None = None
        self._client_api_key: str | None = None
        self._last_error = ""

    def _get_api_key(self) -> str:
        return self.config_manager.get_volc_key()

    def _load_api_config(self) -> tuple[str, str]:
        """读取用户配置的火山方舟 API 地址和图片翻译模型。"""
        try:
            app_config = self.config_manager.get_app_config()
            if not isinstance(app_config, dict):
                raise TypeError("应用配置必须是字典")
            image_config = app_config.get("image_translation", {})
            if not isinstance(image_config, dict):
                raise TypeError("图片翻译配置必须是字典")
            config = image_config.get("ai_volcengine", {})
            if not isinstance(config, dict):
                raise TypeError("火山引擎配置必须是字典")
            base_url = str(config.get("base_url", "")).strip()
            model = str(config.get("model", "")).strip()
            base_url = base_url or self.DEFAULT_BASE_URL
            try:
                base_url = normalize_openai_base_url(base_url)
            except ValueError as exc:
                self._configuration_error = f"图片翻译 API 地址无效: {exc}"
                return "", model or self.DEFAULT_MODEL
            return (
                base_url,
                model or self.DEFAULT_MODEL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取图片翻译配置失败，使用默认配置: %s", exc)
            return self.DEFAULT_BASE_URL, self.DEFAULT_MODEL

    def _get_client(self) -> Ark | None:
        """获取复用的火山方舟官方 SDK 客户端。"""
        if self._configuration_error:
            raise ValueError(self._configuration_error)
        api_key = self._get_api_key()
        if not api_key:
            return None
        if self._client is None or self._client_api_key != api_key:
            # 关闭旧客户端
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
            http_client = httpx.Client(
                timeout=_IMAGE_API_TIMEOUT,
                limits=_IMAGE_API_LIMITS,
                http2=False,
                trust_env=True,
            )
            try:
                self._client = Ark(
                    base_url=self.volc_base_url,
                    api_key=api_key,
                    http_client=http_client,
                    timeout=_IMAGE_API_TIMEOUT,
                    # 由本类统一重试，避免 SDK 重试与外层重试叠加。
                    max_retries=0,
                )
            except Exception:
                http_client.close()
                raise
            self._client_api_key = api_key
        return self._client

    @property
    def last_error(self) -> str:
        return self._last_error

    def _discard_client(self, client: Ark | None = None) -> None:
        if client is not None and client is not self._client:
            return
        current = self._client
        self._client = None
        self._client_api_key = None
        if current is not None:
            try:
                current.close()
            except Exception:
                pass

    @staticmethod
    def _connection_error_message(exc: Exception) -> str:
        cause_text = str(exc.__cause__ or exc)
        if "10053" in cause_text:
            return (
                "与火山方舟的连接被本机网络软件中止（WinError 10053）。"
                "已重建连接，请检查防火墙、杀毒软件或网络代理。"
            )
        if isinstance(exc, ArkAPITimeoutError):
            return "火山方舟图片生成响应超时，已重建连接后重试。"
        return "无法稳定连接火山方舟服务，已重建连接后重试。"

    def close(self):
        """PERF-005：关闭客户端，释放连接池。"""
        self._discard_client()

    def test_connection(self) -> bool:
        """测试火山引擎连接"""
        self._last_error = ""
        client = self._get_client()
        if client is None:
            return False

        try:
            dummy_uri, _ = _build_image_data_uri(_CONNECTION_TEST_PNG_BASE64, "image/png")

            images_response = client.images.generate(
                model=self.volc_model,
                prompt="Test connection",
                size=_IMAGE_GENERATION_SIZE,
                response_format="url",
                image=dummy_uri,
                watermark=True,
            )
            return bool(images_response and images_response.data)
        except ArkAuthenticationError:
            self._last_error = "火山方舟 API Key 无效或格式错误，请重新保存后再试。"
            return False
        except (ArkAPIConnectionError, ArkAPITimeoutError) as e:
            self._last_error = self._connection_error_message(e)
            self._discard_client(client)
            return False
        except Exception as e:
            self._last_error = f"火山方舟连接测试失败: {type(e).__name__}"
            print(f"Connection test exception: {e}")
            return False

    def translate_images(
        self,
        mapping_dir: str,
        target_lang: str,
        progress_callback: Callable[[int, int, str], None] | None = None,
        image_mappings_override: Dict[str, Dict[str, Any]] | None = None,
        image_translations: Dict[str, Dict[str, Any]] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Dict[str, str]:
        """
        并发翻译图片 - 使用用户指定的代码结构
        :param mapping_dir: 映射目录路径
        :param target_lang: 目标语言
        :param progress_callback: 进度回调 (success_count, total, current_file)
        :param image_mappings_override: 可选，直接传入要翻译的图片映射，跳过读取images.json
        :param cancel_event: P1-8：取消令牌，set() 后在下一个检查点抛出
            ``ImageTranslationCancelled``，不再发起新 API 请求或写新文件。
        :return: 映射字典 {原图相对路径: 新图相对路径}
        """
        self._last_error = ""
        client = self._get_client()
        if client is None:
            print("No 火山引擎 API Key found.")
            return {}

        mapping_path = Path(mapping_dir)

        image_mappings: Dict[str, Dict[str, Any]]
        if image_mappings_override is not None:
            image_mappings = image_mappings_override
        else:
            images_json_path = resolve_mapping_file(mapping_path, "images.json")
            if not images_json_path.exists():
                print("No images.json found.")
                return {}

            try:
                with open(images_json_path, encoding="utf-8") as f:
                    images_data = json.load(f)
            except Exception as e:
                print(f"Failed to load images.json: {e}")
                return {}

            raw_mappings = images_data.get("image_mappings", {})
            if not isinstance(raw_mappings, dict):
                logger.warning("images.json 中的 image_mappings 不是对象")
                return {}
            image_mappings = {
                str(name): info for name, info in raw_mappings.items() if isinstance(info, dict)
            }

        if not image_mappings:
            return {}

        output_dir = mapping_path / "images"
        output_dir.mkdir(exist_ok=True)
        logger.debug("[translate_images] 输出目录: %s", output_dir)

        result_mapping = {}
        total_images = len(image_mappings)
        success_count = 0
        processed_count = 0

        logger.debug("[translate_images] model=%s", self.volc_model)

        for idx, (name, info) in enumerate(image_mappings.items()):
            # P1-8：每张图片开始前检查取消令牌
            if cancel_event is not None and cancel_event.is_set():
                logger.info(
                    "[translate_images] 取消令牌已触发，停止处理（已处理 %d/%d）", idx, total_images
                )
                raise ImageTranslationCancelled(f"图片翻译在第 {idx}/{total_images} 张时被取消")

            logger.debug(
                "\n[translate_images] ====== 处理第 %d/%d 张图片: %s ======",
                idx + 1,
                total_images,
                name,
            )
            original_path = info.get("original_path")
            # PERF-6d：内部管道统一传 bytes，避免 load→encode→decode→convert→encode
            # 的冗余 base64 编解码循环。只在 HTTP JSON 边界编码一次。
            raw_bytes = load_image_bytes(mapping_path, info)
            logger.debug(
                "[translate_images] 图片信息: original_path=%s, raw_bytes=%d",
                original_path,
                len(raw_bytes) if raw_bytes else 0,
            )

            if not original_path or not raw_bytes:
                logger.warning(
                    "[translate_images] 缺少必要信息，跳过: original_path=%s, raw_bytes=%s",
                    bool(original_path),
                    bool(raw_bytes),
                )
                continue

            mime_type = info.get("mime_type", "image/png")
            from .image_utils import canonicalize_for_ark_image_generation

            logger.debug("[translate_images] 开始格式转换，mime_type=%s", mime_type)
            converted_bytes, converted_mime = canonicalize_for_ark_image_generation(
                raw_bytes, mime_type
            )
            if converted_bytes is None:
                logger.warning("[translate_images] 格式转换失败，跳过: %s (%s)", name, mime_type)
                print(f"[插图翻译] 跳过不支持的图片格式: {name} ({mime_type})")
                continue
            # PERF-6d：只在进入 HTTP JSON 边界前编码一次 base64
            b64_str = base64.b64encode(converted_bytes).decode("ascii")
            logger.debug(
                "[translate_images] 格式转换成功，mime_type=%s, base64长度=%d",
                converted_mime,
                len(b64_str),
            )

            processed_count += 1
            if progress_callback:
                progress_callback(success_count, total_images, name)

            logger.debug("[translate_images] 调用_process_single_image处理图片...")
            # 获取该图片的文字翻译结果（如果有的话）
            image_translation = None
            if image_translations and name in image_translations:
                image_translation = image_translations[name]
                logger.debug("[translate_images] 找到文字翻译结果: %s", image_translation)

            res = self._process_single_image(
                client,
                name,
                b64_str,
                target_lang,
                output_dir,
                image_translation,
                mime_type=converted_mime,
                original_path=original_path,
                cancel_event=cancel_event,
            )

            if res:
                orig_name, new_filename = res
                result_mapping[orig_name] = new_filename
                success_count += 1
                logger.debug("[translate_images] 处理成功: %s -> %s", orig_name, new_filename)
            else:
                logger.warning("[translate_images] 处理失败，无结果返回: %s", name)

        if progress_callback:
            progress_callback(success_count, total_images, "完成")

        logger.debug("\n[translate_images] ====== 插图翻译统计 ======")
        logger.debug("[translate_images] 总计: %d 张", total_images)
        logger.debug("[translate_images] 处理: %d 张", processed_count)
        logger.debug("[translate_images] 成功: %d 张", success_count)

        return result_mapping

    def _process_single_image(
        self,
        client: Ark,
        original_name: str,
        b64_str: str,
        target_lang: str,
        output_dir: Path,
        image_translation: Dict[str, Any] | None = None,
        mime_type: str = "image/png",
        original_path: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str] | None:
        """处理单张图片：上传 -> 生成 -> 下载 -> 保存

        Args:
            image_translation: 可选，文字翻译结果，格式: {"original_text": ..., "translated_text": ...}
            original_path: 原图在 EPUB 中的完整路径，用于生成唯一文件名（R2-BUG-016）
            cancel_event: P1-8：取消令牌，在 API 调用、下载、重试等待和落盘前检查。
                触发后抛出 ``ImageTranslationCancelled``，不再写新文件。
        """
        logger.debug("[_process_single_image] 开始处理: %s", original_name)
        logger.debug("[_process_single_image] image_translation: %s", image_translation)

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
            logger.debug("[_process_single_image] 使用包含参考翻译的prompt")
        else:
            prompt = base_prompt
            logger.debug("[_process_single_image] 使用基础prompt: %s", prompt)

        # 方舟图生图接口要求 image 为完整 data URI。官方 SDK 原生声明了
        # image 参数；在进入 SDK 前统一规范化 Base64 和 MIME。
        try:
            data_uri, normalized_mime = _build_image_data_uri(b64_str, mime_type)
        except ValueError as exc:
            self._last_error = f"输入图片无效: {exc}"
            logger.error("[_process_single_image] %s: %s", self._last_error, original_name)
            return None
        b64_str = data_uri.split(",", 1)[1]
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[_process_single_image] 构造的 data URI: %s...", data_uri[:80])

        for attempt in range(self.max_retries):
            # P1-8：每次重试前检查取消令牌
            if cancel_event is not None and cancel_event.is_set():
                logger.info("[_process_single_image] 取消令牌已触发，停止重试: %s", original_name)
                raise ImageTranslationCancelled(f"图片翻译在处理 {original_name} 时被取消")

            logger.debug(
                "[_process_single_image] 尝试第 %d/%d 次...", attempt + 1, self.max_retries
            )
            try:
                logger.debug("[_process_single_image] 调用火山引擎API生成图片...")
                images_response = client.images.generate(
                    model=self.volc_model,
                    prompt=prompt,
                    size=_IMAGE_GENERATION_SIZE,
                    response_format="url",
                    image=data_uri,
                    watermark=True,
                )
                logger.debug("[_process_single_image] API响应类型: %s", type(images_response))

                if not images_response or not images_response.data:
                    logger.error("[_process_single_image] 响应中没有数据: %s", original_name)
                    print(f"No data in response for {original_name}")
                    return None

                image_url = images_response.data[0].url
                logger.debug("[_process_single_image] 获取图片URL: %s...", image_url[:80])

                logger.debug("[_process_single_image] 开始下载图片...")
                # P1-8：下载前检查取消令牌
                if cancel_event is not None and cancel_event.is_set():
                    raise ImageTranslationCancelled(f"图片翻译在下载 {original_name} 前被取消")
                # P1-9：使用信任边界下载，拒绝非 HTTPS、非可信 host、
                # 重定向到内网、超大文件、错误 MIME 和无效魔数
                new_img_data = _safe_download_image(image_url)
                logger.debug(
                    "[_process_single_image] 下载完成，图片大小: %d bytes", len(new_img_data)
                )

                orig_size = len(base64.b64decode(b64_str))
                logger.debug(
                    "[_process_single_image] 原图大小: %d bytes, 新图大小: %d bytes",
                    orig_size,
                    len(new_img_data),
                )
                if len(new_img_data) < orig_size * 0.9:
                    logger.warning(
                        "[_process_single_image] 生成图片太小: %s: %d < %s",
                        original_name,
                        len(new_img_data),
                        orig_size * 0.9,
                    )
                    print(
                        f"Generated image too small for {original_name}: {len(new_img_data)} < {orig_size * 0.9}"
                    )
                    return None

                # R2-BUG-016：文件名包含原图完整路径哈希，避免同名图片互相覆盖
                # 同时根据下载内容的真实格式确定扩展名，不盲信原图扩展名
                p = Path(original_name)
                path_for_hash = original_path or original_name
                path_hash = hashlib.sha256(path_for_hash.encode("utf-8")).hexdigest()[:8]
                real_format = _detect_image_format(new_img_data)
                real_ext = _FORMAT_TO_EXT.get(real_format, ".png")
                new_filename = f"{p.stem}_{path_hash}_translated{real_ext}"
                save_path = output_dir / new_filename
                logger.debug(
                    "[_process_single_image] 保存路径: %s, 真实格式: %s", save_path, real_format
                )

                # P1-8：落盘前检查取消令牌（取消后不写新文件）
                if cancel_event is not None and cancel_event.is_set():
                    raise ImageTranslationCancelled(f"图片翻译在保存 {original_name} 前被取消")

                # P1-9：原子落盘，避免半写入文件被后续读取
                from ..infrastructure.atomic_file import write_bytes_atomic

                write_bytes_atomic(save_path, new_img_data)
                logger.debug("[_process_single_image] 图片保存成功")

                self._last_error = ""
                return original_name, new_filename

            except ImageTranslationCancelled:
                # P1-8：取消异常不重试，直接向上传递
                raise
            except ArkBadRequestError as exc:
                # 参数错误（尤其是图片编码/尺寸）不会因重试而恢复。
                self._last_error = f"火山方舟图片参数错误: {str(exc)[:240]}"
                logger.error("[_process_single_image] %s", self._last_error)
                return None
            except ArkAuthenticationError:
                self._last_error = "火山方舟 API Key 无效或格式错误，请在设置中重新保存。"
                logger.error("[_process_single_image] 火山方舟鉴权失败")
                return None
            except (ArkAPIConnectionError, ArkAPITimeoutError) as e:
                self._last_error = self._connection_error_message(e)
                logger.error(
                    "[_process_single_image] 连接异常 (尝试 %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    self._last_error,
                    exc_info=True,
                )
                if attempt == self.max_retries - 1:
                    return None
                self._discard_client(client)
                replacement = self._get_client()
                if replacement is not None:
                    client = replacement
            except Exception as e:
                logger.error(
                    "[_process_single_image] 异常 (尝试 %d/%d): %s: %s",
                    attempt + 1,
                    self.max_retries,
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
                if attempt == self.max_retries - 1:
                    self._last_error = f"火山方舟图片生成失败: {type(e).__name__}: {str(e)[:160]}"
                    print(
                        f"Failed to process {original_name} after {self.max_retries} attempts: {e}"
                    )
                    return None

                wait_time = 2**attempt
                logger.debug("[_process_single_image] 等待 %d 秒后重试...", wait_time)
                # P1-8：重试等待期间检查取消令牌，取消时立即唤醒不继续重试
                if cancel_event is not None:
                    if cancel_event.wait(timeout=wait_time):
                        logger.info("[_process_single_image] 重试等待期间被取消: %s", original_name)
                        raise ImageTranslationCancelled(
                            f"图片翻译在重试等待 {original_name} 时被取消"
                        )
                else:
                    time.sleep(wait_time)

        logger.error("[_process_single_image] 所有重试失败")
        return None
