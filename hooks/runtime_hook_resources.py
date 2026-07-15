#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyInstaller运行时钩子 - 资源路径修复
BUG-001：路径解析已统一到 src/app_paths.py，此钩子仅保留最小兼容标记。
"""

import sys
import os
from pathlib import Path

def setup_resource_paths():
    """设置资源路径（BUG-001：实际路径解析由 AppPaths 统一处理）"""
    try:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # 打包后运行：标记资源根目录，AppPaths 会读取 sys._MEIPASS
            base_path = Path(getattr(sys, '_MEIPASS'))  # type: ignore
            os.environ['PYINSTALLER_BASE_PATH'] = str(base_path)
        else:
            # 开发环境运行
            base_path = Path(__file__).parent.parent
            os.environ['PYINSTALLER_BASE_PATH'] = str(base_path)
    except Exception as e:
        # 静默失败，避免影响应用启动
        print(f"Warning: Failed to setup resource paths: {e}")

# 在模块导入时立即执行
setup_resource_paths()
