#!/usr/bin/env python3
"""Generate a dependency SBOM for a release environment using only the stdlib."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _distribution_name(requirement: str) -> str:
    match = _NAME_RE.match(requirement)
    if match is None:
        raise ValueError(f"无法解析依赖声明：{requirement}")
    return match.group(1)


def _installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            versions[name.casefold().replace("_", "-")] = distribution.version
    return versions


def _load_project_metadata(pyproject_path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - Python 3.11 release tool
        raise RuntimeError("生成 SBOM 需要 Python 3.11+。") from exc
    return tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]


def generate_sbom(pyproject_path: Path, extras: tuple[str, ...]) -> dict[str, Any]:
    project = _load_project_metadata(pyproject_path)
    requirements = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        try:
            requirements.extend(optional[extra])
        except KeyError as exc:
            raise ValueError(f"pyproject.toml 中不存在 extra：{extra}") from exc

    installed = _installed_versions()
    components = []
    for requirement in sorted(set(requirements), key=str.casefold):
        name = _distribution_name(requirement)
        normalized_name = name.casefold().replace("_", "-")
        version = installed.get(normalized_name)
        if version is None:
            raise RuntimeError(f"SBOM 缺少已安装的依赖：{name}（声明：{requirement}）")
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name}@{version}",
                "properties": [
                    {"name": "ai-translater:declared-requirement", "value": requirement}
                ],
            }
        )

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"vendor": "AI-translater", "name": "tools/generate_sbom.py"}],
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
            },
        },
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 CycloneDX JSON SBOM")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    try:
        sbom = generate_sbom(arguments.pyproject, tuple(arguments.extra))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"生成 SBOM 失败：{exc}", file=sys.stderr)
        return 1

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
