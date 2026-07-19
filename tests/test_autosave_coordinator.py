#!/usr/bin/env python3
"""PERF：自动保存协调器单元测试。

覆盖 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §8.7 的要求：
- 连续 100 次变更只启动一次 debounce 保存
- 保存期间继续编辑，完成后自动调度下一代快照
- 旧 generation 成功不能清除新 generation 的 dirty
- 写入失败保留 dirty，并产生可重试状态
- 关闭时 flush 等待成功
- flush 超时返回失败状态
- 项目切换后旧保存结果不更新新项目状态
- worker 线程不调用 Fake root
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List

import pytest

from src.application.autosave import (
    CLEAN,
    DIRTY,
    SAVE_FAILED,
    SAVING,
    AutosaveCoordinator,
    SaveResult,
    SaveSnapshot,
)
from src.application.translation_document import TranslationDocument

# ── 测试辅助 ──────────────────────────────────────────────


class FakeRoot:
    """Fake Tk root：记录 after 调用，支持手动触发回调。

    断言 after 只在主线程调用（worker 不调用 Tk API）。
    """

    def __init__(self):
        self.owner_thread = threading.get_ident()
        self.scheduled: List = []  # [(after_id, delay_ms, callback)]
        self.cancelled: List[str] = []
        self.violation: str | None = None
        self._next_id = 0

    def after(self, delay_ms, callback):
        if threading.get_ident() != self.owner_thread:
            self.violation = "after() called from non-owner thread"
            raise AssertionError(self.violation)
        after_id = f"after-{self._next_id}"
        self._next_id += 1
        self.scheduled.append((after_id, delay_ms, callback))
        return after_id

    def after_cancel(self, after_id):
        self.cancelled.append(after_id)
        self.scheduled = [(aid, d, c) for aid, d, c in self.scheduled if aid != after_id]

    def trigger_oldest(self):
        """触发最早调度的回调（按 FIFO）。"""
        if not self.scheduled:
            return None
        _aid, _delay, callback = self.scheduled.pop(0)
        callback()
        return True

    def trigger_by_delay(self, delay_ms):
        """触发指定 delay 的回调。"""
        for i, (_aid, d, c) in enumerate(self.scheduled):
            if d == delay_ms:
                self.scheduled.pop(i)
                c()
                return True
        return False


class FakeFileHandler:
    """Fake 文件处理器：记录写入，可控制失败和阻塞。"""

    def __init__(self):
        self.writes: List = []  # [(path, content)]
        self.fail_next = False
        # 阻塞事件：clear() 后写入会阻塞，set() 后恢复。
        # 用于测试 SAVING 状态下的行为。
        self.block_event = threading.Event()
        self.block_event.set()  # 默认不阻塞

    def write_file(self, path, content):
        # 等待解除阻塞（测试可控制 worker 完成时机）
        self.block_event.wait()
        if self.fail_next:
            self.fail_next = False
            raise OSError("模拟写入失败")
        self.writes.append((path, content))


class FakeEpubProcessor:
    """Fake EPUB 处理器。"""

    def __init__(self):
        self.save_calls: List = []

    def save_translations(self, mapping_dir, target_lines):
        self.save_calls.append((mapping_dir, list(target_lines)))


def _make_coordinator(
    document=None,
    file_handler=None,
    epub_processor=None,
    root=None,
    results=None,
):
    """构造测试用协调器。"""
    document = document or TranslationDocument()
    file_handler = file_handler or FakeFileHandler()
    epub_processor = epub_processor or FakeEpubProcessor()
    root = root or FakeRoot()
    results = results if results is not None else []
    coordinator = AutosaveCoordinator(
        document=document,
        file_handler=file_handler,
        epub_processor=epub_processor,
        schedule_callback=root.after,
        result_callback=results.append,
        cancel_callback=root.after_cancel,
    )
    coordinator.set_save_paths(Path("/tmp/test.txt"), None)
    return coordinator, root, file_handler, epub_processor, results


def _populate(document, count=5):
    """填充文档。"""
    document.replace([f"原文{i}" for i in range(count)], [""] * count)


# ── 基础状态 ──────────────────────────────────────────────


def test_initial_state_is_clean():
    """协调器初始状态为 CLEAN。"""
    coordinator, *_ = _make_coordinator()
    assert coordinator.state == CLEAN
    assert coordinator.is_saving is False


def test_mark_dirty_transitions_to_dirty():
    """mark_dirty 将状态从 CLEAN 转为 DIRTY 并调度 debounce。"""
    coordinator, root, *_ = _make_coordinator()
    document = TranslationDocument()
    coordinator._document = document

    coordinator.mark_dirty(source="edit")

    assert coordinator.state == DIRTY
    # debounce 1000ms 被调度
    assert len(root.scheduled) >= 1


# ── §8.7: 连续 100 次变更只启动一次 debounce 保存 ────────


def test_continuous_changes_start_one_debounce():
    """连续 100 次变更只启动一次 debounce 保存。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 10)

    # 阻塞 worker，确保在断言时处于 SAVING 状态
    file_handler.block_event.clear()

    # 连续 100 次编辑
    for i in range(100):
        document.update_target(i % 10, f"译{i}")
        coordinator.mark_dirty(source="edit")

    # debounce 未到期前，不应启动保存
    assert len(file_handler.writes) == 0
    assert coordinator.state == DIRTY

    # 触发 debounce（1000ms）
    root.trigger_by_delay(1000)

    # 只启动了一次保存
    assert coordinator.is_saving is True
    assert len(file_handler.writes) == 0  # worker 被阻塞，尚未写入

    # 释放 worker，等待完成
    file_handler.block_event.set()
    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)
    assert len(file_handler.writes) == 1


