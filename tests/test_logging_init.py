#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R2-BUG-021：日志初始化不由模块导入抢先触发

验证：
- 从任意 CWD 启动，日志始终写入 AppPaths.log_dir
- 导入 src.app_paths 不创建目录或日志处理器
- get_logger 不再隐式调用 setup_logging
"""

import importlib
import logging
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def reset_logger_state(tmp_path, monkeypatch):
    """每个测试前后重置 logger 模块的 _initialized 状态，并切到临时 CWD。"""
    from src.utils import logger as logger_mod

    saved_init = logger_mod._initialized
    # 清理 root logger 上可能存在的 handlers
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = []

    # 切换 CWD 到临时目录，确保不会污染真实 ./logs
    monkeypatch.chdir(tmp_path)

    # 重置 _initialized
    logger_mod._initialized = False

    try:
        yield logger_mod
    finally:
        logger_mod._initialized = saved_init
        root.handlers = saved_handlers
        root.level = saved_level


class TestLoggingInitOrder:
    """R2-BUG-021：日志初始化顺序。"""

    def test_import_app_paths_does_not_init_logging(self, reset_logger_state):
        """导入 src.app_paths 不触发 setup_logging。"""
        # 重新导入 app_paths 模块
        if "src.app_paths" in sys.modules:
            importlib.reload(sys.modules["src.app_paths"])
        else:
            importlib.import_module("src.app_paths")

        # _initialized 应仍为 False
        assert reset_logger_state._initialized is False, (
            "导入 app_paths 不应触发日志初始化"
        )

    def test_import_app_paths_does_not_create_logs_dir(self, reset_logger_state, tmp_path):
        """导入 src.app_paths 不在 CWD 下创建 ./logs 目录。"""
        # 确保 tmp_path 下还没有 logs
        assert not (tmp_path / "logs").exists()

        importlib.import_module("src.app_paths")

        # 导入后仍不应创建 ./logs
        assert not (tmp_path / "logs").exists(), (
            "导入 app_paths 不应在 CWD 下创建 logs 目录"
        )

    def test_get_logger_does_not_auto_init(self, reset_logger_state):
        """get_logger 不再隐式调用 setup_logging。"""
        from src.utils.logger import get_logger, is_initialized

        assert is_initialized() is False
        lg = get_logger("test.module")
        assert isinstance(lg, logging.Logger)
        # 仍不应初始化
        assert is_initialized() is False

    def test_setup_logging_uses_explicit_log_dir(self, reset_logger_state, tmp_path):
        """显式 setup_logging(log_dir=...) 后日志写入指定目录。"""
        from src.utils.logger import setup_logging, is_initialized, get_logger

        custom_log_dir = tmp_path / "custom_logs"
        setup_logging(log_dir=custom_log_dir)

        assert is_initialized() is True
        assert custom_log_dir.exists(), "应创建显式指定的日志目录"

        # 触发一条日志，验证文件被创建在 custom_logs 下
        lg = get_logger("test.explicit")
        lg.warning("test message for r2-bug-021")

        # flush handlers
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass

        log_files = list(custom_log_dir.glob("*.log"))
        assert log_files, f"日志文件应创建在 {custom_log_dir} 下"

    def test_setup_logging_idempotent(self, reset_logger_state, tmp_path):
        """setup_logging 只生效一次，第二次调用不改变目录。"""
        from src.utils.logger import setup_logging

        first_dir = tmp_path / "first_logs"
        second_dir = tmp_path / "second_logs"

        setup_logging(log_dir=first_dir)
        root_handlers_after_first = list(logging.getLogger().handlers)

        setup_logging(log_dir=second_dir)
        root_handlers_after_second = list(logging.getLogger().handlers)

        # 第二次不应新增 handler
        assert len(root_handlers_after_second) == len(root_handlers_after_first)
        # second_dir 不应被创建
        assert not second_dir.exists()
