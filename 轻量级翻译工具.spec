# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('config', 'config'), ('src', 'src')],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.filedialog', 'tkinter.simpledialog', 'httpx', 'httpx._client', 'httpx._config', 'httpx._models', 'chardet', 'chardet.universaldetector', 'src', 'src.ui', 'src.ui.main_window', 'src.ui.settings_window', 'src.ui.glossary_window', 'src.api', 'src.api.siliconflow_api', 'src.core', 'src.core.translator', 'src.core.smart_cache', 'src.core.batch_processor', 'src.config', 'src.config.config_manager', 'src.utils', 'src.utils.file_handler', 'json', 'threading', 'pathlib', 'concurrent.futures', 'hashlib', 'datetime', 'shutil', 're', 'time'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pandas', 'scipy', 'PIL'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='轻量级翻译工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
