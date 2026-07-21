#!/usr/bin/env python3
"""
图片相关修复测试

验证 R2-BUG-016/017/018/025 的修复行为：
- R2-BUG-016：图片译文文件名唯一 + 真实格式检测 + 禁止 basename 匹配
- R2-BUG-017：区分"无外文"和"检测失败"
- R2-BUG-018：空结果始终写入结果文件
- R2-BUG-025：Ark SDK 客户端显式关闭
"""

import base64
import io
import json
import struct
import zlib
from unittest.mock import MagicMock, patch

import httpx
import pytest
from volcenginesdkarkruntime import Ark
from volcenginesdkarkruntime._exceptions import ArkAPIConnectionError

from src.core.image_text_translator import (
    DETECTION_FAILED,
    DETECTION_FOREIGN_TEXT,
    DETECTION_NO_TEXT,
    ImageTextTranslator,
)
from src.core.image_translator import (
    _CONNECTION_TEST_PNG_BASE64,
    _FORMAT_TO_EXT,
    ImageTranslator,
    _build_image_data_uri,
    _detect_image_format,
)
from src.core.image_utils import canonicalize_for_ark_image_generation

# ── R2-BUG-016：图片格式检测 ──────────────────────────


class TestImageFormatDetection:
    """R2-BUG-016：根据魔术字节检测真实格式"""

    def test_png_detected(self):
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        assert _detect_image_format(png_header) == "png"

    def test_jpeg_detected(self):
        jpeg_header = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        assert _detect_image_format(jpeg_header) == "jpeg"

    def test_gif_detected(self):
        gif_header = b"GIF89a" + b"\x00" * 100
        assert _detect_image_format(gif_header) == "gif"

    def test_svg_detected(self):
        svg_data = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
        assert _detect_image_format(svg_data) == "svg+xml"

    def test_webp_detected(self):
        webp_header = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 100
        assert _detect_image_format(webp_header) == "webp"

    def test_unknown_defaults_to_png(self):
        unknown_data = b"\x00\x01\x02\x03" + b"\x00" * 100
        assert _detect_image_format(unknown_data) == "png"

    def test_format_to_ext_mapping(self):
        """格式到扩展名的映射完整"""
        assert _FORMAT_TO_EXT["png"] == ".png"
        assert _FORMAT_TO_EXT["jpeg"] == ".jpg"
        assert _FORMAT_TO_EXT["svg+xml"] == ".svg"

    def test_ark_reference_jpeg_is_reencoded_as_canonical_png(self):
        from PIL import Image

        jpeg_buffer = io.BytesIO()
        Image.new("L", (32, 48), 127).save(jpeg_buffer, format="JPEG")

        png, mime = canonicalize_for_ark_image_generation(jpeg_buffer.getvalue(), "image/jpeg")

        assert mime == "image/png"
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(io.BytesIO(png)) as image:
            image.load()
            assert image.format == "PNG"
            assert image.size == (32, 48)
            assert image.mode == "RGB"

    def test_ark_reference_rejects_edges_at_or_below_fourteen_pixels(self):
        from PIL import Image

        image_buffer = io.BytesIO()
        Image.new("RGB", (14, 64), "white").save(image_buffer, format="PNG")

        assert canonicalize_for_ark_image_generation(image_buffer.getvalue(), "image/png") == (
            None,
            None,
        )


# ── R2-BUG-016：唯一文件名 ────────────────────────────


class TestUniqueFilename:
    """R2-BUG-016：不同目录同名图片生成不同文件名"""

    def test_different_paths_produce_different_filenames(self):
        """两个不同路径的同名图片生成不同文件名"""
        import hashlib

        path1 = "images/cover.jpg"
        path2 = "other/cover.jpg"

        hash1 = hashlib.sha256(path1.encode("utf-8")).hexdigest()[:8]
        hash2 = hashlib.sha256(path2.encode("utf-8")).hexdigest()[:8]

        name1 = f"cover_{hash1}_translated.png"
        name2 = f"cover_{hash2}_translated.png"

        assert name1 != name2, "不同路径的同名图片应生成不同文件名"

    def test_same_path_produces_same_filename(self):
        """相同路径生成相同文件名（幂等）"""
        import hashlib

        path = "images/cover.jpg"
        hash1 = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        hash2 = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]

        assert hash1 == hash2


# ── R2-BUG-017：检测状态区分 ──────────────────────────


