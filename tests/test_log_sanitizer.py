#!/usr/bin/env python3
"""
日志脱敏工具单元测试（UXF-006）

验证 src/utils/log_sanitizer.py：
- API Key 脱敏
- Authorization 头脱敏
- 字典敏感字段脱敏
- 长 token 脱敏
- 短字符串完全遮蔽
- 幂等性
"""

from src.utils.log_sanitizer import (
    is_safe_to_log,
    mask_value,
    sanitize_dict,
    sanitize_error_message,
    sanitize_for_log,
)

# ── mask_value ─────────────────────────────


class TestMaskValue:
    def test_empty(self):
        assert mask_value("") == ""

    def test_short_completely_masked(self):
        """短字符串（<=8位）完全遮蔽"""
        assert mask_value("abc") == "***"
        assert mask_value("12345678") == "********"

    def test_long_keeps_prefix_suffix(self):
        """长字符串保留前4后2"""
        masked = mask_value("sk-abcdefghij1234567890")
        assert masked.startswith("sk-a")
        assert masked.endswith("90")
        assert "*" in masked

    def test_boundary_9_chars(self):
        masked = mask_value("123456789")
        assert masked == "1234***89"


# ── sanitize_for_log 字符串 ────────────────


class TestSanitizeString:
    def test_plain_text_unchanged(self):
        assert sanitize_for_log("hello world") == "hello world"

    def test_sk_key_masked(self):
        result = sanitize_for_log("api_key=sk-abcdefghij1234567890")
        assert "sk-abcdefghij1234567890" not in result
        assert "sk-a" in result

    def test_bearer_token_masked(self):
        result = sanitize_for_log("Authorization: Bearer abc123def456ghi789")
        assert "Bearer abc123def456ghi789" not in result
        assert "Bearer" in result
        assert "***" in result

    def test_error_message_sanitized(self):
        msg = "Request failed with key sk-test1234567890abcdef"
        result = sanitize_error_message(msg)
        assert "sk-test1234567890abcdef" not in result

    def test_idempotent(self):
        """对已脱敏内容再次脱敏不破坏结果"""
        text = "key=sk-abcdefghij1234567890"
        once = sanitize_for_log(text)
        twice = sanitize_for_log(once)
        assert twice == once


# ── sanitize_for_log 字典 ──────────────────


class TestSanitizeDict:
    def test_sensitive_field_masked(self):
        data = {
            "provider": "siliconflow",
            "api_key": "sk-test1234567890abcdef",
            "model": "deepseek-v3",
        }
        result = sanitize_dict(data)
        assert result["provider"] == "siliconflow"
        assert result["model"] == "deepseek-v3"
        assert result["api_key"] != "sk-test1234567890abcdef"
        assert "*" in result["api_key"]

    def test_nested_dict_sanitized(self):
        data = {
            "config": {
                "token": "very_long_secret_token_value_12345",
                "name": "test",
            },
        }
        result = sanitize_for_log(data)
        assert result["config"]["name"] == "test"
        assert "*" in str(result["config"]["token"])

    def test_non_sensitive_field_preserved(self):
        data = {"model": "gpt-4", "temperature": 0.3}
        result = sanitize_dict(data)
        assert result == data

    def test_list_sanitized(self):
        data = ["normal", {"api_key": "sk-secret1234567890"}]
        result = sanitize_for_log(data)
        assert result[0] == "normal"
        assert "*" in result[1]["api_key"]

    def test_none_preserved(self):
        assert sanitize_for_log(None) is None

    def test_number_preserved(self):
        assert sanitize_for_log(42) == 42
        assert sanitize_for_log(3.14) == 3.14

    def test_case_insensitive_keys(self):
        """敏感字段名不区分大小写"""
        data = {"API_KEY": "sk-test1234567890", "Token": "secret_value"}
        result = sanitize_dict(data)
        assert "*" in result["API_KEY"]
        assert result["Token"] != "secret_value"

    def test_hyphen_keys(self):
        """带连字符的敏感字段名"""
        data = {"api-key": "sk-test1234567890"}
        result = sanitize_dict(data)
        assert "*" in str(result["api-key"])


# ── is_safe_to_log ─────────────────────────


class TestIsSafeToLog:
    def test_safe_text(self):
        assert is_safe_to_log("normal log message") is True

    def test_unsafe_text(self):
        assert is_safe_to_log("key=sk-abcdefghij1234567890") is False

    def test_bearer_unsafe(self):
        assert is_safe_to_log("Authorization: Bearer token123") is False
