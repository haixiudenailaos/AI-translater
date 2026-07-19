"""Regression tests for image translation startup failures."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from src.domain.image_translation import (
    ImageTranslationProviderId,
    ImageTranslationRequest,
)
from src.domain.translation import OperationStatus
from src.infrastructure.image_translation.manga_worker_client import MangaWorkerClient
from src.infrastructure.image_translation.volcengine_provider import (
    VolcengineImageTranslationProvider,
)
from src.ui.image_translation_handler import ImageTranslationHandler


class ImageTranslationRegressionTests(unittest.TestCase):
    def test_ai_provider_imports_translator_from_src_core(self):
        closed = []
        with tempfile.TemporaryDirectory() as directory:
            mapping_dir = Path(directory)

            class FakeTranslator:
                def __init__(self, config_manager):
                    self.config_manager = config_manager

                def translate_images(
                    self,
                    received_mapping_dir,
                    target_language,
                    progress_cb,
                    *,
                    image_mappings_override=None,
                    cancel_event=None,
                ):
                    self.assert_translate_arguments(
                        received_mapping_dir,
                        target_language,
                    )
                    return {"images/a.png": "translated_images/ai/a.png"}

                def assert_translate_arguments(self, received_mapping_dir, target_language):
                    assert received_mapping_dir == str(mapping_dir)
                    assert target_language == "中文"

                def close(self):
                    closed.append(True)

            config_manager = SimpleNamespace(get_volc_key=lambda: "test-key")
            provider = VolcengineImageTranslationProvider(config_manager)
            request = ImageTranslationRequest(
                mapping_dir=mapping_dir,
                target_language="中文",
                provider_id=ImageTranslationProviderId.AI_VOLCENGINE,
            )

            fake_module = ModuleType("src.core.image_translator")
            fake_module.ImageTranslator = FakeTranslator
            with patch.dict(
                sys.modules,
                {"src.core.image_translator": fake_module},
            ):
                result = provider.translate(request)

        self.assertEqual(result.status, OperationStatus.SUCCEEDED)
        self.assertEqual(
            result.result_map,
            {"images/a.png": "translated_images/ai/a.png"},
        )
        self.assertEqual(closed, [True])

    def test_manga_worker_forces_utf8_protocol(self):
        captured = {}

        class FakeProcess:
            def poll(self):
                return None

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return FakeProcess()

        import src.infrastructure.image_translation.manga_worker_client as client_module

        with tempfile.TemporaryDirectory() as directory:
            client = MangaWorkerClient(
                SimpleNamespace(get_api_config=lambda: {}),
                resource_dir=Path(directory),
            )
            with (
                patch.object(
                    client_module,
                    "resolve_manga_python",
                    return_value=Path(sys.executable),
                ),
                patch.object(client_module.subprocess, "Popen", side_effect=fake_popen),
            ):
                client._ensure_process()

        self.assertEqual(captured["encoding"], "utf-8")
        self.assertEqual(captured["errors"], "replace")
        self.assertEqual(captured["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(captured["env"]["PYTHONUTF8"], "1")

    def test_drop_process_waits_and_kills_stuck_worker(self):
        class _Stream:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _StuckProcess:
            def __init__(self):
                self.stdin = _Stream()
                self.stdout = _Stream()
                self.stderr = _Stream()
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise client_module.subprocess.TimeoutExpired("worker", timeout)
                return 0

        import src.infrastructure.image_translation.manga_worker_client as client_module

        client = MangaWorkerClient(SimpleNamespace(get_api_config=lambda: {}))
        process = _StuckProcess()
        client._process = process

        client._drop_process(process)

        self.assertIsNone(client._process)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_manga_runtime_error_tells_user_to_use_ai(self):
        class FailingService:
            def make_request(self, **kwargs):
                return SimpleNamespace(**kwargs)

            def validate_request(self, _request):
                return []

            def translate(self, _request, _on_progress):
                raise UnicodeDecodeError("utf-8", b"\xb2", 0, 1, "invalid start byte")

        messages = []
        handler = ImageTranslationHandler.__new__(ImageTranslationHandler)
        handler.config_manager = SimpleNamespace(get_app_config=lambda: {"target_language": "中文"})
        handler.status_updater = lambda _message: None
        handler.image_progress_updater = lambda _message: None
        handler.busy_state_updater = lambda _busy: None
        handler.get_mapping_dir = lambda: Path("mapping")
        handler._service = FailingService()
        handler._worker_thread = None
        handler._safe_after = lambda callback: callback()

        with patch(
            "src.ui.image_translation_handler.messagebox.showerror",
            side_effect=lambda title, message: messages.append((title, message)),
        ):
            handler._translation_worker(ImageTranslationProviderId.MANGA)

        self.assertTrue(messages)
        self.assertIn("请先使用「AI 图片翻译」", messages[0][1])


if __name__ == "__main__":
    unittest.main()
