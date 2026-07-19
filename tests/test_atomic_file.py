#!/usr/bin/env python3
"""
R2-BUG-022：原子写入使用唯一临时文件

验证：
- 并发写入不会出现 FileNotFoundError 或半截 JSON
- 失败调用不会删除其他调用的临时文件
- 最终文件始终是某一次完整写入，而不是混合内容
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from src.utils.file_handler import write_json_atomic, write_text_atomic


class TestAtomicWriteUniqueTemp:
    """R2-BUG-022：唯一临时文件名避免并发冲突。"""

    def test_concurrent_writes_no_corruption(self, tmp_path):
        """20 个并发写入不会出现异常或半截内容。"""
        target = tmp_path / "shared.json"
        n_writers = 20
        errors = []

        def writer(i: int):
            try:
                payload = {"writer": i, "content": "x" * 1000}
                write_json_atomic(target, payload)
                return i
            except Exception as exc:  # noqa: BLE001
                errors.append((i, exc))
                return None

        with ThreadPoolExecutor(max_workers=n_writers) as pool:
            results = list(pool.map(writer, range(n_writers)))

        assert len(errors) == 0, f"并发写入出现异常: {errors}"

        # 最终文件应是某一次完整写入
        final = json.loads(target.read_text(encoding="utf-8"))
        assert final["writer"] in set(range(n_writers))
        assert final["content"] == "x" * 1000

    def test_no_leftover_temp_files(self, tmp_path):
        """写入完成后目录中不残留临时文件。"""
        target = tmp_path / "out.txt"
        write_text_atomic(target, "hello")

        siblings = list(tmp_path.iterdir())
        assert siblings == [target], f"残留临时文件: {siblings}"

    def test_failed_write_does_not_delete_others_temp(self, tmp_path):
        """一个写入失败不会删除其他并发写入的临时文件。"""
        target = tmp_path / "shared.txt"
        # 构造一个必然失败的写入目标：父路径是一个已存在的普通文件
        blocking_file = tmp_path / "blocker.txt"
        blocking_file.write_text("i-am-a-file", encoding="utf-8")
        failing_target = blocking_file / "child.txt"  # 父路径是文件，mkdir 必失败

        barrier = threading.Barrier(2)
        results = {}

        def slow_writer():
            barrier.wait()
            write_text_atomic(target, "slow-content", encoding="utf-8")
            results["slow"] = "ok"

        def failing_writer():
            barrier.wait()
            try:
                write_text_atomic(failing_target, "fail")
            except Exception:
                results["fail"] = "raised"
            else:
                results["fail"] = "no-error"

        t1 = threading.Thread(target=slow_writer)
        t2 = threading.Thread(target=failing_writer)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # 失败的写入应抛异常
        assert results.get("fail") == "raised", f"预期失败但得到: {results.get('fail')}"
        # 成功的写入应正常完成
        assert results.get("slow") == "ok"
        # 最终文件是慢写入的内容
        assert target.read_text(encoding="utf-8") == "slow-content"
        # 失败写入不应破坏 blocker 文件
        assert blocking_file.read_text(encoding="utf-8") == "i-am-a-file"

    def test_mixed_content_not_possible(self, tmp_path):
        """多次顺序写入，最终内容是最后一次完整写入。"""
        target = tmp_path / "seq.json"
        for i in range(10):
            write_json_atomic(target, {"i": i, "padding": str(i) * 500})

        final = json.loads(target.read_text(encoding="utf-8"))
        assert final["i"] == 9

    def test_json_atomic_unicode(self, tmp_path):
        """JSON 原子写入保留 Unicode 字符。"""
        target = tmp_path / "unicode.json"
        payload = {"text": "日本語テスト — emoji-free"}
        write_json_atomic(target, payload)

        raw = target.read_text(encoding="utf-8")
        assert "日本語テスト" in raw  # ensure_ascii=False
        assert json.loads(raw) == payload

    def test_atomic_write_calls_fsync(self, tmp_path):
        """P2-7：原子写入在 replace 前调用 fsync 刷盘。"""
        from unittest.mock import patch

        from src.infrastructure import atomic_file as atomic_mod

        target = tmp_path / "fsync_check.txt"
        fsync_calls: list[int] = []

        real_fsync = os.fsync

        def tracking_fsync(fd: int) -> None:
            fsync_calls.append(fd)
            real_fsync(fd)

        with patch.object(atomic_mod.os, "fsync", side_effect=tracking_fsync):
            write_text_atomic(target, "durable-content")

        assert target.read_text(encoding="utf-8") == "durable-content"
        assert fsync_calls, "原子写入必须调用 os.fsync 刷盘"

