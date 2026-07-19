#!/usr/bin/env python3
"""Generate platform-specific, hash-checked release dependency locks.

Run this script on each supported release platform/Python version. It invokes
the installed ``pip-compile`` executable, which resolves wheel hashes for the
current platform. Full locks include the custom Manga Rust wheel index.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_TARGETS = {
    "text-win-py310": ("requirements.txt", "requirements-text-win-py310.lock.txt"),
    "text-win-py311": ("requirements.txt", "requirements-text-win-py311.lock.txt"),
    "text-macos-py311": ("requirements.txt", "requirements-text-macos-py311.lock.txt"),
    "full-win-py311": (
        "requirements-full.in",
        "requirements-full-win-py311.lock.txt",
    ),
    "full-macos-py311": (
        "requirements-full.in",
        "requirements-full-macos-py311.lock.txt",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=sorted(LOCK_TARGETS))
    args = parser.parse_args()

    pip_compile = shutil.which("pip-compile")
    if pip_compile is None:
        raise SystemExit("Install pip-tools first: python -m pip install pip-tools")

    source, output = LOCK_TARGETS[args.target]
    subprocess.run(
        [
            pip_compile,
            "--generate-hashes",
            "--strip-extras",
            "--output-file",
            output,
            source,
        ],
        cwd=ROOT,
        check=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
