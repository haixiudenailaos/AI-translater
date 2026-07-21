import sys
import threading
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
from src.infrastructure.image_translation.runtime import MangaRuntime
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


class MangaRuntimeShutdownTests(unittest.TestCase):
    def test_shutdown_keeps_event_loop_open_while_thread_is_alive(self):
        """超时后不能关闭仍由后台线程拥有的 event loop。"""

        class _StoppedLoop:
            def __init__(self):
                self.close_called = False

            def is_running(self):
                return False

            def close(self):
                self.close_called = True

        class _AliveThread:
            def join(self, timeout=None):
                self.timeout = timeout

            def is_alive(self):
                return True

        runtime = MangaRuntime()
        loop = _StoppedLoop()
        thread = _AliveThread()
        runtime._loop = loop
        runtime._thread = thread
        runtime._started = True

        self.assertFalse(runtime.shutdown(timeout_seconds=0))
        self.assertFalse(loop.close_called)
        self.assertIs(runtime._loop, loop)
        self.assertIs(runtime._thread, thread)


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

    def close_provider(self, provider_id, *, timeout_seconds=None):
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

    def test_close_waits_for_cancelled_worker_before_releasing_providers(self):
        """关闭必须先取消并等待 worker，避免与 Provider teardown 并发。"""

        class _ClosingService(_FakeImageService):
            def __init__(self):
                super().__init__()
                self.cancelled = threading.Event()
                self.worker = None
                self.worker_alive_when_closed = []

            def cancel(self, provider_id):
                super().cancel(provider_id)
                self.cancelled.set()

            def close_provider(self, provider_id, *, timeout_seconds=None):
                self.worker_alive_when_closed.append(self.worker.is_alive())
                super().close_provider(provider_id, timeout_seconds=timeout_seconds)

        service = _ClosingService()
        handler = self._make_handler(service)
        worker = threading.Thread(target=lambda: service.cancelled.wait(), daemon=True)
        service.worker = worker
        handler._worker_thread = worker
        worker.start()

        handler.close(timeout_seconds=0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(service.close_calls, list(ImageTranslationProviderId))
        self.assertEqual(service.worker_alive_when_closed, [False, False])

    def test_close_does_not_release_provider_while_worker_is_still_running(self):
        """超过 deadline 时保留资源，不能关闭仍被 worker 使用的 Provider。"""

        service = _FakeImageService()
        handler = self._make_handler(service)
        release_worker = threading.Event()
        worker = threading.Thread(target=release_worker.wait, daemon=True)
        handler._worker_thread = worker
        worker.start()

        handler.close(timeout_seconds=0.01)

        self.assertTrue(worker.is_alive())
        self.assertEqual(service.close_calls, [])
        release_worker.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