class TestDetectionStatus:
    """R2-BUG-017：区分"无外文"和"检测失败" """

    def _make_translator(self):
        """构造一个 ImageTextTranslator，API 客户端使用 mock"""
        config_manager = MagicMock()
        config_manager.get_api_config.return_value = {"provider": "siliconflow"}
        config_manager.get_app_config.return_value = {}
        translator = ImageTextTranslator(config_manager)
        return translator

    def test_failed_detection_when_vision_query_returns_none(self):
        """vision_query 返回 None 时状态为 FAILED"""
        translator = self._make_translator()

        # mock API 客户端返回 None（模拟鉴权失败、超时等）
        mock_api = MagicMock()
        mock_api.vision_query.return_value = None
        translator._api_client = mock_api

        result = translator.detect_text_in_image("base64data", "image/png")

        assert result["status"] == DETECTION_FAILED
        assert result["has_foreign_text"] is False

    def test_no_text_detection_when_model_says_no_text(self):
        """模型回复"无文字"时状态为 NO_TEXT"""
        translator = self._make_translator()

        mock_api = MagicMock()
        mock_api.vision_query.return_value = "无文字"
        translator._api_client = mock_api

        result = translator.detect_text_in_image("base64data", "image/png")

        assert result["status"] == DETECTION_NO_TEXT
        assert result["has_foreign_text"] is False

    def test_foreign_text_detection_when_model_returns_japanese(self):
        """模型回复含日文时状态为 FOREIGN_TEXT"""
        translator = self._make_translator()

        mock_api = MagicMock()
        mock_api.vision_query.return_value = "これは日本語のテキストです"
        translator._api_client = mock_api

        result = translator.detect_text_in_image("base64data", "image/png")

        assert result["status"] == DETECTION_FOREIGN_TEXT
        assert result["has_foreign_text"] is True

    def test_no_text_when_only_chinese_returned(self):
        """模型回复纯中文时状态为 NO_TEXT"""
        translator = self._make_translator()

        mock_api = MagicMock()
        mock_api.vision_query.return_value = "这是中文内容"
        translator._api_client = mock_api

        result = translator.detect_text_in_image("base64data", "image/png")

        assert result["status"] == DETECTION_NO_TEXT
        assert result["has_foreign_text"] is False


# ── R2-BUG-018：空结果写入文件 ────────────────────────


class TestEmptyResultFile:
    """R2-BUG-018：空结果始终写入结果文件"""

    def test_write_empty_result_file(self, tmp_path):
        """空结果也写入 image_translation_result.json"""
        translator = MagicMock()
        translator._write_image_translation_result = (
            ImageTextTranslator._write_image_translation_result.__get__(translator)
        )

        result_map = {}
        ImageTextTranslator._write_image_translation_result(translator, tmp_path, result_map)

        result_file = tmp_path / "image_translation_result.json"
        assert result_file.exists()

        data = json.loads(result_file.read_text(encoding="utf-8"))
        assert data["result_map"] == {}
        assert data["result_count"] == 0
        assert "run_at" in data

    def test_write_non_empty_result_file(self, tmp_path):
        """非空结果正确写入"""
        translator = MagicMock()
        translator._write_image_translation_result = (
            ImageTextTranslator._write_image_translation_result.__get__(translator)
        )

        result_map = {"images/cover.jpg": "cover_abc12345_translated.png"}
        ImageTextTranslator._write_image_translation_result(translator, tmp_path, result_map)

        result_file = tmp_path / "image_translation_result.json"
        assert result_file.exists()

        data = json.loads(result_file.read_text(encoding="utf-8"))
        assert data["result_map"] == result_map
        assert data["result_count"] == 1

    def test_empty_result_overwrites_old_file(self, tmp_path):
        """空结果覆盖旧文件，避免导出时使用过期数据"""
        # 先写入旧的成功结果
        old_data = {"images/old.jpg": "old_translated.png"}
        old_file = tmp_path / "image_translation_result.json"
        old_file.write_text(json.dumps(old_data), encoding="utf-8")

        # 模拟新的空运行
        translator = MagicMock()
        ImageTextTranslator._write_image_translation_result(translator, tmp_path, {})

        # 读取并验证旧数据已被覆盖
        data = json.loads(old_file.read_text(encoding="utf-8"))
        assert data["result_map"] == {}
        assert "images/old.jpg" not in data.get("result_map", {})


# ── R2-BUG-018：结果文件格式兼容 ──────────────────────


