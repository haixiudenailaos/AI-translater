from __future__ import annotations

from pathlib import Path

from main import PACKAGED_SMOKE_TEST_ARG, run_packaged_smoke_test


def test_packaged_smoke_exercises_character_detection():
    assert PACKAGED_SMOKE_TEST_ARG == "--packaged-smoke-test"
    assert run_packaged_smoke_test(verify_gui=False) == 0


def test_windows_workflow_runs_functional_packaged_smoke():
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
    assert 'ArgumentList "--packaged-smoke-test"' in workflow
    assert "WaitForExit(60000)" in workflow