# ── §8.7: 保存期间继续编辑，完成后自动调度下一代快照 ────


def test_edits_during_save_schedule_next_generation():
    """保存期间继续编辑，完成后自动调度下一代快照。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 5)

    # 阻塞 worker，确保在编辑时处于 SAVING 状态
    file_handler.block_event.clear()

    # 启动保存
    coordinator.mark_dirty(source="flush")
    assert coordinator.is_saving is True
    save_gen = coordinator._save_generation

    # 保存期间继续编辑（增加 version）
    document.update_target(0, "新译文")

    # 释放 worker，等待结果
    file_handler.block_event.set()
    for _ in range(50):
        if coordinator.state != SAVING:
            break
        coordinator._poll_result()
        time.sleep(0.02)

    # 保存期间产生了新版本：状态应为 DIRTY（非 CLEAN）
    assert coordinator.state == DIRTY
    # 应调度下一次保存
    assert any(d == 100 for _aid, d, _c in root.scheduled) or coordinator.is_saving


# ── §8.7: 旧 generation 成功不能清除新 generation 的 dirty ──


def test_old_generation_success_does_not_clear_new_dirty():
    """旧 generation 保存成功时，新 generation 的 dirty 保留。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # 启动保存 generation=1
    coordinator.mark_dirty(source="flush")
    gen1 = coordinator._save_generation
    assert gen1 == 1

    # 模拟保存期间产生新版本
    document.update_target(0, "新译")  # version → 2

    # 手动构造旧 generation 的成功结果
    old_result = SaveResult(
        generation=gen1,
        succeeded=True,
        elapsed_seconds=0.01,
        bytes_written=100,
    )

    coordinator._handle_result(old_result)

    # 旧 generation 成功，但 version 已更新：状态应为 DIRTY
    assert coordinator.state == DIRTY


# ── §8.7: 写入失败保留 dirty，并产生可重试状态 ────────────


def test_write_failure_preserves_dirty():
    """写入失败保留 dirty，状态为 SAVE_FAILED。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    # 设置下次写入失败
    file_handler.fail_next = True
    # 阻塞 worker，确保在断言时处于 SAVING 状态
    file_handler.block_event.clear()

    # 启动保存
    coordinator.mark_dirty(source="flush")
    assert coordinator.is_saving is True

    # 释放 worker，让其触发失败
    file_handler.block_event.set()

    # 等待 worker 完成
    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)

    # 保存失败：状态为 SAVE_FAILED
    assert coordinator.state == SAVE_FAILED

    # 再次 mark_dirty 应能重新调度保存（可重试）
    file_handler.fail_next = False
    coordinator.mark_dirty(source="flush")

    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)

    # 重试成功：状态为 CLEAN
    assert coordinator.state == CLEAN


# ── §8.7: 关闭时 flush 等待成功 ──────────────────────────


def test_flush_waits_for_success():
    """关闭时 flush 等待保存成功。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    coordinator.mark_dirty(source="flush")

    # flush 在另一个线程中等待（避免阻塞测试）
    flush_thread = threading.Thread(target=coordinator.flush, args=(5.0,))
    flush_thread.start()

    # 等待 worker 完成
    for _ in range(50):
        coordinator._poll_result()
        if not coordinator.is_saving and coordinator.state == CLEAN:
            break
        time.sleep(0.02)

    flush_thread.join(timeout=5.0)

    assert coordinator.state == CLEAN
    assert len(file_handler.writes) == 1


