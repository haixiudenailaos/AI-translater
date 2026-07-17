#!/usr/bin/env python3
"""Locate and validate the vendored manga translation engine.

The upstream package is stored below ``third_party`` and is intentionally not
installed into the user's global site-packages.  This module is the only place
that adjusts the import path, keeping path discovery out of the Provider and UI.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_IMPORT_LOCK = threading.Lock()
_SUPPORTED_PYTHON = {(3, 10), (3, 11)}
_REQUIRED_MODULES = {
    "aiofiles": "aiofiles",
    "aiohttp": "aiohttp",
    "aioshutil": "aioshutil",
    "cv2": "opencv-python",
    "einops": "einops",
    "freetype": "freetype-py",
    "google.genai": "google-genai",
    "kornia": "kornia",
    "langdetect": "langdetect",
    "numpy": "numpy",
    "onnxruntime": "onnxruntime",
    "openai": "openai",
    "PIL": "Pillow",
    "pydantic": "pydantic",
    "py3langid": "py3langid",
    "pyclipper": "pyclipper",
    "rich": "rich",
    "rusty_manga_image_translator": "rusty-manga-image-translator",
    "shapely": "shapely",
    "skimage": "scikit-image",
    "torch": "torch",
    "torchvision": "torchvision",
}


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def resolve_manga_python(configured: str | None = None) -> Path | None:
    """Resolve a Python 3.11 interpreter for the isolated Manga worker."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    if os.environ.get("MANGA_PYTHON311"):
        candidates.append(os.environ["MANGA_PYTHON311"])
    if sys.version_info[:2] == (3, 11):
        candidates.append(sys.executable)
    candidates.extend(
        (
            r"C:\Python311\python.exe",
            r"C:\Program Files\Python311\python.exe",
            str(Path.home() / "AppData/Local/Programs/Python/Python311/python.exe"),
        )
    )
    launcher = shutil.which("py")
    if launcher:
        try:
            discovered = subprocess.check_output(
                [launcher, "-3.11", "-c", "import sys; print(sys.executable)"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            if discovered:
                candidates.append(discovered)
        except (OSError, subprocess.SubprocessError):
            pass

    seen: set[str] = set()
    for candidate in candidates:
        path = Path(candidate).expanduser()
        key = str(path).lower()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            version = subprocess.check_output(
                [
                    str(path),
                    "-c",
                    "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if version == "3.11":
            return path.resolve()
    return None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _candidate_roots(resource_dir: Path | None = None) -> tuple[Path, ...]:
    roots: list[Path] = []
    if resource_dir is not None:
        resource = Path(resource_dir)
        roots.extend(
            (
                resource,
                resource / "third_party" / "manga-image-translator",
            )
        )
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        roots.append(Path(sys._MEIPASS))  # type: ignore[attr-defined]
    project = _project_root()
    roots.extend((project, project / "third_party" / "manga-image-translator"))

    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def configure_engine_import_path(resource_dir: Path | None = None) -> Path | None:
    """Make the vendored ``manga_translator`` package importable.

    Returns the package directory when found, otherwise ``None``.  The operation
    is idempotent and guarded because UI and queue workers can start concurrently.
    """
    with _IMPORT_LOCK:
        try:
            spec = importlib.util.find_spec("manga_translator")
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            locations = spec.submodule_search_locations
            if locations:
                return Path(next(iter(locations))).resolve()

        for root in _candidate_roots(resource_dir):
            package_dir = root / "manga_translator"
            if not (package_dir / "__init__.py").is_file():
                continue
            root_text = str(root)
            if root_text not in sys.path:
                sys.path.insert(0, root_text)
            importlib.invalidate_caches()
            return package_dir
    return None


def diagnose_manga_engine(resource_dir: Path | None = None) -> list[str]:
    """Return actionable environment errors without importing ML models."""
    errors: list[str] = []
    version = sys.version_info[:2]
    if version not in _SUPPORTED_PYTHON:
        errors.append(
            f"本地图片翻译仅支持全局 Python 3.10/3.11，当前为 "
            f"Python {version[0]}.{version[1]}（{sys.executable}）"
        )

    package_dir = configure_engine_import_path(resource_dir)
    if package_dir is None:
        errors.append(
            "未找到 manga_translator 源码，请确认 "
            "third_party/manga-image-translator/manga_translator 已完整存在"
        )
        return errors

    missing = [
        package_name
        for module_name, package_name in _REQUIRED_MODULES.items()
        if not _module_available(module_name)
    ]
    if missing:
        errors.append(
            "全局 Python 缺少本地图片翻译依赖: "
            + ", ".join(sorted(missing))
            + "；请执行 python -m pip install -r requirements-image-manga.txt"
        )
        return errors

    try:
        importlib.import_module("manga_translator.config")
        importlib.import_module("manga_translator.manga_translator")
    except Exception as exc:
        errors.append(f"manga_translator 加载失败: {type(exc).__name__}: {exc}")
    return errors
