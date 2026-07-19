"""Small stdlib-only helpers for durable infrastructure metadata writes.

P3-1：统一的原子写入实现。文本、JSON 和 bytes 使用同一个临时文件与
replace 策略，Windows 瞬态 PermissionError 通过有限指数退避重试规避。

utils/file_handler.py 的 write_text_atomic / write_json_atomic 现委托到此模块，
项目中只有这一个原子写入实现。
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# Windows 上并发 os.replace 可能因目标文件被短暂占用而返回 PermissionError，
# 属于瞬态错误，短暂退避后重试即可成功（R2-BUG-022 并发安全）。
_REPLACE_RETRY_COUNT = 8
_REPLACE_RETRY_BASE_DELAY = 0.005  # 5ms 起步，指数退避


def _atomic_replace(src: Path, dst: Path) -> None:
    """执行 os.replace，并在 Windows 瞬态 PermissionError 上有限重试。

    FileNotFoundError（源临时文件被其他写入者删除）属于冲突，不重试。
    """
    last_exc: Exception | None = None
    for attempt in range(_REPLACE_RETRY_COUNT):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError as exc:
            # Windows：目标文件被其他线程/进程短暂占用
            last_exc = exc
            time.sleep(_REPLACE_RETRY_BASE_DELAY * (2**attempt))
        except FileNotFoundError as exc:
            # 源临时文件被其他写入者删除，属于冲突，不重试
            last_exc = exc
            break
    # 重试耗尽，抛出最后一次异常
    raise last_exc  # type: ignore[misc]


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Write bytes through a unique sibling temporary file before replacement.

    P3-1：统一使用 _atomic_replace 处理 Windows 瞬态占用。
    P2-7：在 replace 前对临时文件执行 flush+fsync，把页缓存刷到磁盘，
    使“原子写入”在普通断电场景下也尽量可恢复；fsync 失败不静默吞掉。
    失败时清理临时文件并抛出异常，不静默吞掉。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, content: str, encoding: str = "utf-8") -> None:
    """原子写入文本文件。

    P3-1：统一实现，委托到 write_bytes_atomic，共享 Windows 重试策略。
    临时文件与目标文件位于同一目录，保证 os.replace() 尽量原子化。
    失败时直接抛出异常，由调用方（UI 边界）负责展示。
    """
    write_bytes_atomic(Path(path), content.encode(encoding))


def write_json_atomic(path: Path, payload: Any) -> None:
    """Serialize JSON to a unique sibling temp file, then atomically replace it.

    P3-1：统一实现，ensure_ascii=False, indent=2。
    """
    write_bytes_atomic(
        Path(path),
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