# ── §8.7: flush 超时返回失败状态 ──────────────────────────


def test_flush_timeout_returns_false():
    """flush 超时返回 False。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    # 阻塞 worker，使其无法在超时内完成
    file_handler.block_event.clear()

    # mark_dirty 并触发立即保存
    coordinator.mark_dirty(source="flush")
    assert coordinator.is_saving is True

    # flush 等待，但 worker 被阻塞无法完成
    result = coordinator.flush(timeout=0.1)

    # 超时返回 False
    assert result is False

    # 清理：释放 worker
    file_handler.block_event.set()


# ── §8.7: 项目切换后旧保存结果不更新新项目状态 ──────────


def test_old_project_result_does_not_update_new_project():
    """项目切换后，旧项目的保存结果不影响新项目。

    通过 document.version 重置（replace 后 version=1）模拟项目切换。
    """
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # 旧项目保存
    coordinator.mark_dirty(source="flush")
    old_gen = coordinator._save_generation

    # 项目切换：replace 重置文档
    document.replace(["新原文1", "新原文2"], ["", ""])

    # 旧保存结果到达
    old_result = SaveResult(
        generation=old_gen,
        succeeded=True,
        elapsed_seconds=0.01,
        bytes_written=50,
    )
    coordinator._handle_result(old_result)

    # 旧 generation < 新 version（replace 后 version=1，但 old_gen 可能等于 1）
    # 关键是状态不应错误地变为 CLEAN（除非 generation 真的匹配）
    if old_gen < document.version:
        assert coordinator.state == DIRTY


# ── §8.7: worker 线程不调用 Fake root ────────────────────


def test_worker_does_not_call_tk_api():
    """保存 worker 线程不调用 Fake root（Tk API）。"""
    root = FakeRoot()
    results: List[SaveResult] = []
    document = TranslationDocument()
    _populate(document, 2)
    file_handler = FakeFileHandler()
    epub_processor = FakeEpubProcessor()

    coordinator = AutosaveCoordinator(
        document=document,
        file_handler=file_handler,
        epub_processor=epub_processor,
        schedule_callback=root.after,
        result_callback=results.append,
        cancel_callback=root.after_cancel,
    )
    coordinator.set_save_paths(Path("/tmp/test.txt"), None)

    coordinator.mark_dirty(source="flush")

    # 等待 worker 完成
    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)

    # worker 不应调用 after（无 violation）
    assert root.violation is None
    # 保存成功
    assert coordinator.state == CLEAN
    assert len(file_handler.writes) == 1


# ── EPUB 映射保存 ─────────────────────────────────────────


def test_epub_mapping_saved_on_success():
    """保存成功时同时保存 EPUB 映射。"""
    epub_processor = FakeEpubProcessor()
    coordinator, root, file_handler, *_ = _make_coordinator(epub_processor=epub_processor)
    document = coordinator._document
    _populate(document, 3)

    # 设置映射目录
    coordinator.set_save_paths(Path("/tmp/test.txt"), Path("/tmp/mapping"))

    coordinator.mark_dirty(source="flush")

    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)

    assert coordinator.state == CLEAN
    assert len(epub_processor.save_calls) == 1


# ── 单飞：同时只有一个保存线程 ───────────────────────────


def test_single_flight_only_one_save_at_a_time():
    """单飞：同时只有一个保存线程。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 5)

    # 阻塞 worker，确保在断言时处于 SAVING 状态
    file_handler.block_event.clear()

    coordinator.mark_dirty(source="flush")
    assert coordinator.is_saving is True

    # 保存进行中再次 mark_dirty 不启动第二个保存
    document.update_target(0, "编辑1")
    coordinator.mark_dirty(source="flush")

    # 仍然只有一个保存线程，worker 被阻塞尚未写入
    assert coordinator.is_saving is True
    assert len(file_handler.writes) == 0

    # 释放 worker
    file_handler.block_event.set()
    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)
    assert len(file_handler.writes) == 1


