#!/usr/bin/env python3
"""
阶段 5：打包配置 smoke test

验证 PyInstaller spec、hook 和 requirements-image-manga.txt 文件本身：
- spec 文件可被 Python AST 解析
- hook 文件可被加载且声明了 hiddenimports / datas
- requirements-image-manga.txt 行格式合法

实际发行包 smoke test 需在干净 Windows 机器上手动执行：
1. 安装依赖：`pip install -r requirements.txt -r requirements-image-manga.txt`
2. 打包：`pyinstaller translator.spec`
3. Windows 启动 dist/LightNovelTranslator-1.6-Windows-x64.exe；macOS 解压并双击 LightNovelTranslator-1.6-macOS-Universal.app
4. 在设置页确认 Manga 模型状态为「可用」
5. 导入 EPUB，点击「图片翻译」启动 Manga
6. AI Key 为空时仍可启动 Manga（关键断言）
7. 取消、重启应用，验证结果不丢失（manifest v2 在磁盘上）
"""

import importlib.util
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestSpecFile:
    """translator.spec 文件本身的可加载性。"""

    def test_spec_file_parses_as_python(self):
        spec_path = PROJECT_ROOT / "translator.spec"
        assert spec_path.exists(), "translator.spec 不存在"

        import ast

        ast.parse(spec_path.read_text(encoding="utf-8"))

    def test_spec_includes_manga_modules(self):
        """spec 应包含 Manga Provider 相关模块的 hiddenimports。"""
        spec_text = (PROJECT_ROOT / "translator.spec").read_text(encoding="utf-8")

        required = [
            "src.application.image_translation_service",
            "src.domain.image_translation",
            "src.infrastructure.image_translation.manga_provider",
            "src.infrastructure.image_translation.registry",
            "src.infrastructure.image_translation.manifest_repository",
            "src.infrastructure.image_translation.runtime",
        ]
        for module in required:
            assert module in spec_text, f"spec 未声明 {module}"

    def test_spec_includes_third_party_pathex(self):
        """spec 应将 third_party/manga-image-translator 加入 pathex，使 vendor 源码可被分析。"""
        spec_text = (PROJECT_ROOT / "translator.spec").read_text(encoding="utf-8")
        assert "third_party" in spec_text
        assert "manga-image-translator" in spec_text

    def test_spec_excludes_unused_subsystems(self):
        """spec 应排除 manga-image-translator 中本项目不使用的子系统。"""
        spec_text = (PROJECT_ROOT / "translator.spec").read_text(encoding="utf-8")
        excludes_block = re.search(r"excludes\s*=\s*\[(.*?)\]", spec_text, re.DOTALL)
        assert excludes_block, "spec 未声明 excludes"
        excludes_text = excludes_block.group(1)
        assert "PyQt5" in excludes_text
        assert "fastapi" in excludes_text
        assert "uvicorn" in excludes_text


class TestTextEditionSpec:
    """PERF §11.2/§11.3：Text Edition spec（默认下载版本）。

    Text Edition 仅包含 TXT/EPUB 文本翻译 + 在线 AI 图片 Provider，
    明确排除本地 Manga 推理重依赖，确保冷启动不被 Torch/OpenCV/ONNX 主导。
    """

    def test_text_spec_file_exists_and_parses(self):
        """translator_text.spec 存在且为合法 Python AST"""
        spec_path = PROJECT_ROOT / "translator_text.spec"
        assert spec_path.exists(), "translator_text.spec 不存在"

        import ast

        ast.parse(spec_path.read_text(encoding="utf-8"))

    def test_text_spec_excludes_manga_heavy_deps(self):
        """PERF §11.3：Text Edition 必须排除本地 Manga 推理重依赖"""
        spec_text = (PROJECT_ROOT / "translator_text.spec").read_text(encoding="utf-8")
        excludes_block = re.search(r"excludes\s*=\s*\[(.*?)\]", spec_text, re.DOTALL)
        assert excludes_block, "Text Edition spec 未声明 excludes"

        excludes_text = excludes_block.group(1)
        required_excludes = [
            "torch",
            "torchvision",
            "cv2",
            "onnxruntime",
            "manga_translator",
            "transformers",
            "tokenizers",
            "sentencepiece",
            "ctranslate2",
        ]
        for dep in required_excludes:
            assert dep in excludes_text, f"Text Edition 必须排除 {dep}，避免冷启动导入重依赖"

    def test_text_spec_no_manga_hiddenimports(self):
        """PERF §11.3：Text Edition 不声明 Manga 引擎 hiddenimports"""
        spec_text = (PROJECT_ROOT / "translator_text.spec").read_text(encoding="utf-8")
        # manga_provider 不应出现在 Text Edition 的 hiddenimports 中
        assert "manga_provider" not in spec_text, (
            "Text Edition 不应包含 manga_provider hiddenimport"
        )

    def test_text_spec_no_third_party_pathex(self):
        """PERF §11.3：Text Edition 不需要 third_party 路径（无 Manga 源码）"""
        spec_text = (PROJECT_ROOT / "translator_text.spec").read_text(encoding="utf-8")
        assert "manga-image-translator" not in spec_text, (
            "Text Edition 不应引用 third_party/manga-image-translator"
        )

    def test_text_spec_includes_text_translation_modules(self):
        """PERF §11.3：Text Edition 包含文本翻译性能修复新增模块"""
        spec_text = (PROJECT_ROOT / "translator_text.spec").read_text(encoding="utf-8")
        required = [
            "src.application.autosave",
            "src.application.translation_document",
            "src.application.translation_events",
            "src.ui.translation_table_adapter",
            "src.ui.translation_event_mailbox",
            "src.ui.tk_event_pump",
        ]
        for module in required:
            assert module in spec_text, f"Text Edition 未声明 {module}"


