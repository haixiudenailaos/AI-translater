#!/usr/bin/env python3
"""
统一日志模块
提供全局日志配置，替代散落各处的 print() 和 logging.basicConfig()。
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .log_sanitizer import sanitize_for_log

# 是否已显式初始化。R2-BUG-021：get_logger 不再隐式调用 setup_logging，
# 避免在 AppPaths 生效前把日志目录固定为 ./logs。
_initialized = False


class _SanitizingFilter(logging.Filter):
    """P1-5：中央脱敏 Filter，对每条日志的格式化结果做脱敏。

    安装到所有 handler 上，确保 API Key / Bearer token 等敏感信息
    不会写入日志文件或控制台。脱敏逻辑由 ``log_sanitizer.sanitize_for_log``
    提供（正则匹配 sk-xxx、Bearer xxx、长 token 等）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 先格式化消息（record.msg % record.args），再做脱敏
        msg = record.getMessage()
        sanitized = sanitize_for_log(msg)
        if sanitized != msg:
            # 将脱敏后的结果写回 record，清空 args 避免二次格式化
            record.msg = sanitized if isinstance(sanitized, str) else str(sanitized)
            record.args = None
        return True


class _SanitizingFormatter(logging.Formatter):
    """Sanitize both normal records and rendered exception tracebacks."""

    def formatException(self, exc_info) -> str:
        return str(sanitize_for_log(super().formatException(exc_info)))

    def format(self, record: logging.LogRecord) -> str:
        return str(sanitize_for_log(super().format(record)))


def setup_logging(
    level=logging.INFO, log_file: str = "translator.log", log_dir: "Path | str | None" = None
):
    """初始化全局日志配置（仅执行一次）。

    - 控制台输出 INFO 及以上
    - 文件输出 DEBUG 及以上（便于排查问题）

    P1-5：
    - 文件 handler 改为 RotatingFileHandler，单文件上限 10MB、保留 5 个备份，
      避免日志无限增长占满磁盘。
    - 所有 handler 安装 ``_SanitizingFilter``，确保 API Key 等敏感信息不泄漏。

    R2-BUG-021：必须由启动入口在解析完 AppPaths 后显式调用，
    get_logger 不再自动触发本函数。

    Args:
        level: 控制台日志级别
        log_file: 日志文件名
        log_dir: 日志目录（BUG-001：优先使用 AppPaths.log_dir；默认回退到 ./logs）
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = _SanitizingFormatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # P1-5：中央脱敏 Filter，所有 handler 共享
    sanitizing_filter = _SanitizingFilter()

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    console.addFilter(sanitizing_filter)
    root.addHandler(console)

    # 文件（P1-5：改用 RotatingFileHandler，有界、轮转）
    try:
        resolved_log_dir = Path(log_dir) if log_dir is not None else Path("logs")
        resolved_log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            resolved_log_dir / log_file,
            encoding="utf-8",
            delay=True,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        file_handler.addFilter(sanitizing_filter)
        root.addHandler(file_handler)
    except Exception:
        pass  # 文件日志非关键路径，失败不影响运行


def is_initialized() -> bool:
    """返回日志是否已显式初始化（供启动流程诊断用）。"""
    return _initialized


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger。

    R2-BUG-021：本函数不再隐式调用 setup_logging()，避免模块导入阶段
    在 AppPaths.log_dir 生效前就把全局日志目录固定为 ./logs。
    启动入口必须先解析路径，再显式调用 setup_logging(log_dir=...)。
    在初始化完成前，日志通过 root logger 的 lastResort handler 输出到 stderr。
    """
    return logging.getLogger(name)
