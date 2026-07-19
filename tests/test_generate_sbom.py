from __future__ import annotations

import json

import pytest

from tools.generate_sbom import generate_sbom


def test_generate_sbom_records_installed_declared_dependency(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    monkeypatch.setattr(
        "tools.generate_sbom._load_project_metadata",
        lambda _path: {
            "name": "sample-app",
            "version": "1.0",
            "dependencies": ["Example-Package>=1"],
            "optional-dependencies": {"dev": ["Dev-Package==2"]},
        },
    )
    monkeypatch.setattr(
        "tools.generate_sbom._installed_versions",
        lambda: {"example-package": "1.2.3", "dev-package": "2.0.0"},
    )

    sbom = generate_sbom(pyproject, ("dev",))

    assert sbom["bomFormat"] == "CycloneDX"
    assert [(item["name"], item["version"]) for item in sbom["components"]] == [
        ("Dev-Package", "2.0.0"),
        ("Example-Package", "1.2.3"),
    ]
    json.dumps(sbom)


def test_generate_sbom_rejects_uninstalled_declared_dependency(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    monkeypatch.setattr(
        "tools.generate_sbom._load_project_metadata",
        lambda _path: {
            "name": "sample-app",
            "version": "1.0",
            "dependencies": ["missing>=1"],
            "optional-dependencies": {},
        },
    )
    monkeypatch.setattr("tools.generate_sbom._installed_versions", lambda: {})

    with pytest.raises(RuntimeError, match="missing"):
        generate_sbom(pyproject, ())