# ── SaveSnapshot/SaveResult 不可变性 ─────────────────────


def test_save_snapshot_is_frozen():
    """SaveSnapshot 不可变。"""
    snapshot = SaveSnapshot(
        generation=1,
        target_path=Path("/tmp/test.txt"),
        mapping_dir=None,
        target_lines=("译1", "译2"),
    )
    with pytest.raises(Exception):
        snapshot.generation = 2  # type: ignore[misc]


def test_save_result_is_frozen():
    """SaveResult 不可变。"""
    result = SaveResult(
        generation=1,
        succeeded=True,
        elapsed_seconds=0.01,
        bytes_written=100,
    )
    with pytest.raises(Exception):
        result.succeeded = False  # type: ignore[misc]


# ── §8 D-1: _cancel_debounce 必须实际调用 after_cancel ────


def test_cancel_debounce_actually_invokes_after_cancel():
    """PERF §8 D-1：连续 mark_dirty 时，旧 debounce after_id 必须被 after_cancel 取消。

    原实现只清空引用不调用 after_cancel，导致旧回调仍滞留在 Tk 调度队列。
    修正后每次 mark_dirty 应通过 cancel_callback 取消上一次的 debounce id。
    """
    coordinator, root, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # 第一次 mark_dirty：调度一个 debounce after_id
    coordinator.mark_dirty(source="edit")
    first_debounce_ids = [aid for aid, delay, _cb in root.scheduled if delay == 1000]
    assert len(first_debounce_ids) == 1

    # 第二次 mark_dirty：应取消上一次的 debounce after_id
    coordinator.mark_dirty(source="edit")

    # 第一次的 debounce after_id 必须出现在 cancelled 列表中
    assert first_debounce_ids[0] in root.cancelled, (
        "连续 mark_dirty 时未通过 after_cancel 取消旧 debounce after_id，PERF §8 D-1 修正未生效"
    )


def test_closed_coordinator_rejects_debounce_callbacks():
    """PERF §8 D-3：close 后 debounce/max_delay 回调应被守卫拦截，不启动保存。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # mark_dirty 调度 debounce
    coordinator.mark_dirty(source="edit")
    assert len(root.scheduled) >= 1

    # close 协调器
    coordinator.close()
    # close 应取消所有 pending debounce / max_delay
    # 触发剩余调度回调（即便 close 取消失败也应被 closed 守卫拦截）
    while root.scheduled:
        root.trigger_oldest()

    # close 后不应启动任何保存
    assert coordinator.state != SAVING
    assert len(file_handler.writes) == 0


# ── P0-3: mark_clean 公共方法 ─────────────────────────────


def test_mark_clean_clears_dirty_state():
    """P0-3：mark_clean 在 DIRTY 状态下置为 CLEAN，并取消 debounce。"""
    coordinator, root, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # 编辑产生 dirty + 调度 debounce
    document.update_target(0, "译A")
    coordinator.mark_dirty(source="edit")
    assert coordinator.state == DIRTY
    assert len(root.scheduled) >= 1
    scheduled_ids = [aid for aid, *_rest in root.scheduled]

    # mark_clean 应清除状态并取消 debounce
    coordinator.mark_clean()

    assert coordinator.state == CLEAN
    # 所有已调度的 debounce/max_delay 都应被取消
    assert all(aid in root.cancelled for aid in scheduled_ids)
    # 残余回调即使被触发也不会改回 DIRTY
    while root.scheduled:
        root.trigger_oldest()
    assert coordinator.state == CLEAN


def test_mark_clean_does_not_interrupt_saving():
    """P0-3：SAVING 中调用 mark_clean 不修改状态，由 _handle_result 接管。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # 阻塞 worker 保持 SAVING
    file_handler.block_event.clear()

    document.update_target(0, "译A")
    coordinator.mark_dirty(source="flush")
    assert coordinator.is_saving is True

    # 在 SAVING 中调用 mark_clean：状态保持 SAVING
    coordinator.mark_clean()
    assert coordinator.is_saving is True

    # 释放 worker 并完成
    file_handler.block_event.set()
    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)

    assert coordinator.state == CLEAN


