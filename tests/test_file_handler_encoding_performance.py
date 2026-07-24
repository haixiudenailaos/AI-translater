"""Regression tests for bounded TXT encoding detection."""

from unittest.mock import patch

from src.utils.file_handler import _ENCODING_DETECTION_SAMPLE_BYTES, FileHandler


def test_utf8_import_bypasses_chardet(tmp_path):
    path = tmp_path / "utf8.txt"
    content = "你好，世界\n" * 10
    path.write_bytes(content.encode("utf-8"))

    with patch("chardet.detect") as detect:
        assert FileHandler().read_file(str(path)) == content

    detect.assert_not_called()


def test_legacy_encoding_detection_uses_bounded_sample(tmp_path):
    content = "中文内容\n" * 50_000
    path = tmp_path / "legacy-gbk.txt"
    path.write_bytes(content.encode("gbk"))

    with patch("chardet.detect", return_value={"encoding": "GBK"}) as detect:
        assert FileHandler().read_file(str(path)) == content

    sample = detect.call_args.args[0]
    assert len(sample) == _ENCODING_DETECTION_SAMPLE_BYTES