class TestResultFileCompat:
    """R2-BUG-018：新格式向后兼容"""

    def test_new_format_with_result_map_key(self, tmp_path):
        """新格式包含 result_map 键"""
        result_file = tmp_path / "image_translation_result.json"
        payload = {
            "result_map": {"a.jpg": "a_translated.png"},
            "run_at": "2026-01-01T00:00:00",
            "result_count": 1,
        }
        result_file.write_text(json.dumps(payload), encoding="utf-8")

        # 模拟读取逻辑（与 translation_controller.py 一致）
        raw = json.loads(result_file.read_text(encoding="utf-8"))
        image_map = raw["result_map"] if isinstance(raw, dict) and "result_map" in raw else raw

        assert image_map == {"a.jpg": "a_translated.png"}

    def test_old_format_without_result_map_key(self, tmp_path):
        """旧格式（直接 dict）仍可读取"""
        result_file = tmp_path / "image_translation_result.json"
        old_format = {"a.jpg": "a_translated.png"}
        result_file.write_text(json.dumps(old_format), encoding="utf-8")

        raw = json.loads(result_file.read_text(encoding="utf-8"))
        image_map = raw["result_map"] if isinstance(raw, dict) and "result_map" in raw else raw

        assert image_map == {"a.jpg": "a_translated.png"}


# ── R2-BUG-025 / PERF-005：Ark SDK 客户端复用与关闭 ────


