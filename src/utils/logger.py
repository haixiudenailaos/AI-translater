#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一日志模块
提供全局日志配置，替代散落各处的 print() 和 logging.basicConfig()。
"""

import logging
import sys
from pathlib import Path


_initialized = False


def setup_logging(level=logging.INFO, log_file: str = "translator.log"):
    """初始化全局日志配置（仅执行一次）。

    - 控制台输出 INFO 及以上
    - 文件输出 DEBUG 及以上（便于排查问题）
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / log_file, encoding="utf-8", delay=True
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:
        pass  # 文件日志非关键路径，失败不影响运行


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger，首次调用时自动初始化。"""
    setup_logging()
    return logging.getLogger(name)
