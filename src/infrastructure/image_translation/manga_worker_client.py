#!/usr/bin/env python3
"""Parent-side client for the Python 3.11 Manga translation worker."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
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

    # P1-3：默认请求 deadline。单张图片翻译含 OCR + 修复 + 渲染可能耗时数分钟，
    # 批量场景下 30 分钟足以覆盖正常用例，同时能兜住 worker 完全卡死的情况。
    _DEFAULT_DEADLINE_SECONDS = 1800.0
    # P1-3：health/validate 校验只读配置不调用模型，deadline 收紧。
    _HEALTH_DEADLINE_SECONDS = 30.0

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
        # The worker processes commands sequentially.  Serialise the complete
        # request/response exchange as well as writes; locking only stdin
        # writes lets concurrent callers consume and discard each other's
        # request-id messages from the shared stdout queue.
        self._request_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._closed = False
        # P1-3：stdout reader 线程 + 消息队列，替代阻塞 readline()。
        # reader 线程把每行 JSON 解析后塞进队列，_request 用 deadline 阻塞出队。
        # None 表示 EOF（worker 已退出）。
        self._stdout_queue: queue.Queue[dict | None] = queue.Queue()
        self._stdout_reader_thread: threading.Thread | None = None
        self._stderr_reader_thread: threading.Thread | None = None

    def validate(self, request: ImageTranslationRequest) -> list[str]:
        response = self._request(
            {
                "op": "health",
                "request": self._serialize_request(request),
                "api_config": self._api_config(),
            },
            deadline_seconds=self._HEALTH_DEADLINE_SECONDS,
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
            deadline_seconds=self._DEFAULT_DEADLINE_SECONDS,
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
        # Snapshot the process under the same lock used by close/startup.  A
        # cancellation request is intentionally not serialized behind the
        # long-running request lock, otherwise it could not interrupt a
        # translation that is waiting for worker progress.
        with self._process_lock:
            process = self._process
            if self._closed or process is None or process.poll() is not None:
                return
        try:
            self._send_to(process, {"op": "cancel"})
        except (BrokenPipeError, OSError, ImageTranslationConfigError):
            # close()/worker exit can race cancellation; the operation is
            # already converging and there is nothing useful to propagate.
            logger.debug("Manga worker 取消请求未发送（进程正在关闭）")

    def close(self, *, timeout_seconds: float | None = None) -> bool:
        """P1-8：关闭 worker，不阻塞读取 stdout。

        旧实现直接 ``stdout.readline()`` 等待 worker 响应，worker 卡死时
        主线程会长期阻塞。新实现发送 ``close`` 后直接进入 terminate -> wait -> kill
        收敛流程，超时后强制结束。
        """
        # Mark closed and detach the process atomically with respect to
        # request startup.  Otherwise a request can pass its closed check,
        # observe the process, and write to stdin while close is tearing it
        # down.
        deadline = (
            time.monotonic() + max(0.0, timeout_seconds) if timeout_seconds is not None else None
        )
        with self._process_lock:
            if self._closed:
                return True
            self._closed = True
            process = self._process
            self._process = None
        if process is None:
            return True
        # P1-8：发送 close 后不等待响应，直接进入收敛流程
        try:
            self._send_to(process, {"op": "close"})
        except Exception:
            pass
        # P1-8：terminate -> wait(5s) -> kill 收敛，无残留子进程
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self._remaining_timeout(deadline, 5.0))
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=self._remaining_timeout(deadline, 2.0))
                except subprocess.TimeoutExpired:
                    logger.warning("Manga worker kill 后仍未退出")
        # P1-3：清空队列残留，避免内存泄漏
        self._close_process_streams(process)
        self._join_reader_threads(deadline=deadline)
        self._drain_stdout_queue()
        return process.poll() is not None

    def _request(
        self,
        payload: dict,
        *,
        on_progress: Callable[[ImageTranslationProgress], None] | None = None,
        deadline_seconds: float,
    ) -> dict:
        """P1-3：发送请求并等待响应，带 deadline 和 request_id 配对。

        - ``request_id`` 随 payload 发送给 worker，worker 在所有相关
          response/progress/error 消息中回传，客户端按 ID 过滤迟到事件。
        - deadline 是"静默期"：每收到一条消息（progress 或 response）
          就重置倒计时。若 ``deadline_seconds`` 内无任何消息，判定为
          worker 卡死，丢弃进程并抛出超时错误。
        """
        with self._request_lock:
            if self._closed:
                raise ImageTranslationConfigError("Manga worker 已关闭")
            process = self._ensure_process()
            # P1-3：每个请求分配唯一 request_id，用于配对响应、丢弃迟到事件
            request_id = f"req-{time.monotonic_ns()}"
            payload = {**payload, "request_id": request_id}
            self._send_to(process, payload)

            deadline = time.monotonic() + deadline_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._drop_process(process)
                    raise ImageTranslationConfigError(
                        f"Manga worker 静默超过 {deadline_seconds:.0f}s，已终止进程"
                    )
                try:
                    message = self._stdout_queue.get(timeout=remaining)
                except queue.Empty:
                    self._drop_process(process)
                    raise ImageTranslationConfigError(
                        f"Manga worker 静默超过 {deadline_seconds:.0f}s，已终止进程"
                    )
                if message is None:
                    # reader 线程遇到 EOF：worker 已退出
                    self._drop_process(process)
                    raise ImageTranslationConfigError(
                        "Python 3.11 Manga worker 意外退出，请检查 worker 日志和依赖"
                    )
                # P1-3：request_id 配对——丢弃迟到/不匹配的消息
                msg_request_id = message.get("request_id")
                if msg_request_id and msg_request_id != request_id:
                    logger.debug(
                        "丢弃迟到消息 request_id=%s (current=%s, type=%s)",
                        msg_request_id,
                        request_id,
                        message.get("type"),
                    )
                    # 不重置 deadline（迟到消息不算活动信号）
                    continue
                # 收到匹配消息，重置静默倒计时
                deadline = time.monotonic() + deadline_seconds
                mtype = message.get("type")
                if mtype == "progress":
                    if on_progress is not None:
                        try:
                            on_progress(
                                ImageTranslationProgress(
                                    stage=str(message.get("stage") or ""),
                                    current=int(message.get("current") or 0),
                                    total=int(message.get("total") or 0),
                                    image_path=str(message.get("image_path") or ""),
                                )
                            )
                        except Exception as exc:
                            logger.debug("on_progress 回调异常: %s", exc)
                    continue
                if mtype == "error":
                    raise ImageTranslationConfigError(str(message.get("message") or "worker error"))
                if mtype == "response":
                    return message
                # 未知消息类型：忽略，继续等待
                logger.debug("未知 worker 消息类型: %s", mtype)

    def _ensure_process(self) -> subprocess.Popen:
        with self._process_lock:
            if self._closed:
                raise ImageTranslationConfigError("Manga worker 已关闭")
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
            # P1-3：启动 stdout reader 线程，把行 JSON 入队供 _request 消费
            self._start_stdout_reader(process)
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
        with self._process_lock:
            if self._process is process:
                self._process = None
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
        self._close_process_streams(process)
        self._join_reader_threads()
        self._drain_stdout_queue()

    @staticmethod
    def _close_process_streams(process: subprocess.Popen) -> None:
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    @staticmethod
    def _remaining_timeout(deadline: float | None, default: float) -> float:
        if deadline is None:
            return default
        return max(0.0, min(default, deadline - time.monotonic()))

    def _join_reader_threads(self, *, deadline: float | None = None) -> None:
        current = threading.current_thread()
        for attribute in ("_stdout_reader_thread", "_stderr_reader_thread"):
            thread = getattr(self, attribute, None)
            if thread is not None and thread is not current:
                thread.join(timeout=self._remaining_timeout(deadline, 1.0))
            setattr(self, attribute, None)

    def _drain_stdout_queue(self) -> None:
        """非阻塞清空 stdout 队列中的残留消息。"""
        while True:
            try:
                self._stdout_queue.get_nowait()
            except queue.Empty:
                break

    # P1-3：stdout reader 线程
    #
    # 旧实现 ``_request`` 在主线程内直接 ``readline()``，无 deadline。
    # worker 卡死时主线程永久阻塞，且 Windows 上无法用 select 等待管道。
    # 新实现用独立 daemon 线程逐行读取 stdout，解析 JSON 后入队，
    # ``_request`` 用 ``queue.get(timeout=deadline)`` 实现 deadline。
    _STDOUT_MAX_LINE_LENGTH = 65536

    def _start_stdout_reader(self, process: subprocess.Popen) -> None:
        """启动 daemon 线程读取 worker stdout，解析 JSON 后入队。"""
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            return

        def _reader_loop() -> None:
            try:
                for raw_line in stdout:
                    if isinstance(raw_line, bytes):
                        raw_line = raw_line.decode("utf-8", errors="replace")
                    line = raw_line.rstrip("\r\n")
                    if not line:
                        continue
                    # 单行长度截断，防止恶意超长行耗尽内存
                    if len(line) > self._STDOUT_MAX_LINE_LENGTH:
                        line = line[: self._STDOUT_MAX_LINE_LENGTH]
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError as exc:
                        # 协议错误：作为 error 消息入队，让 _request 处理
                        logger.warning("Manga worker stdout 非 JSON 行: %s", line[:200])
                        self._stdout_queue.put(
                            {
                                "type": "error",
                                "message": f"Manga worker 返回了无效消息: {exc}",
                            }
                        )
                        continue
                    self._stdout_queue.put(message)
                # 循环结束 = EOF（worker 已退出）
                self._stdout_queue.put(None)
            except Exception as exc:
                logger.debug("Manga worker stdout 读取线程异常: %s", exc)
                self._stdout_queue.put(None)

        thread = threading.Thread(
            target=_reader_loop,
            name="manga-worker-stdout-reader",
            daemon=True,
        )
        self._stdout_reader_thread = thread
        thread.start()

    # P1-8：stderr 读取器，有界、脱敏日志
    #
    # 旧实现把 stderr 设为 DEVNULL，worker 卡死时缺少诊断信息。
    # 新实现使用 daemon 线程逐行读取 stderr，对每行做脱敏后输出到日志，
    # 并对记录的日志行数和单行长度做有界限制，避免恶意输出耗尽内存或日志空间。
    # 即使日志记录已到上限，仍必须持续 drain stderr；停止读取会让子进程在
    # Windows 管道写满时永久阻塞。
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
                            "Manga worker stderr 超过 %d 行上限，后续输出将被丢弃",
                            self._STDERR_MAX_LINES,
                        )
                        # 继续读取并丢弃，保证子进程 stderr 管道始终可写。
                        for _discarded_line in stderr:
                            pass
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
        self._stderr_reader_thread = thread
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