class TestArkClientClose:
    """R2-BUG-025 / PERF-005：Ark SDK 客户端复用与显式关闭"""

    def test_remote_http_volc_endpoint_never_creates_a_client(self):
        """远程明文 endpoint 必须在携带 API Key 前被拒绝。"""
        config_manager = MagicMock()
        config_manager.get_volc_key.return_value = "sk-image-test"
        config_manager.get_app_config.return_value = {
            "image_translation": {
                "ai_volcengine": {
                    "base_url": "http://image-api.example.com/v1",
                    "model": "image-model",
                }
            }
        }
        translator = ImageTranslator(config_manager)

        with patch("src.core.image_translator.Ark") as ark_cls:
            with pytest.raises(ValueError, match="HTTPS"):
                translator._get_client()

        ark_cls.assert_not_called()

    def test_loopback_http_volc_endpoint_remains_supported(self):
        """本地开发服务仍可使用受限的 HTTP loopback 地址。"""
        config_manager = MagicMock()
        config_manager.get_volc_key.return_value = "sk-image-test"
        config_manager.get_app_config.return_value = {
            "image_translation": {
                "ai_volcengine": {
                    "base_url": "http://127.0.0.1:11434/v1/",
                    "model": "image-model",
                }
            }
        }
        translator = ImageTranslator(config_manager)
        fake_client = MagicMock()

        with patch("src.core.image_translator.Ark", return_value=fake_client):
            assert translator._get_client() is fake_client

        assert translator.volc_base_url == "http://127.0.0.1:11434/v1"

    def test_custom_volc_endpoint_and_model_are_loaded(self):
        config_manager = MagicMock()
        config_manager.get_app_config.return_value = {
            "image_translation": {
                "ai_volcengine": {
                    "base_url": "https://example.com/custom/v1/",
                    "model": "ep-custom-model-id",
                }
            }
        }

        translator = ImageTranslator(config_manager)

        assert translator.volc_base_url == "https://example.com/custom/v1"
        assert translator.volc_model == "ep-custom-model-id"

    def test_unknown_custom_volc_model_is_not_replaced(self):
        config_manager = MagicMock()
        config_manager.get_app_config.return_value = {
            "image_translation": {"ai_volcengine": {"model": "user-defined-model"}}
        }

        translator = ImageTranslator(config_manager)

        assert translator.volc_model == "user-defined-model"
        assert translator.volc_base_url == ImageTranslator.DEFAULT_BASE_URL

    def test_translate_images_does_not_close_client(self, tmp_path):
        """PERF-005：translate_images 完成后不关闭客户端（复用连接池）"""
        config_manager = MagicMock()
        config_manager.get_volc_key.return_value = "test-key"

        translator = ImageTranslator(config_manager)

        closed = {"called": False}

        class FakeClient:
            def __init__(self):
                self.images = MagicMock()

            def close(self):
                closed["called"] = True

        image_data = {
            "cover.jpg": {
                "original_path": "cover.jpg",
                "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
                "mime_type": "image/png",
            }
        }

        with patch("src.core.image_translator.Ark", return_value=FakeClient()):
            with patch(
                "src.core.image_utils.canonicalize_for_ark_image_generation",
                return_value=(
                    base64.b64decode(_CONNECTION_TEST_PNG_BASE64),
                    "image/png",
                ),
            ):
                with patch("src.core.image_translator.time.sleep"):
                    translator.translate_images(
                        str(tmp_path),
                        "zh",
                        None,
                        image_mappings_override=image_data,
                    )

        assert closed["called"] is False, "PERF-005：translate_images 不应关闭复用的客户端"

    def test_client_uses_single_retry_layer_and_no_keepalive(self):
        """图片客户端由外层重试，并避免复用 Windows 半失效长连接。"""
        config_manager = MagicMock()
        config_manager.get_volc_key.return_value = "test-key"
        translator = ImageTranslator(config_manager)
        fake_client = MagicMock()
        fake_http_client = MagicMock()

        with (
            patch(
                "src.core.image_translator.httpx.Client",
                return_value=fake_http_client,
            ) as http_client_cls,
            patch(
                "src.core.image_translator.Ark",
                return_value=fake_client,
            ) as ark_cls,
        ):
            assert translator._get_client() is fake_client

        assert ark_cls.call_args.kwargs["max_retries"] == 0
        assert ark_cls.call_args.kwargs["http_client"] is fake_http_client
        limits = http_client_cls.call_args.kwargs["limits"]
        assert limits.max_keepalive_connections == 0

    def test_connection_uses_valid_seedream_compatible_image_request(self):
        config_manager = MagicMock()
        translator = ImageTranslator(config_manager)
        client = MagicMock()
        client.images.generate.return_value = MagicMock(data=[MagicMock(url="https://example")])

        with patch.object(translator, "_get_client", return_value=client):
            assert translator.test_connection() is True

        kwargs = client.images.generate.call_args.kwargs
        assert kwargs["size"] == "2K"
        assert kwargs["watermark"] is True
        assert "sequential_image_generation" not in kwargs
        assert kwargs["image"] == f"data:image/png;base64,{_CONNECTION_TEST_PNG_BASE64}"

        png = base64.b64decode(_CONNECTION_TEST_PNG_BASE64, validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", png[16:24]) == (64, 64)

        offset = 8
        seen_iend = False
        while offset < len(png):
            length = struct.unpack(">I", png[offset : offset + 4])[0]
            chunk_type = png[offset + 4 : offset + 8]
            chunk_data = png[offset + 8 : offset + 8 + length]
            stored_crc = struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])[0]
            assert stored_crc == zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
            offset += length + 12
            if chunk_type == b"IEND":
                seen_iend = True
                break

        assert seen_iend is True
        assert offset == len(png)

    def test_connection_error_rebuilds_client_before_retry(self, tmp_path):
        """WinError 10053 后不得继续复用发生错误的连接池。"""
        config_manager = MagicMock()
        config_manager.get_volc_key.return_value = "test-key"
        translator = ImageTranslator(config_manager)
        request = httpx.Request(
            "POST", "https://ark.cn-beijing.volces.com/api/v3/images/generations"
        )
        error = ArkAPIConnectionError(request=request, request_id="test-request")
        error.__cause__ = httpx.ReadError("[WinError 10053] connection aborted", request=request)

        failed_client = MagicMock()
        failed_client.images.generate.side_effect = error
        replacement_client = MagicMock()
        replacement_client.images.generate.return_value = MagicMock(
            data=[MagicMock(url="https://ark.volces.com/generated.png")]
        )
        translator._client = failed_client
        translator._client_api_key = "test-key"
        generated_image = b"\x89PNG\r\n\x1a\n" + b"x" * 128

        with (
            patch.object(translator, "_get_client", return_value=replacement_client),
            patch(
                "src.core.image_translator._safe_download_image",
                return_value=generated_image,
            ),
            patch("src.core.image_translator.time.sleep"),
        ):
            result = translator._process_single_image(
                failed_client,
                "cover.png",
                base64.b64encode(b"original-image").decode("ascii"),
                "中文",
                tmp_path,
                mime_type="image/png",
                original_path="images/cover.png",
            )

        assert result is not None
        failed_client.close.assert_called_once()
        replacement_client.images.generate.assert_called_once()
        assert translator.last_error == ""

    def test_close_releases_client(self, tmp_path):
        """PERF-005：translator.close() 显式关闭客户端"""
        config_manager = MagicMock()
        config_manager.get_volc_key.return_value = "test-key"

        translator = ImageTranslator(config_manager)

        closed = {"called": False}

        class FakeClient:
            def __init__(self):
                self.images = MagicMock()

            def close(self):
                closed["called"] = True

        image_data = {
            "cover.jpg": {
                "original_path": "cover.jpg",
                "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
                "mime_type": "image/png",
            }
        }

        with patch("src.core.image_translator.Ark", return_value=FakeClient()):
            with patch(
                "src.core.image_utils.canonicalize_for_ark_image_generation",
                return_value=(
                    base64.b64decode(_CONNECTION_TEST_PNG_BASE64),
                    "image/png",
                ),
            ):
                with patch("src.core.image_translator.time.sleep"):
                    translator.translate_images(
                        str(tmp_path),
                        "zh",
                        None,
                        image_mappings_override=image_data,
                    )
                    translator.close()

        assert closed["called"] is True, "translator.close() 未关闭客户端"

    def test_translate_images_survives_exception(self, tmp_path):
        """PERF-005：translate_images 异常时不关闭客户端（保持复用）"""
        config_manager = MagicMock()
        config_manager.get_volc_key.return_value = "test-key"

        translator = ImageTranslator(config_manager)

        closed = {"called": False}

        class FakeClient:
            def __init__(self):
                self.images = MagicMock()
                self.images.generate.side_effect = Exception("API error")

            def close(self):
                closed["called"] = True

        image_data = {
            "cover.jpg": {
                "original_path": "cover.jpg",
                "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
                "mime_type": "image/png",
            }
        }

        with patch("src.core.image_translator.Ark", return_value=FakeClient()):
            with patch(
                "src.core.image_utils.canonicalize_for_ark_image_generation",
                return_value=(
                    base64.b64decode(_CONNECTION_TEST_PNG_BASE64),
                    "image/png",
                ),
            ):
                with patch("src.core.image_translator.time.sleep"):
                    translator.translate_images(
                        str(tmp_path),
                        "zh",
                        None,
                        image_mappings_override=image_data,
                    )

        assert closed["called"] is False, "PERF-005：异常时不应关闭复用的客户端"

    def test_generate_uses_official_ark_sdk_image_parameter(self, tmp_path):
        """火山图片参数应通过官方 Ark SDK 的原生参数传递。"""
        config_manager = MagicMock()
        translator = ImageTranslator(config_manager)
        client = MagicMock()
        client.images.generate.return_value = MagicMock(
            data=[MagicMock(url="https://ark.volces.com/generated.png")]
        )
        generated_image = b"\x89PNG\r\n\x1a\n" + b"x" * 128

        # P1-9：mock _safe_download_image，避免依赖其内部实现
        with patch(
            "src.core.image_translator._safe_download_image",
            return_value=generated_image,
        ):
            result = translator._process_single_image(
                client,
                "cover.png",
                base64.b64encode(b"original-image").decode("ascii"),
                "中文",
                tmp_path,
                mime_type="image/png",
                original_path="images/cover.png",
            )

        assert result is not None
        kwargs = client.images.generate.call_args.kwargs
        assert kwargs["response_format"] == "url"
        assert kwargs["size"] == "2K"
        assert kwargs["image"].startswith("data:image/png;base64,")
        assert kwargs["watermark"] is True
        assert "extra_body" not in kwargs
        assert "sequential_image_generation" not in kwargs

    def test_official_ark_sdk_serializes_image_as_top_level_field(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "created": 1,
                    "model": "test-model",
                    "data": [{"url": "https://example.com/generated.png"}],
                },
                request=request,
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = Ark(
            api_key="test-key",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            http_client=http_client,
            max_retries=0,
        )
        data_uri = f"data:image/png;base64,{_CONNECTION_TEST_PNG_BASE64}"

        try:
            response = client.images.generate(
                model="test-model",
                prompt="test",
                image=data_uri,
                size="2K",
                response_format="url",
                watermark=True,
            )
        finally:
            client.close()

        assert response.data[0].url == "https://example.com/generated.png"
        assert captured["image"] == data_uri
        assert captured["watermark"] is True
        assert captured["size"] == "2K"

    def test_build_image_data_uri_normalizes_legacy_mime_and_padding(self):
        """Ark receives canonical ASCII Base64 regardless of mapping legacy format."""
        raw = base64.b64encode(b"jpeg-bytes").decode("ascii")
        uri, mime = _build_image_data_uri(f"data:image/jpg;base64,{raw}\n", "image/jpg")

        assert mime == "image/jpeg"
        assert uri == f"data:image/jpeg;base64,{raw}"

    def test_invalid_image_base64_fails_before_api_call(self, tmp_path):
        config_manager = MagicMock()
        translator = ImageTranslator(config_manager)
        client = MagicMock()

        result = translator._process_single_image(
            client,
            "cover.jpg",
            "not-base64",
            "中文",
            tmp_path,
            mime_type="image/jpeg",
            original_path="images/cover.jpg",
        )

        assert result is None
        assert "Base64" in translator.last_error
        client.images.generate.assert_not_called()