class TestOnedirFormat:
    """PERF §11.3：验证两个 spec 都使用 onedir 格式。

    onedir 避免单文件启动时解包到临时目录的开销。
    """

    @pytest.fixture(params=["translator.spec", "translator_text.spec"])
    def spec_path(self, request):
        path = PROJECT_ROOT / request.param
        assert path.exists(), f"{request.param} 不存在"
        return path

    def test_spec_uses_onedir_exclude_binaries(self, spec_path):
        """PERF §11.3：EXE 必须设置 exclude_binaries=True（onedir 标志）"""
        spec_text = spec_path.read_text(encoding="utf-8")
        assert "exclude_binaries=True" in spec_text, (
            f"{spec_path.name} 未使用 exclude_binaries=True，不是 onedir 格式"
        )

    def test_spec_uses_collect(self, spec_path):
        """PERF §11.3：spec 必须包含 COLLECT 段（onedir 目录收集）"""
        spec_text = spec_path.read_text(encoding="utf-8")
        assert "COLLECT(" in spec_text, f"{spec_path.name} 未声明 COLLECT，不是 onedir 格式"

    def test_spec_no_src_data_duplicate(self, spec_path):
        """PERF §11.3：src 不作为重复 data 打包（由 Analysis/PYZ 收集）"""
        spec_text = spec_path.read_text(encoding="utf-8")
        # 不应出现 ('src', 'src') 形式的 data 声明
        assert "('src', 'src')" not in spec_text, (
            f"{spec_path.name} 仍将 src 作为 data 打包，与 PYZ 模块重复"
        )

    def test_spec_upx_disabled(self, spec_path):
        """PERF §11.3：UPX 默认关闭，实测后再决定是否启用"""
        spec_text = spec_path.read_text(encoding="utf-8")
        # EXE 段的 upx 应为 False
        assert "upx=False" in spec_text, f"{spec_path.name} 未关闭 UPX"


