# -*- mode: python ; coding: utf-8 -*-
"""
轻小说翻译器 V1.6.1 PyInstaller 规格文件

包含 TXT/EPUB 文本翻译，以及 V1.5 风格的视觉检测和在线图片翻译。
使用 onefile 生成单个可执行文件。
"""

import sys
from pathlib import Path

# 项目根目录
project_root = Path(SPECPATH)

# Python 3.14 on Windows can import ``_tkinter`` while PyInstaller's Tcl/Tk
# probe still fails during analysis. Keep a local fallback so GUI modules and
# their runtime data are not silently excluded from the executable.
_tcl_tk_datas = []
_tcl_tk_binaries = []
if sys.platform == 'win32':
    _python_root = Path(sys.base_prefix)
    _tcl_root = _python_root / 'tcl' / 'tcl8.6'
    _tk_root = _python_root / 'tcl' / 'tk8.6'
    if _tcl_root.exists() and _tk_root.exists():
        _tcl_tk_datas.extend(
            [
                (str(_tcl_root), '_tcl_data'),
                (str(_tk_root), '_tk_data'),
            ]
        )
    for _dll_name in ('tcl86t.dll', 'tk86t.dll'):
        _dll_path = _python_root / 'DLLs' / _dll_name
        if _dll_path.exists():
            _tcl_tk_binaries.append((str(_dll_path), '.'))

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
    binaries=_tcl_tk_binaries,
    datas=[
        # PERF §11.3：仅保留真正的非 Python 资源；src 模块由 Analysis/PYZ 收集。
        # 配置文件目录（仅包含示例文件和基础配置）
        ('config/api_config_sample.json', 'config'),
        ('config/glossary_sample.json', 'config'),
    ]
    + _font_datas
    + _tcl_tk_datas,
    hiddenimports=[
        # 文本翻译核心模块
        'src.bootstrap',
        'src.app_paths',
        'src.ui.main_window',
        'src.ui.settings_window',
        'src.ui.glossary_window',
        'src.ui.concurrent_window',
        'src.ui.file_importer',
        'src.ui.image_translation_handler',
        'src.ui.translation_table_adapter',
        'src.ui.translation_event_mailbox',
        'src.ui.tk_event_pump',
        'src.config.config_manager',
        'src.core.translator',
        'src.core.epub_processor',
        'src.core.image_text_translator',
        'src.core.image_translator',
        'src.core.image_utils',
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
        # 图片翻译与 EPUB 资源替换
        'src.application.ports',
        'src.domain.errors',
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
        'volcenginesdkarkruntime',
        'volcenginesdkcore',
        'keyring',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
    ],
    hookspath=[str(project_root / 'hooks')],
    hooksconfig={},
    runtime_hooks=['hooks/runtime_hook_resources.py'],
    excludes=[
        # 在线图片翻译不需要本地深度学习栈。
        'torch',
        'torchvision',
        'cv2',
        'onnxruntime',
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

# Single onefile executable.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LightNovelTranslatorV1.6.1',
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
