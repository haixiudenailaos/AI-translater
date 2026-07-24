from __future__ import annotations

from pathlib import Path


def test_svg_converter_is_declared_in_runtime_dependencies():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    requirements = Path("requirements.txt").read_text(encoding="utf-8")

    assert '"CairoSVG>=2.7.0,<3.0"' in pyproject
    assert "CairoSVG>=2.7.0,<3.0" in requirements


def test_svg_converter_is_included_in_pyinstaller_build():
    spec = Path("translator.spec").read_text(encoding="utf-8")
    assert "'cairosvg'" in spec


def test_chardet_is_pinned_to_pyinstaller_safe_release():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    windows_lock = Path("requirements-text-win-py310.lock.txt").read_text(encoding="utf-8")

    assert '"chardet==5.2.0"' in pyproject
    assert "chardet==5.2.0" in requirements
    assert "chardet==5.2.0" in windows_lock
    assert "chardet==7" not in windows_lock


def test_project_python_contract_matches_tested_release_matrix():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    build_workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10,!=3.11.*"' in pyproject
    assert 'target-version = "py310"' in pyproject
    assert 'python-version: ["3.10", "3.12", "3.13"]' in build_workflow
