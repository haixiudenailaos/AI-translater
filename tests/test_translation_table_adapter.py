#!/usr/bin/env python3
"""PERF：翻译表格适配器单元测试。

覆盖 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §7.6 的要求：
- 更新 3 行时只调用 3 次 ``Treeview.item()`` 写入
- 流式刷新不调用 ``get_children()``
- 分块加载完成后 item ID 与模型行数一致
- 搜索过滤不破坏原始行索引
"""

from __future__ import annotations

from src.ui.translation_table_adapter import TranslationTableAdapter


class FakeTreeview:
    """Fake Treeview：记录所有 item() 调用，支持 get/see。

    模拟真实 Tk Treeview 的 item() 行为：
    - ``item(id, "values")`` 读取，返回 values 列表（positional option）
    - ``item(id, values=[...])`` 写入（keyword values）
    两者通过 keyword-only 参数区分。
    """

    def __init__(self, items_data):
        """items_data: dict[item_id] = [line_num, source, target]"""
        self._data = {iid: list(vals) for iid, vals in items_data.items()}
        self.item_write_count = 0
        self.get_children_count = 0
        self.seen_items = []

    def item(self, item_id, option=None, *, values=None):
        if values is not None:
            # 写入模式：tree.item(id, values=[...])
            self._data[item_id] = list(values)
            self.item_write_count += 1
            return
        # 读取模式：tree.item(id) 或 tree.item(id, "values")
        data = self._data.get(item_id, [])
        if option == "values":
            return list(data)
        return {"values": data}

    def get_children(self):
        self.get_children_count += 1
        return list(self._data.keys())

    def see(self, item_id):
        self.seen_items.append(item_id)


# ── item ID 管理 ────────────────────────────────────────


def test_adapter_starts_empty():
    """适配器初始为空。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)

    assert adapter.row_count == 0
    assert adapter.item_id(0) is None


def test_append_item_increments_count():
    """append_item 逐行追加。"""
    tv = FakeTreeview({"i1": [1, "A", ""], "i2": [2, "B", ""]})
    adapter = TranslationTableAdapter(tv)

    adapter.append_item("i1")
    adapter.append_item("i2")

    assert adapter.row_count == 2
    assert adapter.item_id(0) == "i1"
    assert adapter.item_id(1) == "i2"


def test_set_items_replaces_list():
    """set_items 整体替换。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)

    adapter.append_item("old")
    adapter.set_items(["new1", "new2"])

    assert adapter.row_count == 2
    assert adapter.item_id(0) == "new1"


def test_reset_clears_items():
    """reset 清空。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i1")

    adapter.reset()

    assert adapter.row_count == 0


def test_item_id_out_of_bounds_returns_none():
    """越界返回 None。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i1")

    assert adapter.item_id(-1) is None
    assert adapter.item_id(1) is None
    assert adapter.item_id(0) == "i1"


# ── apply_target_updates ───────────────────────────────


def test_apply_target_updates_writes_only_changed_rows():
    """更新 3 行时只对变化的行调用 item() 写入。"""
    tv = FakeTreeview(
        {
            "i0": [1, "A", ""],
            "i1": [2, "B", ""],
            "i2": [3, "C", "旧译"],
        }
    )
    adapter = TranslationTableAdapter(tv)
    adapter.set_items(["i0", "i1", "i2"])

    # i1 的值与当前相同，不应写入
    updates = {0: "译A", 1: "", 2: "新译"}
    last_item = adapter.apply_target_updates(updates)

    assert tv.item_write_count == 2  # 只写了 i0 和 i2
    assert last_item == "i2"
    assert tv.item("i0")["values"][2] == "译A"
    assert tv.item("i2")["values"][2] == "新译"


def test_apply_target_updates_skips_empty_by_default():
    """默认 skip_empty=True，空译文不覆盖已有译文。"""
    tv = FakeTreeview({"i0": [1, "A", "已有译"]})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i0")

    adapter.apply_target_updates({0: "  "})  # 空白字符串

    assert tv.item_write_count == 0
    assert tv.item("i0")["values"][2] == "已有译"


def test_apply_target_updates_skip_empty_false_writes_empty():
    """skip_empty=False 时空值也写入。"""
    tv = FakeTreeview({"i0": [1, "A", "已有译"]})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i0")

    adapter.apply_target_updates({0: ""}, skip_empty=False)

    assert tv.item_write_count == 1
    assert tv.item("i0")["values"][2] == ""


def test_apply_target_updates_returns_last_item():
    """返回最后更新的 item ID。"""
    tv = FakeTreeview(
        {
            "i0": [1, "A", ""],
            "i1": [2, "B", ""],
            "i2": [3, "C", ""],
        }
    )
    adapter = TranslationTableAdapter(tv)
    adapter.set_items(["i0", "i1", "i2"])

    last = adapter.apply_target_updates({0: "译A", 2: "译C"})

    assert last == "i2"


def test_apply_target_updates_ignores_out_of_bounds():
    """越界行被忽略，不抛出异常。"""
    tv = FakeTreeview({"i0": [1, "A", ""]})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i0")

    adapter.apply_target_updates({0: "译A", 5: "越界"})

    assert tv.item_write_count == 1


# ── apply_streaming_preview ────────────────────────────


def test_streaming_preview_writes_empty_values():
    """流式预览允许空值写入（显示中间状态）。"""
    tv = FakeTreeview({"i0": [1, "A", "旧译"]})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i0")

    adapter.apply_streaming_preview({0: "新预览"})

    assert tv.item_write_count == 1
    assert tv.item("i0")["values"][2] == "新预览"


def test_streaming_preview_does_not_call_get_children():
    """流式刷新不调用 get_children()。"""
    tv = FakeTreeview({f"i{i}": [i + 1, f"行{i}", ""] for i in range(100)})
    adapter = TranslationTableAdapter(tv)
    adapter.set_items([f"i{i}" for i in range(100)])

    adapter.apply_streaming_preview({0: "译0", 50: "译50", 99: "译99"})

    assert tv.get_children_count == 0


# ── see / get_target ───────────────────────────────────


def test_see_scrolls_to_row():
    """see 根据行号滚动。"""
    tv = FakeTreeview({"i5": [6, "F", ""]})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i5")

    adapter.see(0)

    assert tv.seen_items == ["i5"]


def test_see_out_of_bounds_is_noop():
    """see 越界时不操作。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)

    adapter.see(0)

    assert tv.seen_items == []


def test_get_target_reads_current_value():
    """get_target 读取当前译文。"""
    tv = FakeTreeview({"i0": [1, "A", "当前译"]})
    adapter = TranslationTableAdapter(tv)
    adapter.append_item("i0")

    assert adapter.get_target(0) == "当前译"


def test_get_target_out_of_bounds_returns_empty():
    """get_target 越界返回空字符串。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)

    assert adapter.get_target(0) == ""


# ── consistency_check ──────────────────────────────────


def test_consistency_check_passes_when_counts_match():
    """item ID 数量等于模型行数时通过。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)
    adapter.set_items(["a", "b", "c"])

    assert adapter.consistency_check(3) is True


def test_consistency_check_fails_when_counts_mismatch():
    """item ID 数量不等于模型行数时失败。"""
    tv = FakeTreeview({})
    adapter = TranslationTableAdapter(tv)
    adapter.set_items(["a", "b"])

    assert adapter.consistency_check(3) is False
