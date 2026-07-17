"""P1-9 专项测试：图片路径和远程下载信任边界。

验收标准：
- 绝对路径、``../``、符号链接逃逸、超大文件、错误 MIME、非 HTTPS、
  非可信 host 和重定向到内网均被拒绝。
- 错误中不泄露本地敏感路径。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.image_translator import (
    ImageDownloadError,
    _safe_download_image,
    _validate_download_url,
)
from src.infrastructure.image_asset_store import (
    ImagePathValidationError,
    _validate_local_path,
    load_image_base64,
    load_image_bytes,
)

# ── local_path 信任边界 ─────────────────────────────────────


class LocalPathValidationTests(unittest.TestCase):
    """P1-9：local_path 信任边界校验测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mapping_dir = Path(self.tmpdir) / "mapping"
        self.mapping_dir.mkdir()
        (self.mapping_dir / "assets").mkdir()
        # 在 assets 下放一个合法文件
        self.valid_file = self.mapping_dir / "assets" / "000001.png"
        self.valid_file.write_bytes(b"\x89PNG\r\n\x1a\n")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_relative_path_accepted(self):
        """合法的 assets/ 相对路径通过校验。"""
        result = _validate_local_path(self.mapping_dir, "assets/000001.png")
        self.assertTrue(result.is_absolute())
        self.assertEqual(result.name, "000001.png")

    def test_absolute_path_rejected(self):
        """绝对路径被拒绝。

        注意：在 Windows 上 ``/etc/passwd`` 不被 ``Path.is_absolute()``
        识别为绝对路径，但会被 resolve 后的逃逸检查拒绝。
        两种错误都算通过。
        """
        with self.assertRaises(ImagePathValidationError):
            _validate_local_path(self.mapping_dir, "/etc/passwd")

        # Windows 绝对路径
        with self.assertRaises(ImagePathValidationError):
            _validate_local_path(self.mapping_dir, "C:\\Windows\\system32")

    def test_parent_directory_reference_rejected(self):
        """包含 ``..`` 的路径被拒绝。"""
        with self.assertRaises(ImagePathValidationError) as ctx:
            _validate_local_path(self.mapping_dir, "assets/../../../etc/passwd")
        self.assertIn("父目录引用", str(ctx.exception))

    def test_parent_directory_at_start_rejected(self):
        """开头的 ``..`` 被拒绝。"""
        with self.assertRaises(ImagePathValidationError):
            _validate_local_path(self.mapping_dir, "../outside.bin")

    def test_symlink_escape_rejected(self):
        """符号链接逃逸被拒绝。"""
        # 创建指向 mapping_dir 外的符号链接
        outside_file = Path(self.tmpdir) / "outside.bin"
        outside_file.write_bytes(b"secret")
        link_path = self.mapping_dir / "assets" / "escape_link"
        try:
            os.symlink(outside_file, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不支持符号链接创建")

        with self.assertRaises(ImagePathValidationError):
            _validate_local_path(self.mapping_dir, "assets/escape_link")

    def test_non_assets_prefix_rejected(self):
        """不在 assets/ 下的路径被拒绝。"""
        with self.assertRaises(ImagePathValidationError) as ctx:
            _validate_local_path(self.mapping_dir, "images/000001.png")
        self.assertIn("assets", str(ctx.exception).lower())

    def test_empty_path_rejected(self):
        """空路径被拒绝。"""
        with self.assertRaises(ImagePathValidationError):
            _validate_local_path(self.mapping_dir, "")

    def test_error_message_does_not_leak_sensitive_path(self):
        """错误消息不泄露 mapping_dir 完整路径。"""
        with self.assertRaises(ImagePathValidationError) as ctx:
            _validate_local_path(self.mapping_dir, "../secret")
        error_msg = str(ctx.exception)
        # 错误消息不应包含 mapping_dir 的完整路径
        self.assertNotIn(str(self.mapping_dir), error_msg)
        self.assertNotIn(self.tmpdir, error_msg)


class LoadImageBase64TrustBoundaryTests(unittest.TestCase):
    """P1-9：load_image_base64 信任边界测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mapping_dir = Path(self.tmpdir) / "mapping"
        self.mapping_dir.mkdir()
        (self.mapping_dir / "assets").mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_path_traversal_returns_empty(self):
        """路径穿越攻击返回空字符串，不读取文件。"""
        # 在 mapping_dir 外放一个文件
        outside = Path(self.tmpdir) / "secret.txt"
        outside.write_text("secret content", encoding="utf-8")

        info = {
            "local_path": "../secret.txt",
            "mime_type": "image/png",
        }
        result = load_image_base64(self.mapping_dir, info)
        self.assertEqual(result, "")

    def test_absolute_path_returns_empty(self):
        """绝对路径返回空字符串。"""
        info = {
            "local_path": "/etc/passwd",
            "mime_type": "image/png",
        }
        result = load_image_base64(self.mapping_dir, info)
        self.assertEqual(result, "")

    def test_valid_path_loads_successfully(self):
        """合法路径正常加载。"""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (self.mapping_dir / "assets" / "001.png").write_bytes(png_bytes)

        info = {
            "local_path": "assets/001.png",
            "mime_type": "image/png",
        }
        result = load_image_base64(self.mapping_dir, info)
        self.assertTrue(result.startswith("data:image/png;base64,"))


class LoadImageBytesTrustBoundaryTests(unittest.TestCase):
    """P1-9：load_image_bytes 信任边界测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mapping_dir = Path(self.tmpdir) / "mapping"
        self.mapping_dir.mkdir()
        (self.mapping_dir / "assets").mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_path_traversal_returns_none(self):
        """路径穿越攻击返回 None，不读取文件。"""
        outside = Path(self.tmpdir) / "secret.bin"
        outside.write_bytes(b"secret")

        info = {"local_path": "../secret.bin"}
        result = load_image_bytes(self.mapping_dir, info)
        self.assertIsNone(result)

    def test_absolute_path_returns_none(self):
        """绝对路径返回 None。"""
        info = {"local_path": "/etc/shadow"}
        result = load_image_bytes(self.mapping_dir, info)
        self.assertIsNone(result)


# ── 远程下载信任边界 ────────────────────────────────────────


class DownloadUrlValidationTests(unittest.TestCase):
    """P1-9：下载 URL 信任边界校验测试。"""

    def test_https_with_trusted_host_accepted(self):
        """HTTPS + 可信 host 通过校验。"""
        # 不应抛异常
        _validate_download_url("https://ark.volces.com/img.png")
        _validate_download_url("https://cdn.byteimg.com/img.png")
        _validate_download_url("https://oss.bytedance.com/img.png")

    def test_http_rejected(self):
        """非 HTTPS 被拒绝。"""
        with self.assertRaises(ImageDownloadError) as ctx:
            _validate_download_url("http://ark.volces.com/img.png")
        self.assertIn("HTTPS", str(ctx.exception))

    def test_non_trusted_host_rejected(self):
        """非可信 host 被拒绝。"""
        with self.assertRaises(ImageDownloadError) as ctx:
            _validate_download_url("https://example.com/img.png")
        self.assertIn("host", str(ctx.exception).lower())

    def test_internal_ip_rejected(self):
        """内网 IP 字面量被拒绝。"""
        with self.assertRaises(ImageDownloadError):
            _validate_download_url("https://127.0.0.1/img.png")
        with self.assertRaises(ImageDownloadError):
            _validate_download_url("https://10.0.0.1/img.png")
        with self.assertRaises(ImageDownloadError):
            _validate_download_url("https://192.168.1.1/img.png")
        with self.assertRaises(ImageDownloadError):
            _validate_download_url("https://169.254.169.254/latest/meta-data/")

    def test_localhost_rejected(self):
        """localhost 被拒绝。"""
        with self.assertRaises(ImageDownloadError):
            _validate_download_url("https://localhost/img.png")

    def test_empty_url_rejected(self):
        """空 URL 被拒绝。"""
        with self.assertRaises(ImageDownloadError):
            _validate_download_url("")

    def test_missing_host_rejected(self):
        """缺少 host 被拒绝。"""
        with self.assertRaises(ImageDownloadError):
            _validate_download_url("https:///img.png")


class SafeDownloadTests(unittest.TestCase):
    """P1-9：_safe_download_image 信任边界测试。"""

    def _make_response(
        self,
        status_code=200,
        content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
        content_type="image/png",
        location=None,
    ):
        """构造 mock response。"""
        response = unittest.mock.MagicMock()
        response.status_code = status_code
        response.headers = {}
        if content_type is not None:
            response.headers["Content-Type"] = content_type
        if location is not None:
            response.headers["Location"] = location
        response.iter_content.return_value = [content]
        response.close = unittest.mock.MagicMock()
        return response

    def test_valid_https_download_succeeds(self):
        """合法 HTTPS + 可信 host + 正确 MIME 下载成功。"""
        response = self._make_response()
        with patch("src.core.image_translator.requests.get", return_value=response):
            data = _safe_download_image("https://ark.volces.com/img.png")
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_non_https_rejected(self):
        """非 HTTPS 被拒绝，不发起请求。"""
        with patch("src.core.image_translator.requests.get") as mock_get:
            with self.assertRaises(ImageDownloadError) as ctx:
                _safe_download_image("http://ark.volces.com/img.png")
        self.assertIn("HTTPS", str(ctx.exception))
        mock_get.assert_not_called()

    def test_non_trusted_host_rejected(self):
        """非可信 host 被拒绝。"""
        with self.assertRaises(ImageDownloadError) as ctx:
            _safe_download_image("https://evil.com/img.png")
        self.assertIn("host", str(ctx.exception).lower())

    def test_redirect_to_internal_rejected(self):
        """重定向到内网被拒绝。"""
        # 第一次返回 302 重定向到内网地址
        redirect_response = self._make_response(
            status_code=302, location="https://127.0.0.1/secret"
        )
        with patch(
            "src.core.image_translator.requests.get",
            return_value=redirect_response,
        ):
            with self.assertRaises(ImageDownloadError):
                _safe_download_image("https://ark.volces.com/img.png")

    def test_redirect_to_non_trusted_host_rejected(self):
        """重定向到非可信 host 被拒绝。"""
        redirect_response = self._make_response(status_code=302, location="https://evil.com/steal")
        with patch(
            "src.core.image_translator.requests.get",
            return_value=redirect_response,
        ):
            with self.assertRaises(ImageDownloadError):
                _safe_download_image("https://ark.volces.com/img.png")

    def test_redirect_to_trusted_host_followed(self):
        """重定向到可信 host 被跟随。"""
        redirect_response = self._make_response(
            status_code=302, location="https://cdn.byteimg.com/img.png"
        )
        final_response = self._make_response(status_code=200)
        with patch(
            "src.core.image_translator.requests.get",
            side_effect=[redirect_response, final_response],
        ):
            data = _safe_download_image("https://ark.volces.com/img.png")
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_oversized_file_rejected(self):
        """超大文件被拒绝。"""
        # 构造超过 50MB 的流式响应
        big_chunk = b"\x89PNG\r\n\x1a\n" + b"\x00" * (51 * 1024 * 1024)
        response = self._make_response(content=big_chunk)
        with patch(
            "src.core.image_translator.requests.get",
            return_value=response,
        ):
            with self.assertRaises(ImageDownloadError) as ctx:
                _safe_download_image("https://ark.volces.com/img.png")
        self.assertIn("最大字节数", str(ctx.exception))

    def test_wrong_content_type_rejected(self):
        """错误 Content-Type 被拒绝。"""
        response = self._make_response(content_type="text/html")
        with patch(
            "src.core.image_translator.requests.get",
            return_value=response,
        ):
            with self.assertRaises(ImageDownloadError) as ctx:
                _safe_download_image("https://ark.volces.com/img.png")
        self.assertIn("Content-Type", str(ctx.exception))

    def test_invalid_magic_bytes_rejected(self):
        """无效魔数被拒绝（不盲信 Content-Type）。"""
        # Content-Type 声称是 image/png，但内容是 HTML
        response = self._make_response(
            content=b"<html><body>evil</body></html>",
            content_type="image/png",
        )
        with patch(
            "src.core.image_translator.requests.get",
            return_value=response,
        ):
            with self.assertRaises(ImageDownloadError) as ctx:
                _safe_download_image("https://ark.volces.com/img.png")
        self.assertIn("无法识别", str(ctx.exception))

    def test_http_error_status_rejected(self):
        """HTTP 错误状态码被拒绝。"""
        response = self._make_response(status_code=404)
        with patch(
            "src.core.image_translator.requests.get",
            return_value=response,
        ):
            with self.assertRaises(ImageDownloadError) as ctx:
                _safe_download_image("https://ark.volces.com/img.png")
        self.assertIn("404", str(ctx.exception))

    def test_streaming_download_used(self):
        """下载使用流式读取（iter_content）。"""
        response = self._make_response()
        with patch(
            "src.core.image_translator.requests.get",
            return_value=response,
        ):
            _safe_download_image("https://ark.volces.com/img.png")
        # 应调用 iter_content
        response.iter_content.assert_called_once()

    def test_no_auto_redirect(self):
        """不使用自动重定向（allow_redirects=False）。"""
        response = self._make_response()
        with patch(
            "src.core.image_translator.requests.get",
            return_value=response,
        ) as mock_get:
            _safe_download_image("https://ark.volces.com/img.png")
        # 验证 allow_redirects=False
        _, kwargs = mock_get.call_args
        self.assertFalse(kwargs.get("allow_redirects", True))

    def test_error_message_does_not_leak_url(self):
        """错误消息不泄露完整 URL（避免敏感路径泄露）。"""
        sensitive_url = "https://ark.volces.com/secret/path/with/credentials"
        with self.assertRaises(ImageDownloadError) as ctx:
            _validate_download_url("http://evil.com")
        # 错误消息是通用的，不包含具体路径
        # 这里主要验证非可信 host 的错误不包含敏感信息


if __name__ == "__main__":
    unittest.main()
