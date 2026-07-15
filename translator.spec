# -*- mode: python ; coding: utf-8 -*-
"""
轻小说翻译器V1.6 - PyInstaller 规格文件

集成 manga-image-translator 后的打包配置：
- 默认包含 Manga Provider 所需的隐藏导入和资源。
- torch/onnxruntime/cv2 等重依赖由官方 PyInstaller hooks 自动收集。
- 字体资源单独声明，便于审计和分发。
- 未安装重依赖时 spec 仍可加载（Manga Provider 内部惰性导入，
  validate() 会给出明确的引擎缺失提示）。
"""

import sys
from pathlib import Path

# 项目根目录
project_root = Path(SPECPATH)

# ── 字体资源（Manga 渲染模块需要中文字体）─────────────
# 若存在 assets/fonts 目录则打包；缺失则跳过，Manga Provider 会在运行时
# 通过 Pillow 使用系统默认字体
_font_datas = []
_fonts_dir = project_root / 'assets' / 'fonts'
if _fonts_dir.exists():
    for _font_file in _fonts_dir.glob('*.[to]tf'):
        _font_datas.append(
            (str(_font_file.relative_to(project_root)), 'assets/fonts')
        )

# ── manga_translator 源码（如已 vendor 到 third_party）──
# third_party/manga-image-translator/manga_translator/ 存在时打包
_manga_datas = []
_manga_vendor = project_root / 'third_party' / 'manga-image-translator' / 'manga_translator'
if _manga_vendor.exists():
    _manga_datas.append(
        (str(_manga_vendor.relative_to(project_root)), 'manga_translator')
    )

# 分析主要脚本
a = Analysis(
    ['main.py'],
    pathex=[
        str(project_root),
        # third_party 路径：使 manga_translator 可被作为顶层包导入
        str(project_root / 'third_party' / 'manga-image-translator'),
    ],
    binaries=[],
    datas=[
        # 配置文件目录（仅包含示例文件和基础配置）
        ('config/api_config_sample.json', 'config'),
        ('config/glossary_sample.json', 'config'),
        ('config/app_config.json', 'config'),
        ('config/glossary.json', 'config'),
        # 源代码目录
        ('src', 'src'),
    ]
    + _font_datas
    + _manga_datas,
    hiddenimports=[
        # 确保这些模块被打包
        'src.ui.main_window',
        'src.ui.settings_window',
        'src.ui.glossary_window',
        'src.ui.concurrent_window',
        'src.ui.image_translation_handler',
        'src.ui.file_importer',
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
        'src.api.openai_compatible_api',
        'src.utils.file_handler',
        'src.utils.secure_storage',
        # 图片翻译新模块
        'src.application.image_translation_service',
        'src.application.ports',
        'src.domain.image_translation',
        'src.domain.errors',
        'src.infrastructure.image_translation',
        'src.infrastructure.image_translation.manga_provider',
        'src.infrastructure.image_translation.volcengine_provider',
        'src.infrastructure.image_translation.registry',
        'src.infrastructure.image_translation.runtime',
        'src.infrastructure.image_translation.manifest_repository',
        'src.infrastructure.image_translation.language_codes',
        'src.infrastructure.image_translation.fake_provider',
        'src.infrastructure.atomic_file',
        'src.infrastructure.image_rewriter',
        'src.infrastructure.image_asset_store',
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
        'keyring',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        # Manga 引擎相关（惰性导入，但打包时需声明以供运行时使用）
        # 实际安装状态在运行时由 _is_engine_available() 检查
        'numpy',
        'cv2',
    ],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=['hooks/runtime_hook_resources.py'],
    excludes=[
        # 不需要的 manga-image-translator 子模块（按需排除，避免打包膨胀）
        'PyQt5',
        'fastapi',
        'uvicorn',
    ],
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
    name='LightNovelTranslatorV1.6',
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
