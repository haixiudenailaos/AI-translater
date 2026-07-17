#!/usr/bin/env python3
"""
统一日志模块
提供全局日志配置，替代散落各处的 print() 和 logging.basicConfig()。
"""

import logging
import sys
from pathlib import Path

# 是否已显式初始化。R2-BUG-021：get_logger 不再隐式调用 setup_logging，
# 避免在 AppPaths 生效前把日志目录固定为 ./logs。
_initialized = False


def setup_logging(
    level=logging.INFO, log_file: str = "translator.log", log_dir: "Path | str | None" = None
):
    """初始化全局日志配置（仅执行一次）。

    - 控制台输出 INFO 及以上
    - 文件输出 DEBUG 及以上（便于排查问题）

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
        resolved_log_dir = Path(log_dir) if log_dir is not None else Path("logs")
        resolved_log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            resolved_log_dir / log_file, encoding="utf-8", delay=True
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
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
