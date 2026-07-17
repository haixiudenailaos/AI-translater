import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.domain.image_translation import (
    ImageTranslationProviderId,
    ImageTranslationResult,
)
from src.domain.translation import OperationStatus
from src.infrastructure.image_translation.engine_loader import (
    configure_engine_import_path,
    diagnose_manga_engine,
)
from src.ui.image_translation_handler import ImageTranslationHandler


class EngineLoaderTests(unittest.TestCase):
    def test_vendored_package_is_discoverable(self):
        package_dir = configure_engine_import_path()

        self.assertIsNotNone(package_dir)
        assert package_dir is not None
        self.assertTrue((package_dir / "__init__.py").is_file())
        self.assertEqual(package_dir.name, "manga_translator")

    def test_diagnostics_identify_the_running_global_python(self):
        with (
            patch(
                "src.infrastructure.image_translation.engine_loader._SUPPORTED_PYTHON",
                set(),
            ),
            patch(
                "src.infrastructure.image_translation.engine_loader._REQUIRED_MODULES",
                {},
            ),
            patch("src.infrastructure.image_translation.engine_loader.importlib.import_module"),
        ):
            errors = diagnose_manga_engine()

        self.assertTrue(any(sys.executable in error for error in errors))


class _FakeImageService:
    def __init__(self):
        self.translate_calls = 0
        self.cancel_calls = []
        self.close_calls = []

    def make_request(self, **kwargs):
        return SimpleNamespace(**kwargs)

    def validate_request(self, _request):
        return []

    def translate(self, _request, _on_progress):
        self.translate_calls += 1
        return ImageTranslationResult(
            status=OperationStatus.SUCCEEDED,
            provider_id=ImageTranslationProviderId.MANGA,
            result_map={"images/a.png": "translated_images/manga/a.png"},
        )

    def cancel(self, provider_id):
        self.cancel_calls.append(provider_id)

    def close_provider(self, provider_id):
        self.close_calls.append(provider_id)


class ImageHandlerLifecycleTests(unittest.TestCase):
    def _make_handler(self, service):
        handler = ImageTranslationHandler.__new__(ImageTranslationHandler)
        handler.config_manager = SimpleNamespace(get_app_config=lambda: {"target_language": "中文"})
        handler.status_updater = lambda _message: None
        handler.image_progress_updater = lambda _message: None
        handler.get_mapping_dir = lambda: Path("mapping")
        handler.busy_state_updater = lambda _busy: None
        handler._service = service
        handler._worker_thread = None
        handler._safe_after = lambda callback: callback()
        return handler

    def test_provider_is_reused_across_consecutive_runs(self):
        service = _FakeImageService()
        handler = self._make_handler(service)

        with patch("src.ui.image_translation_handler.messagebox.showinfo"):
            handler._translation_worker(ImageTranslationProviderId.MANGA)
            handler._translation_worker(ImageTranslationProviderId.MANGA)

        self.assertEqual(service.translate_calls, 2)
        self.assertEqual(service.close_calls, [])

        handler.close()
        self.assertEqual(
            service.close_calls,
            list(ImageTranslationProviderId),
        )


if __name__ == "__main__":
    unittest.main()
