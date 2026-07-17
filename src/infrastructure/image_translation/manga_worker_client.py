#!/usr/bin/env python3
"""Parent-side client for the Python 3.11 Manga translation worker."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from ...domain.errors import ImageTranslationConfigError
from ...domain.image_translation import (
    ImageTranslationProgress,
    ImageTranslationProviderId,
    ImageTranslationRequest,
    ImageTranslationResult,
)
from ...domain.translation import OperationStatus
from ...utils.logger import get_logger
from .engine_loader import resolve_manga_python

logger = get_logger(__name__)


class MangaWorkerClient:
    """Manage one long-lived 3.11 worker process.

    The worker communicates with newline-delimited JSON over stdin/stdout. Only
    JSON-compatible request metadata crosses the boundary; image bytes stay in
    the shared mapping directory and are read/written by the worker.
    """

    def __init__(
        self,
        config_manager,
        *,
        model_dir: Path | None = None,
        resource_dir: Path | None = None,
        font_path: Path | None = None,
        quality_preset: str = "standard",
        device: str = "auto",
        python_executable: str | None = None,
    ) -> None:
        self._config_manager = config_manager
        self._model_dir = Path(model_dir) if model_dir else None
        self._resource_dir = Path(resource_dir) if resource_dir else None
        self._font_path = str(font_path) if font_path else None
        self._quality_preset = quality_preset
        self._device = device
        self._python_executable = python_executable
        self._process: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._closed = False

    def validate(self, request: ImageTranslationRequest) -> list[str]:
        response = self._request(
            {
                "op": "health",
                "request": self._serialize_request(request),
                "api_config": self._api_config(),
            }
        )
        return [str(item) for item in response.get("errors", [])]

    def translate(
        self,
        request: ImageTranslationRequest,
        on_progress: Callable[[ImageTranslationProgress], None] | None = None,
    ) -> ImageTranslationResult:
        response = self._request(
            {
                "op": "translate",
                "request": self._serialize_request(request),
                "api_config": self._api_config(),
            },
            on_progress=on_progress,
        )
        payload = response.get("result") or {}
        return ImageTranslationResult(
            status=OperationStatus(payload.get("status", OperationStatus.FAILED.value)),
            result_map=dict(payload.get("result_map") or {}),
            skipped_images=list(payload.get("skipped_images") or []),
            failed_images=dict(payload.get("failed_images") or {}),
            provider_id=ImageTranslationProviderId.MANGA,
            run_id=str(payload.get("run_id") or ""),
        )

    def cancel(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        self._send({"op": "cancel"})

    def close(self) -> None:
        """P1-8：关闭 worker，不阻塞读取 stdout。

        旧实现直接 ``stdout.readline()`` 等待 worker 响应，worker 卡死时
        主线程会长期阻塞。新实现发送 ``close`` 后直接进入 terminate -> wait -> kill
        收敛流程，超时后强制结束。
        """
        if self._closed:
            return
        self._closed = True
        process = self._process
        self._process = None
        if process is None:
            return
        # P1-8：发送 close 后不等待响应，直接进入收敛流程
        try:
            self._send_to(process, {"op": "close"})
        except Exception:
            pass
        # P1-8：terminate -> wait(5s) -> kill 收敛，无残留子进程
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    logger.warning("Manga worker kill 后仍未退出")

    def _request(
        self,
        payload: dict,
        *,
        on_progress: Callable[[ImageTranslationProgress], None] | None = None,
    ) -> dict:
        if self._closed:
            raise ImageTranslationConfigError("Manga worker 已关闭")
        process = self._ensure_process()
        self._send_to(process, payload)
        while True:
            # 第三方 Manga 依赖可能向 stdout 混入本地代码页字节。
            # 协议本身是 UTF-8 JSON，非法字节替换后仍可继续解析合法响应，
            # 避免 UnicodeDecodeError 直接中断整个翻译任务。
            line = process.stdout.readline() if process.stdout else ""
            if not line:
                self._drop_process(process)
                raise ImageTranslationConfigError(
                    "Python 3.11 Manga worker 意外退出，请检查 worker 日志和依赖"
                )
            try:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ImageTranslationConfigError(f"Manga worker 返回了无效消息: {exc}") from exc
            if message.get("type") == "progress":
                if on_progress is not None:
                    on_progress(
                        ImageTranslationProgress(
                            stage=str(message.get("stage") or ""),
                            current=int(message.get("current") or 0),
                            total=int(message.get("total") or 0),
                            image_path=str(message.get("image_path") or ""),
                        )
                    )
                continue
            if message.get("type") == "error":
                raise ImageTranslationConfigError(str(message.get("message") or "worker error"))
            if message.get("type") == "response":
                return message

    def _ensure_process(self) -> subprocess.Popen:
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            executable = resolve_manga_python(self._python_executable)
            if executable is None:
                raise ImageTranslationConfigError(
                    "未找到 Python 3.11.9。请在图片翻译配置的 "
                    "python_executable 中填写 python.exe 路径，或设置 MANGA_PYTHON311"
                )
            root = self._resource_dir or Path(__file__).resolve().parents[3]
            env = os.environ.copy()
            pythonpath = [str(root), str(root / "third_party" / "manga-image-translator")]
            if env.get("PYTHONPATH"):
                pythonpath.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(pythonpath)
            # The JSON protocol must not inherit the Windows console code page.
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            process = subprocess.Popen(
                [
                    str(executable),
                    "-m",
                    "src.infrastructure.image_translation.manga_worker",
                    "--model-dir",
                    str(self._model_dir or ""),
                    "--resource-dir",
                    str(root),
                    "--font-path",
                    self._font_path or "",
                    "--quality-preset",
                    self._quality_preset,
                    "--device",
                    self._device,
                ],
                cwd=str(root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # 采用替换策略，防止第三方输出的本地代码页字节触发
                # 严格 UTF-8 解码异常；JSON 协议中的 ASCII 结构仍可解析。
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            # P1-8：启动 stderr reader 线程，有界、脱敏日志
            self._start_stderr_reader(process)
            self._process = process
            return process

    def _send(self, payload: dict) -> None:
        process = self._process
        if process is not None:
            self._send_to(process, payload)

    def _send_to(self, process: subprocess.Popen, payload: dict) -> None:
        if process.stdin is None:
            raise ImageTranslationConfigError("Manga worker stdin 不可用")
        with self._write_lock:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            try:
                process.stdin.write(line)
            except TypeError:
                # 兼容 bytes-mode 测试替身或外部调用者。
                process.stdin.write(line.encode("utf-8"))
            process.stdin.flush()

    def _drop_process(self, process: subprocess.Popen) -> None:
        if self._process is process:
            self._process = None
        if process.poll() is None:
            process.terminate()

    # P1-8：stderr 读取器，有界、脱敏日志
    #
    # 旧实现把 stderr 设为 DEVNULL，worker 卡死时缺少诊断信息。
    # 新实现使用 daemon 线程逐行读取 stderr，对每行做脱敏后输出到日志，
    # 并对总行数和单行长度做有界限制，避免恶意输出耗尽内存或日志空间。
    _STDERR_MAX_LINES = 500
    _STDERR_MAX_LINE_LENGTH = 4096
    _STDERR_SENSITIVE_TOKENS = (
        "api_key",
        "apikey",
        "bearer ",
        "authorization",
        "secret",
        "token",
    )

    def _start_stderr_reader(self, process: subprocess.Popen) -> None:
        """启动 daemon 线程读取 worker stderr，做有界、脱敏日志记录。"""
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return

        def _reader_loop() -> None:
            lines_read = 0
            try:
                for raw_line in stderr:
                    if lines_read >= self._STDERR_MAX_LINES:
                        logger.warning(
                            "Manga worker stderr 超过 %d 行上限，停止读取",
                            self._STDERR_MAX_LINES,
                        )
                        break
                    lines_read += 1
                    if isinstance(raw_line, bytes):
                        raw_line = raw_line.decode("utf-8", errors="replace")
                    # 去除尾部换行，避免日志里出现空行
                    line = raw_line.rstrip("\r\n")
                    if not line:
                        continue
                    # 单行长度截断
                    if len(line) > self._STDERR_MAX_LINE_LENGTH:
                        line = line[: self._STDERR_MAX_LINE_LENGTH] + "...(truncated)"
                    # 脱敏
                    sanitized = self._sanitize_stderr_line(line)
                    logger.debug("[manga-worker stderr] %s", sanitized)
            except Exception as exc:
                # 读取线程异常不得影响主流程
                logger.debug("Manga worker stderr 读取线程异常: %s", exc)

        thread = threading.Thread(
            target=_reader_loop,
            name="manga-worker-stderr-reader",
            daemon=True,
        )
        thread.start()

    @classmethod
    def _sanitize_stderr_line(cls, line: str) -> str:
        """对 stderr 单行做脱敏，避免凭据泄露到日志。"""
        lower = line.lower()
        for token in cls._STDERR_SENSITIVE_TOKENS:
            if token in lower:
                return "[filtered: contains sensitive token]"
        return line

    def _api_config(self) -> dict:
        return dict(self._config_manager.get_api_config())

    @staticmethod
    def _serialize_request(request: ImageTranslationRequest) -> dict:
        return {
            "mapping_dir": str(request.mapping_dir),
            "target_language": request.target_language,
            "selected_images": list(request.selected_images or []),
        }
