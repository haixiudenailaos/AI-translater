#!/usr/bin/env python3
"""P1-6：筛选往返顺序测试。

验证 ``MainWindow.apply_review_filter`` 在隐藏部分行再恢复全部后，
行的显示顺序与 ``_all_items`` 原始顺序一致。

旧实现只 reattach 刚恢复可见的行到 "end"，导致隐藏过的行追加到末尾，
顺序错乱。新实现按 ``_all_items`` 顺序逐项 reattach，保证顺序正确。
"""

import unittest
from types import SimpleNamespace


class _FakeTreeview:
    """模拟 ttk.Treeview，跟踪 reattach/detach 调用和当前可见顺序。"""

    def __init__(self, items_values):
        # items_values: {item_id: (source, target)}
        self._items_values = items_values
        self._visible_order = list(items_values.keys())

    def item(self, item, option=None):
        if option == "values":
            src, tgt = self._items_values.get(item, ("", ""))
            return ("", src, tgt, "", "")
        return None

    def reattach(self, item, parent, index):
        if item in self._visible_order:
            self._visible_order.remove(item)
        if index == "end":
            self._visible_order.append(item)
        else:
            self._visible_order.insert(int(index), item)

    def detach(self, item):
        if item in self._visible_order:
            self._visible_order.remove(item)

    def get_children(self):
        return tuple(self._visible_order)


class FilterOrderTests(unittest.TestCase):
    """P1-6：筛选往返后行序不变。"""

    def _make_window(self, items_values):
        from src.ui.main_window import MainWindow

        window = MainWindow.__new__(MainWindow)
        window._all_items = list(items_values.keys())
        window._hidden_items = set()
        window._manually_edited_items = set()
        window._review_filter_var = SimpleNamespace()
        window.translation_table = _FakeTreeview(items_values)
        return window

    def test_filter_round_trip_preserves_original_order(self):
        """隐藏部分行再恢复全部，顺序应与原始一致。"""
        items_values = {
            "row1": ("原文1", "译文1"),
            "row2": ("原文2", ""),
            "row3": ("原文3", "译文3"),
            "row4": ("原文4", ""),
            "row5": ("原文5", "译文5"),
        }
        window = self._make_window(items_values)

        # 步骤1：筛选"未翻译"，隐藏已翻译的行
        window._review_filter_var.get = lambda: "未翻译"
        window.apply_review_filter()

        # 此时只有 row2 和 row4 可见
        visible = window.translation_table.get_children()
        self.assertEqual(visible, ("row2", "row4"))

        # 步骤2：恢复"全部"
        window._review_filter_var.get = lambda: "全部"
        window.apply_review_filter()

        # P1-6：恢复后顺序应与 _all_items 一致
        visible = window.translation_table.get_children()
        self.assertEqual(visible, ("row1", "row2", "row3", "row4", "row5"))

    def test_multiple_filter_round_trips_preserve_order(self):
        """多次筛选往返后顺序仍正确。"""
        items_values = {
            "A": ("src-a", "tgt-a"),
            "B": ("src-b", ""),
            "C": ("src-c", "tgt-c"),
            "D": ("src-d", ""),
            "E": ("src-e", "tgt-e"),
            "F": ("src-f", ""),
        }
        window = self._make_window(items_values)
        original_order = list(items_values.keys())

        # 多次往返
        for _ in range(3):
            window._review_filter_var.get = lambda: "未翻译"
            window.apply_review_filter()
            window._review_filter_var.get = lambda: "全部"
            window.apply_review_filter()

        visible = window.translation_table.get_children()
        self.assertEqual(visible, tuple(original_order))

    def test_quality_check_filter_round_trip(self):
        """质检问题筛选往返后顺序正确。"""
        items_values = {
            "row1": ("相同", "相同"),  # source == target → 质检问题
            "row2": ("原文2", "译文2"),  # 正常
            "row3": ("原文3", ""),  # 未翻译 → 质检问题
            "row4": ("原文4", "译文4"),  # 正常
        }
        window = self._make_window(items_values)
        original_order = list(items_values.keys())

        # 筛选"质检问题"
        window._review_filter_var.get = lambda: "质检问题"
        window.apply_review_filter()
        visible = window.translation_table.get_children()
        self.assertEqual(set(visible), {"row1", "row3"})

        # 恢复全部
        window._review_filter_var.get = lambda: "全部"
        window.apply_review_filter()
        visible = window.translation_table.get_children()
        self.assertEqual(visible, tuple(original_order))


if __name__ == "__main__":
    unittest.main()