class TestPyInstallerHook:
    """hook-manga_translator.py 文件可加载且声明完整。"""

    @pytest.fixture()
    def hook_module(self):
        hook_path = PROJECT_ROOT / "hooks" / "hook-manga_translator.py"
        assert hook_path.exists(), "hook-manga_translator.py 不存在"

        spec = importlib.util.spec_from_file_location("hook_manga_translator", hook_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_hook_declares_datas(self, hook_module):
        """hook 应声明 datas（YAML / tokenizer 等资源文件）。"""
        assert hasattr(hook_module, "datas")
        assert isinstance(hook_module.datas, list)
        assert len(hook_module.datas) > 0
        for src, dst in hook_module.datas:
            assert isinstance(src, str) and isinstance(dst, str)
            assert src.startswith("manga_translator/")

    def test_hook_declares_hiddenimports(self, hook_module):
        """hook 应声明核心 manga_translator 子模块的 hiddenimports。"""
        assert hasattr(hook_module, "hiddenimports")
        assert isinstance(hook_module.hiddenimports, list)

        required = [
            "manga_translator.manga_translator",
            "manga_translator.config",
            "manga_translator.detection.default",
            "manga_translator.ocr.model_48px",
            "manga_translator.translators.external_llm",
            "manga_translator.inpainting.inpainting_lama",
            "manga_translator.rendering.text_render",
            "manga_translator.upscaling.esrgan",
        ]
        for module in required:
            assert module in hook_module.hiddenimports, f"hook 未声明 hiddenimport: {module}"

    def test_hook_does_not_import_torch(self, hook_module):
        """hook 不应在加载时 import torch，避免打包期触发重依赖。"""

        # 加载 hook 后不应在 sys.modules 中出现 torch
        # （如果 torch 已被其他测试加载，则至少验证 hook 模块没有显式 torch 属性）
        assert not hasattr(hook_module, "torch"), "hook 不应直接 import torch"


class TestRequirementsLock:
    """requirements-image-manga.txt 行格式合法且包含关键依赖。"""

    @pytest.fixture()
    def requirements_lines(self):
        path = PROJECT_ROOT / "requirements-image-manga.txt"
        assert path.exists(), "requirements-image-manga.txt 不存在"
        text = path.read_text(encoding="utf-8")
        # 去注释、去空白行、去 pip 指令行（如 --extra-index-url）
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and not line.lstrip().startswith("--")
        ]

    def test_includes_torch(self, requirements_lines):
        assert any("torch" in line and "torchvision" not in line for line in requirements_lines)

    def test_includes_onnxruntime(self, requirements_lines):
        assert any("onnxruntime" in line for line in requirements_lines)

    def test_includes_numpy_version_pin(self, requirements_lines):
        """numpy 必须锁定 <2.0 以避免与 torch/opencv ABI 冲突。"""
        numpy_lines = [line for line in requirements_lines if "numpy" in line]
        assert numpy_lines, "未声明 numpy 依赖"
        assert any("<2.0" in line or "==1." in line for line in numpy_lines), (
            "numpy 必须锁定 <2.0（或 pin 到 1.x 版本），避免 ABI 冲突"
        )

    def test_all_lines_valid_pin_format(self, requirements_lines):
        """所有依赖行格式合法（包名>=版本 或 包名>=版本,<上限）。

        PERF §10.2：允许 PEP 508 extras 语法 ``package[extra]==version``，
        例如 ``httpx[http2]==0.27.2``（HTTP/2 支持）和 ``langcodes[data]>=3.3.0``
        （数据文件）。原正则不允许 ``[...]``，误报这两行为非法格式。
        """
        pattern = re.compile(
            r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,.\-]+\])?(\s*[<>=!~]=?\s*[\d.]+(\s*,\s*)?)*$"
        )
        for line in requirements_lines:
            # 允许形如 torch>=2.1.0,<2.6 / opencv-python>=4.8.0,<4.10 /
            # httpx[http2]==0.27.2 / langcodes[data]>=3.3.0
            assert pattern.match(line), f"行格式不合法: {line}"


class TestThirdPartyProvenance:
    """third_party/manga-image-translator/ provenance 文档完整。"""

    def test_upstream_md_exists(self):
        path = PROJECT_ROOT / "third_party" / "manga-image-translator" / "UPSTREAM.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "manga-image-translator" in text
        assert "https://github.com/zyddnys/manga-image-translator" in text

    def test_local_changes_md_exists(self):
        path = PROJECT_ROOT / "third_party" / "manga-image-translator" / "LOCAL_CHANGES.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "修改记录" in text or "LOCAL_CHANGES" in text

    def test_third_party_notices_md_exists(self):
        path = PROJECT_ROOT / "third_party" / "manga-image-translator" / "THIRD_PARTY_NOTICES.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "MIT" in text
        assert "manga-image-translator" in text


class TestMangaProviderLazyImportSafety:
    """验证 Manga Provider 的惰性导入在缺少 torch 时不会让模块加载失败。"""

    def test_module_imports_without_torch(self):
        """未安装 torch 时也应能 import manga_provider 模块。"""
        # 直接 import 模块（不应抛 ImportError）
        from src.infrastructure.image_translation import manga_provider

        assert hasattr(manga_provider, "MangaImageTranslationProvider")

    def test_registry_module_imports_without_torch(self):
        from src.infrastructure.image_translation import registry

        assert hasattr(registry, "ImageTranslationProviderRegistry")

    def test_service_module_imports_without_torch(self):
        from src.application import image_translation_service

        assert hasattr(image_translation_service, "ImageTranslationService")
