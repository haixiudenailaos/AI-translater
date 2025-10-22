#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyInstaller运行时钩子 - 资源路径修复
确保打包后的应用能正确访问资源文件和配置文件
"""

import sys
import os
from pathlib import Path

def setup_resource_paths():
    """设置资源路径,确保打包后能正确访问"""
    try:
        # 检测是否在PyInstaller打包环境中运行
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # 打包后运行：_MEIPASS是PyInstaller临时解压目录
            base_path = Path(getattr(sys, '_MEIPASS'))  # type: ignore
            
            # 设置环境变量，方便其他模块访问
            os.environ['PYINSTALLER_BASE_PATH'] = str(base_path)
            
            # 确保config目录存在于可写位置
            # 使用用户目录而不是临时目录
            user_config_dir = Path.home() / ".轻小说翻译器V1.1" / "config"
            user_config_dir.mkdir(parents=True, exist_ok=True)
            
            # 如果用户配置目录为空，从打包资源复制默认配置
            packaged_config = base_path / "config"
            if packaged_config.exists():
                import shutil
                for config_file in packaged_config.glob("*.json"):
                    user_config_file = user_config_dir / config_file.name
                    # 只在用户配置不存在时才复制
                    if not user_config_file.exists():
                        try:
                            shutil.copy2(config_file, user_config_file)
                        except Exception:
                            pass
            
            # 设置用户配置目录环境变量
            os.environ['APP_CONFIG_DIR'] = str(user_config_dir)
            
        else:
            # 开发环境运行：使用项目根目录
            base_path = Path(__file__).parent.parent
            os.environ['PYINSTALLER_BASE_PATH'] = str(base_path)
            os.environ['APP_CONFIG_DIR'] = str(base_path / "config")
            
    except Exception as e:
        # 静默失败，避免影响应用启动
        print(f"Warning: Failed to setup resource paths: {e}")

# 在模块导入时立即执行
setup_resource_paths()