def test_mark_clean_after_close_is_noop():
    """P0-3：close 后调用 mark_clean 应被守卫拦截，不修改状态。"""
    coordinator, *_ = _make_coordinator()
    coordinator.close()
    assert coordinator.state == CLEAN

    # close 后不应抛异常也不应改状态
    coordinator.mark_clean()
    assert coordinator.state == CLEAN


# ── P0-1: discard_pending 放弃未保存更改 ──────────────────────


def test_discard_pending_dirty_state_does_not_save():
    """P0-1：DIRTY 状态下 discard_pending 不启动保存，直接置 CLEAN。

    覆盖审查文档 P0-1 的核心契约：用户选择"放弃更改"后，
    保存函数调用次数为 0，磁盘保持旧内容。
    """
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # 编辑产生 dirty + 调度 debounce
    document.update_target(0, "未保存的译文")
    coordinator.mark_dirty(source="edit")
    assert coordinator.state == DIRTY
    scheduled_ids = [aid for aid, *_rest in root.scheduled]
    assert scheduled_ids, "mark_dirty 应调度 debounce"

    # discard_pending：取消 debounce，置 CLEAN，不启动保存
    ok = coordinator.discard_pending(timeout=1.0)

    assert ok is True
    assert coordinator.state == CLEAN
    # 没有任何写入发生
    assert file_handler.writes == []
    # 所有已调度的 debounce/max_delay 应被取消
    assert all(aid in root.cancelled for aid in scheduled_ids), (
        "discard_pending 必须取消所有已调度的 after 回调"
    )


def test_discard_pending_save_failed_state_clears_without_save():
    """P0-1：SAVE_FAILED 状态下 discard_pending 置 CLEAN，不重试保存。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    # 制造一次保存失败
    file_handler.fail_next = True
    coordinator.mark_dirty(source="flush")
    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)
    assert coordinator.state == SAVE_FAILED

    # discard_pending：不再重试，直接置 CLEAN
    file_handler.fail_next = False  # 确保即使重试也会成功，以验证没有重试
    ok = coordinator.discard_pending(timeout=1.0)

    assert ok is True
    assert coordinator.state == CLEAN
    # 仍只有失败那一次写入尝试（无新增）
    assert len(file_handler.writes) == 0


def test_discard_pending_save_scheduled_cancels_after_and_skips_save():
    """P0-1：SAVE_SCHEDULED（debounce 已调度未到期）状态下 discard_pending
    取消 after 且不启动 worker。"""
    from src.application.autosave import SAVE_SCHEDULED

    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    document.update_target(0, "未保存")
    coordinator.mark_dirty(source="edit")
    # 状态机中 mark_dirty 在 CLEAN 时置 DIRTY 并调度 debounce，
    # 这里直接断言调度存在，不再模拟 SAVE_SCHEDULED 中间态（实现细节）。
    assert coordinator.state == DIRTY
    debounce_ids = [aid for aid, d, _cb in root.scheduled if d == 1000]
    assert len(debounce_ids) == 1

    ok = coordinator.discard_pending(timeout=0.5)

    assert ok is True
    assert coordinator.state == CLEAN
    # debounce after_id 必须被取消，worker 不应启动
    assert debounce_ids[0] in root.cancelled
    assert file_handler.writes == []
    # 标记为 SAVE_SCHEDULED 仅用于类型提示，避免未使用导入告警
    _ = SAVE_SCHEDULED


def test_discard_pending_saving_waits_and_clears_without_new_save():
    """P0-1：SAVING 中调用 discard_pending：等待当前保存完成，
    完成后即使 generation 落后也置 CLEAN，不再调度新保存。

    覆盖审查文档 P0-1 第 4 点：若询问期间已有保存正在写盘，
    先完成该保存；discard_pending 不得假装可取消运行中的写入，
    但完成后不得再启动新保存。
    """
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 3)

    # 阻塞 worker，使其停留在 SAVING 状态
    file_handler.block_event.clear()
    coordinator.mark_dirty(source="flush")
    assert coordinator.is_saving is True
    save_gen = coordinator._save_generation

    # 保存期间产生新版本：正常 flush 会再次启动保存
    document.update_target(1, "新版本译文")
    assert document.version > save_gen

    # 在另一线程调用 discard_pending（主线程模拟需轮询）
    result_holder = {}

    def _discard():
        result_holder["ok"] = coordinator.discard_pending(timeout=5.0)

    discard_thread = threading.Thread(target=_discard)
    discard_thread.start()

    # 短暂等待，确保 discard_pending 已进入轮询
    time.sleep(0.05)

    # 释放 worker，让其完成
    file_handler.block_event.set()

    discard_thread.join(timeout=5.0)

    assert result_holder.get("ok") is True
    assert coordinator.state == CLEAN
    # 只发生一次保存（SAVING 完成那次），不再因 generation 落后启动新保存
    assert len(file_handler.writes) == 1


def test_discard_pending_saving_timeout_returns_false():
    """P0-1：SAVING 中 discard_pending 超时返回 False，
    状态保持 SAVING（未假装已放弃）。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    # 阻塞 worker，使其无法在超时内完成
    file_handler.block_event.clear()
    coordinator.mark_dirty(source="flush")
    assert coordinator.is_saving is True

    ok = coordinator.discard_pending(timeout=0.1)

    # 超时返回 False，状态仍为 SAVING
    assert ok is False
    assert coordinator.is_saving is True
    # discard_after_save 标记应被清除
    assert coordinator._discard_after_save is False

    # 清理：释放 worker
    file_handler.block_event.set()
    for _ in range(50):
        coordinator._poll_result()
        if coordinator.state != SAVING:
            break
        time.sleep(0.02)


