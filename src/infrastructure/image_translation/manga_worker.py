#!/usr/bin/env python3
"""Python 3.11 entry point for the isolated Manga image translator."""

from __future__ import annotations

import argparse
import contextlib
import json
import queue
import sys
import threading
from pathlib import Path

from ...domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
)
from .manga_provider import _LocalMangaImageTranslationProvider


def _configure_utf8_stdio() -> None:
    """Keep the worker JSON protocol independent of the Windows code page."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")


_configure_utf8_stdio()
_OUTPUT = sys.stdout


class _WorkerConfigManager:
    def __init__(self) -> None:
        self.api_config: dict = {}

    def get_api_config(self):
        return dict(self.api_config)


def _send(payload: dict) -> None:
    _OUTPUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _OUTPUT.flush()


def _request_from_payload(payload: dict) -> ImageTranslationRequest:
    return ImageTranslationRequest(
        mapping_dir=Path(payload["mapping_dir"]),
        target_language=str(payload["target_language"]),
        provider_id=ImageTranslationProviderId.MANGA,
        selected_images=tuple(payload.get("selected_images") or []) or None,
    )


def _result_payload(result) -> dict:
    return {
        "status": result.status.value,
        "result_map": result.result_map,
        "skipped_images": result.skipped_images,
        "failed_images": result.failed_images,
        "run_id": result.run_id,
    }


def run_worker(args: argparse.Namespace) -> int:
    config_manager = _WorkerConfigManager()
    provider = _LocalMangaImageTranslationProvider(
        config_manager,
        model_dir=Path(args.model_dir) if args.model_dir else None,
        resource_dir=Path(args.resource_dir) if args.resource_dir else None,
        font_path=Path(args.font_path) if args.font_path else None,
        quality_preset=args.quality_preset,
        device=args.device,
    )
    commands: queue.Queue[dict] = queue.Queue()
    active = {"provider": None}
    active_lock = threading.Lock()

    def read_commands() -> None:
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                _send({"type": "error", "message": "worker 收到无效 JSON 命令"})
                continue
            if command.get("op") == "cancel":
                with active_lock:
                    current = active["provider"]
                if current is not None:
                    current.cancel()
            else:
                commands.put(command)

    reader = threading.Thread(target=read_commands, name="manga-worker-stdin", daemon=True)
    reader.start()
    try:
        while True:
            command = commands.get()
            operation = command.get("op")
            if operation == "close":
                _send({"type": "response", "ok": True})
                return 0

            config_manager.api_config = dict(command.get("api_config") or {})
            if operation == "health":
                errors = provider.validate(_request_from_payload(command["request"]))
                _send({"type": "response", "errors": errors})
                continue

            if operation != "translate":
                _send({"type": "error", "message": f"未知 worker 操作: {operation}"})
                continue

            request = _request_from_payload(command["request"])

            def on_progress(progress: ImageTranslationProgress) -> None:
                _send(
                    {
                        "type": "progress",
                        "stage": progress.stage,
                        "current": progress.current,
                        "total": progress.total,
                        "image_path": progress.image_path,
                    }
                )

            with active_lock:
                active["provider"] = provider
            try:
                # Upstream writes diagnostic output to stdout. Redirect it so the
                # JSON protocol remains parseable by the parent process.
                with contextlib.redirect_stdout(sys.stderr):
                    result = provider.translate(request, on_progress)
                _send({"type": "response", "result": _result_payload(result)})
            except Exception as exc:
                _send({"type": "error", "message": provider._sanitize_error(str(exc))})
            finally:
                with active_lock:
                    active["provider"] = None
    finally:
        provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manga Python 3.11 worker")
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--resource-dir", default="")
    parser.add_argument("--font-path", default="")
    parser.add_argument("--quality-preset", default="standard")
    parser.add_argument("--device", default="auto")
    return run_worker(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
