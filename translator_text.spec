# -*- mode: python ; coding: utf-8 -*-
"""
轻小说翻译器V1.6 - Text Edition PyInstaller 规格文件

PERF §11.2/§11.3：onedir 发布结构，默认下载版本。
- 仅包含 TXT/EPUB 文本翻译 + AI 图片翻译 Provider（在线 API）。
- 明确排除本地 Manga 推理重依赖：torch、torchvision、cv2、onnxruntime、
  manga_translator 及相关深度学习栈，避免冷启动被重依赖主导。
- onedir 避免单文件解包开销，启动时不再解压到临时目录。
- 冷启动期间不导入 Torch/OpenCV/ONNX Runtime，符合 §3.1 启动目标。
- 需要本地图片推理的用户使用 translator.spec（Full Manga Edition）。
"""

import sys
from pathlib import Path

# 项目根目录
project_root = Path(SPECPATH)

# ── 字体资源（文本翻译 UI 渲染需要中文字体）──────────
_font_datas = []
_fonts_dir = project_root / 'assets' / 'fonts'
if _fonts_dir.exists():
    for _font_file in _fonts_dir.glob('*.[to]tf'):
        _font_datas.append(
            (str(_font_file.relative_to(project_root)), 'assets/fonts')
        )

# 分析主要脚本
a = Analysis(
    ['main.py'],
    pathex=[
        str(project_root),
    ],
    binaries=[],
    datas=[
        ('config/edition_text.json', 'edition.json'),
        # PERF §11.3：仅保留真正的非 Python 资源；src 模块由 Analysis/PYZ 收集。
        # 配置文件目录（仅包含示例文件和基础配置）
        ('config/api_config_sample.json', 'config'),
        ('config/glossary_sample.json', 'config'),
    ]
    + _font_datas,
    hiddenimports=[
        # 文本翻译核心模块
        'src.bootstrap',
        'src.app_paths',
        'src.ui.main_window',
        'src.ui.settings_window',
        'src.ui.glossary_window',
        'src.ui.concurrent_window',
        'src.ui.file_importer',
        'src.ui.translation_table_adapter',
        'src.ui.translation_event_mailbox',
        'src.ui.tk_event_pump',
        'src.config.config_manager',
        'src.core.translator',
        'src.core.epub_processor',
        'src.core.smart_cache',
        'src.api.deepseek_api',
        'src.api.siliconflow_api',
        'src.api.openai_compatible_api',
        'src.utils.file_handler',
        'src.utils.secure_storage',
        # PERF §8/§7：文本翻译性能修复新增模块
        'src.application.autosave',
        'src.application.translation_document',
        'src.application.translation_events',
        # 图片翻译在线 Provider（不含本地 Manga 推理）
        'src.application.image_translation_service',
        'src.application.ports',
        'src.domain.image_translation',
        'src.domain.errors',
        'src.infrastructure.image_translation',
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
        'cairosvg',
        'chardet',
        'requests',
        'aiohttp',
        'openai',
        'keyring',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['hooks/runtime_hook_resources.py'],
    excludes=[
        # PERF §11.3：Text Edition 明确排除本地 Manga 推理重依赖，
        # 避免冷启动导入 Torch/OpenCV/ONNX Runtime。
        'torch',
        'torchvision',
        'cv2',
        'onnxruntime',
        'manga_translator',
        'manga_ocr',
        'transformers',
        'tokenizers',
        'sentencepiece',
        'ctranslate2',
        'accelerate',
        'open_clip_torch',
        'timm',
        'einops',
        'skimage',
        'kornia',
        'pyclipper',
        'shapely',
        'freetype',
        'imagehash',
        'omegaconf',
        'bidi',
        'arabic_reshaper',
        'hyphen',
        'py3langid',
        'langdetect',
        'langcodes',
        'editdistance',
        'tensorboardX',
        'websockets',
        'tiktoken',
        'marshmallow',
        'groq',
        'deepl',
        'google.genai',
        'pandas',
        'torch_summary',
        # 不需要的子系统
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

# PERF §11.3：onedir 结构。EXE 只包含脚本和 PYZ，二进制由 COLLECT 收集到目录。
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
    upx=False,  # PERF §11.3：先关闭 UPX，实测后决定是否启用
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=(
        str(project_root / 'windows.manifest')
        if (project_root / 'windows.manifest').exists()
        else None
    ),
    # 如果有图标文件，取消下面这行的注释
    # icon='assets/icon.ico',
)
