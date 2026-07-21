#!/usr/bin/env python3
"""Packaging contract for the single online-AI desktop edition."""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _spec_text() -> str:
    return (PROJECT_ROOT / "translator.spec").read_text(encoding="utf-8")


def test_single_spec_exists_and_parses():
    spec_path = PROJECT_ROOT / "translator.spec"
    assert spec_path.exists()
    ast.parse(spec_path.read_text(encoding="utf-8"))
    assert not (PROJECT_ROOT / "translator_text.spec").exists()


def test_spec_builds_onefile_and_includes_ai_image_workflow():
    spec = _spec_text()
    assert "exclude_binaries=True" not in spec
    assert "COLLECT(" not in spec
    assert "src.ui.image_translation_handler" in spec
    assert "src.core.image_text_translator" in spec
    assert "src.core.image_translator" in spec
    assert "volcenginesdkarkruntime" in spec


def test_local_image_engine_artifacts_are_removed():
    assert not (PROJECT_ROOT / "requirements-image-manga.txt").exists()
    assert not (PROJECT_ROOT / "hooks" / "hook-manga_translator.py").exists()
    assert not (PROJECT_ROOT / "third_party" / "manga-image-translator").exists()
    assert "image-manga" not in (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_release_workflow_uses_single_spec():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert "pyinstaller translator.spec --clean --noconfirm" in workflow
    assert "translator_text.spec" not in workflow


def test_spec_does_not_duplicate_src_as_data_and_disables_upx():
    spec = _spec_text()
    assert "('src', 'src')" not in spec
    assert "upx=False" in spec