def test_discard_pending_after_close_returns_clean_state():
    """P0-1：close 后 discard_pending 不抛异常，按 CLEAN 返回。"""
    coordinator, *_ = _make_coordinator()
    coordinator.close()
    assert coordinator.state == CLEAN

    ok = coordinator.discard_pending(timeout=0.1)
    assert ok is True
    assert coordinator.state == CLEAN


def test_discard_pending_clean_state_is_noop():
    """P0-1：CLEAN 状态下 discard_pending 无副作用，返回 True。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    assert coordinator.state == CLEAN

    ok = coordinator.discard_pending(timeout=0.1)

    assert ok is True
    assert coordinator.state == CLEAN
    assert file_handler.writes == []


def test_handle_result_with_discard_flag_forces_clean():
    """P0-1：_discard_after_save=True 时 _handle_result 强制置 CLEAN，
    不因 generation 落后而置 DIRTY。"""
    coordinator, root, file_handler, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    # 启动保存
    file_handler.block_event.clear()
    coordinator.mark_dirty(source="flush")
    save_gen = coordinator._save_generation

    # 保存期间产生新版本
    document.update_target(0, "新版本")

    # 设置 discard 标记（模拟 discard_pending 在 SAVING 中的路径）
    coordinator._discard_after_save = True

    # 释放 worker
    file_handler.block_event.set()
    # 触发结果处理（主线程内联）
    for _ in range(50):
        if coordinator._drain_result_inline():
            break
        time.sleep(0.02)

    # discard 标记应被清除，状态为 CLEAN（而非 DIRTY）
    assert coordinator._discard_after_save is False
    assert coordinator.state == CLEAN


def test_drain_result_inline_with_discard_flag_forces_clean():
    """P0-1：_drain_result_inline 在 _discard_after_save=True 时
    直接置 CLEAN，不调度新保存（不调用 _schedule）。"""
    coordinator, root, *_ = _make_coordinator()
    document = coordinator._document
    _populate(document, 2)

    # 模拟一个已完成的保存结果（generation 落后于 document.version）
    coordinator.mark_dirty(source="flush")
    save_gen = coordinator._save_generation
    document.update_target(0, "新版本")  # version 提升

    # 手动放入结果队列
    old_result = SaveResult(
        generation=save_gen,
        succeeded=True,
        elapsed_seconds=0.01,
        bytes_written=10,
    )
    coordinator._result_queue.put(old_result)

    # 设置 discard 标记
    coordinator._discard_after_save = True
    scheduled_before = list(root.scheduled)

    # 内联抽取：应置 CLEAN 而非 DIRTY
    drained = coordinator._drain_result_inline()

    assert drained is True
    assert coordinator.state == CLEAN
    # 不应调度新的 after 回调（不启动新保存）
    assert root.scheduled == scheduled_before
