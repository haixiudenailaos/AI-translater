# -*- mode: python ; coding: utf-8 -*-
"""
轻小说翻译器V1.5 - PyInstaller 规格文件
"""

import sys
from pathlib import Path

# 项目根目录
project_root = Path(SPECPATH)

# 分析主要脚本
a = Analysis(
    ['main.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        # 配置文件目录（仅包含示例文件和基础配置）
        ('config/api_config_sample.json', 'config'),
        ('config/glossary_sample.json', 'config'),
        ('config/app_config.json', 'config'),
        ('config/glossary.json', 'config'),
        # 源代码目录
        ('src', 'src'),
    ],
    hiddenimports=[
        # 确保这些模块被打包
        'src.ui.main_window',
        'src.ui.settings_window',
        'src.ui.glossary_window',
        'src.config.config_manager',
        'src.core.translator',
        'src.core.batch_processor',
        'src.core.epub_processor',
        'src.core.image_translator',
        'src.core.image_text_translator',
        'src.core.image_utils',
        'src.core.smart_cache',
        'src.api.deepseek_api',
        'src.api.siliconflow_api',
        'src.utils.file_handler',
        # 第三方库
        'httpx',
        'ebooklib',
        'bs4',
        'lxml',
        'PIL',
        'chardet',
        'requests',
        'aiohttp',
        'openai',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
    ],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=['hooks/runtime_hook_resources.py'],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

# 去除重复项
pyz = PYZ(a.pure, a.zipped_data)

# 创建可执行文件
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='轻小说翻译器V1.5',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 如果有图标文件，取消下面这行的注释
    # icon='assets/icon.ico',
)
