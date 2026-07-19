from __future__ import annotations

from pathlib import Path


def test_svg_converter_is_declared_in_runtime_dependencies():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    requirements = Path("requirements.txt").read_text(encoding="utf-8")

    assert '"CairoSVG>=2.7.0,<3.0"' in pyproject
    assert "CairoSVG>=2.7.0,<3.0" in requirements


def test_svg_converter_is_included_in_both_pyinstaller_editions():
    for spec_name in ("translator.spec", "translator_text.spec"):
        spec = Path(spec_name).read_text(encoding="utf-8")
        assert "'cairosvg'" in spec


def test_project_python_contract_matches_tested_release_matrix():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    manga_requirements = Path("requirements-image-manga.txt").read_text(encoding="utf-8")
    build_workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10"' in pyproject
    assert 'target-version = "py310"' in pyproject
    assert "Python 3.10 及以上" in manga_requirements
    assert 'python-version: ["3.10", "3.11", "3.12", "3.13"]' in build_workflow
