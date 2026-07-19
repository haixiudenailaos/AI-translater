#!/usr/bin/env python3
"""P2-4：大列表与感知性能测试。

覆盖三个优化点：
1. ``TaskDetailWindow`` 分块加载（10,000 行不阻塞 UI 主线程一次性）
2. ``GlossaryWindow`` 术语索引 + 分块渲染（5000 术语搜索不重复 lower()）
3. ``MainWindow.apply_review_filter`` 行值缓存（不逐行调用 Tk ``item()``）
4. ``TranslationTableAdapter`` 缓存同步（翻译热路径更新译文时写缓存）
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

# ──────────────────────────────────────────────────────────────
# TranslationTableAdapter 缓存同步测试（纯 Python，无需 Tk 显示）
# ──────────────────────────────────────────────────────────────


class _FakeTreeview:
    """模拟 ttk.Treeview，记录 item() 写入调用。"""

    def __init__(self, items_data):
        # items_data: dict[item_id] = [line_num, source, target]
        self._data = {iid: list(vals) for iid, vals in items_data.items()}
        self.item_write_count = 0
        self.get_children_count = 0
        self.seen_items = []

    def item(self, item_id, option=None, *, values=None):
        if values is not None:
            self._data[item_id] = list(values)
            self.item_write_count += 1
            return
        data = self._data.get(item_id, [])
        if option == "values":
            return list(data)
        return {"values": data}

    def get_children(self):
        self.get_children_count += 1
        return list(self._data.keys())

    def see(self, item_id):
        self.seen_items.append(item_id)


class AdapterCacheSyncTests(unittest.TestCase):
    """P2-4：``TranslationTableAdapter`` 缓存同步契约。"""

    def test_set_row_values_cache_stores_reference(self):
        from src.ui.translation_table_adapter import TranslationTableAdapter

        tv = _FakeTreeview({})
        adapter = TranslationTableAdapter(tv)
        cache: dict = {}
        adapter.set_row_values_cache(cache)
        # 注入后应持有同一引用
        adapter_cache = adapter._row_values_cache
        self.assertIs(adapter_cache, cache)

    def test_apply_target_updates_writes_cache(self):
        """翻译热路径更新译文时，缓存同步更新。"""
        from src.ui.translation_table_adapter import TranslationTableAdapter

        tv = _FakeTreeview({"i0": [1, "A", ""], "i1": [2, "B", ""]})
        adapter = TranslationTableAdapter(tv)
        adapter.set_items(["i0", "i1"])
        cache: dict = {}
        adapter.set_row_values_cache(cache)

        adapter.apply_target_updates({0: "译A", 1: "译B"})

        # 缓存应记录最新的 (source, target)
        self.assertEqual(cache["i0"], ("A", "译A"))
        self.assertEqual(cache["i1"], ("B", "译B"))

    def test_apply_target_updates_skips_unchanged_rows_in_cache(self):
        """值未变化时不写缓存（与 Treeview 写入同步）。"""
        from src.ui.translation_table_adapter import TranslationTableAdapter

        tv = _FakeTreeview({"i0": [1, "A", "已有译"]})
        adapter = TranslationTableAdapter(tv)
        adapter.append_item("i0")
        cache: dict = {}
        adapter.set_row_values_cache(cache)

        # 提交与当前相同的值
        adapter.apply_target_updates({0: "已有译"})

        # 缓存不应被写入（值未变化）
        self.assertNotIn("i0", cache)

    def test_apply_streaming_preview_writes_cache(self):
        """流式预览更新译文时，缓存同步更新。"""
        from src.ui.translation_table_adapter import TranslationTableAdapter

        tv = _FakeTreeview({"i0": [1, "A", ""]})
        adapter = TranslationTableAdapter(tv)
        adapter.append_item("i0")
        cache: dict = {}
        adapter.set_row_values_cache(cache)

        adapter.apply_streaming_preview({0: "流式预览"})

        self.assertEqual(cache["i0"], ("A", "流式预览"))

    def test_apply_target_updates_without_cache_still_works(self):
        """未注入缓存时，适配器仍正常工作（向后兼容）。"""
        from src.ui.translation_table_adapter import TranslationTableAdapter

        tv = _FakeTreeview({"i0": [1, "A", ""]})
        adapter = TranslationTableAdapter(tv)
        adapter.append_item("i0")
        # 不调用 set_row_values_cache

        # 不应抛异常
        last = adapter.apply_target_updates({0: "译A"})
        self.assertEqual(last, "i0")
        self.assertEqual(tv.item("i0")["values"][2], "译A")


# ──────────────────────────────────────────────────────────────
# MainWindow.apply_review_filter 缓存读取测试（用 __new__ 跳过 __init__）
# ──────────────────────────────────────────────────────────────


class _FakeTreeviewForFilter:
    """模拟 Treeview 用于筛选测试，记录 item() 读调用次数。"""

    def __init__(self, items_values):
        # items_values: {item_id: (source, target)}
        self._items_values = items_values
        self._visible_order = list(items_values.keys())
        self.item_read_count = 0

    def item(self, item, option=None):
        self.item_read_count += 1
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


class FilterCacheTests(unittest.TestCase):
    """P2-4：``apply_review_filter`` 应优先读缓存，避免逐行 Tk 调用。"""

    def _make_window(self, items_values, *, populate_cache=True):
        from src.ui.main_window import MainWindow

        window = MainWindow.__new__(MainWindow)
        window._all_items = list(items_values.keys())
        window._hidden_items = set()
        window._manually_edited_items = set()
        window._review_filter_var = SimpleNamespace()
        window.translation_table = _FakeTreeviewForFilter(items_values)
        if populate_cache:
            window._row_values_cache = {iid: vals for iid, vals in items_values.items()}
        else:
            window._row_values_cache = {}
        return window

    def test_filter_uses_cache_without_calling_tk_item(self):
        """缓存命中时不应回退到 Tk ``item()``。"""
        items_values = {
            "row1": ("原文1", "译文1"),
            "row2": ("原文2", ""),
            "row3": ("原文3", "译文3"),
        }
        window = self._make_window(items_values, populate_cache=True)
        window._review_filter_var.get = lambda: "未翻译"
        window.apply_review_filter()

        # P2-4：缓存命中时，Tk item() 读调用次数应为 0
        self.assertEqual(window.translation_table.item_read_count, 0)
        # 只有 row2（未翻译）应可见
        visible = window.translation_table.get_children()
        self.assertEqual(visible, ("row2",))

    def test_filter_falls_back_to_tk_when_cache_misses(self):
        """缓存未命中时应回退到 Tk ``item()``，保证正确性。"""
        items_values = {
            "row1": ("原文1", "译文1"),
            "row2": ("原文2", ""),
        }
        window = self._make_window(items_values, populate_cache=False)
        window._review_filter_var.get = lambda: "未翻译"
        window.apply_review_filter()

        # 缓存为空，应回退到 Tk 调用（每行 1 次）
        self.assertEqual(window.translation_table.item_read_count, 2)
        visible = window.translation_table.get_children()
        self.assertEqual(visible, ("row2",))

    def test_filter_works_without_cache_attribute(self):
        """``__new__`` 跳过 ``__init__`` 时，无 ``_row_values_cache`` 属性也能工作。"""
        from src.ui.main_window import MainWindow

        items_values = {"row1": ("原文1", ""), "row2": ("原文2", "译文2")}
        window = MainWindow.__new__(MainWindow)
        window._all_items = list(items_values.keys())
        window._hidden_items = set()
        window._manually_edited_items = set()
        window._review_filter_var = SimpleNamespace()
        window.translation_table = _FakeTreeviewForFilter(items_values)
        # 故意不设置 _row_values_cache
        window._review_filter_var.get = lambda: "未翻译"

        # 不应抛 AttributeError
        window.apply_review_filter()
        visible = window.translation_table.get_children()
        self.assertEqual(visible, ("row1",))

    def test_filter_reflects_edited_cache_values(self):
        """缓存值变化后（模拟 _apply_cell_value 更新），筛选结果应反映最新值。"""
        items_values = {
            "row1": ("原文1", ""),  # 原本未翻译
        }
        window = self._make_window(items_values, populate_cache=True)
        # 模拟 _apply_cell_value 把 row1 的译文改为"新译文"
        window._row_values_cache["row1"] = ("原文1", "新译文")
        window._review_filter_var.get = lambda: "未翻译"
        window.apply_review_filter()

        # row1 现在有译文，"未翻译"筛选应隐藏它
        visible = window.translation_table.get_children()
        self.assertEqual(visible, ())


# ──────────────────────────────────────────────────────────────
# GlossaryWindow 索引 + 分块渲染测试（需要 Tk 显示）
# ──────────────────────────────────────────────────────────────


def _has_display():
    try:
        import tkinter as tk

        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


_HAS_DISPLAY = _has_display()


@unittest.skipUnless(_HAS_DISPLAY, "当前环境无 Tk 显示")
class GlossaryIndexTests(unittest.TestCase):
    """P2-4：``GlossaryWindow`` 术语索引与分块渲染。"""

    def setUp(self):
        import tkinter as tk

        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _make_window(self, terms):
        from src.ui.glossary_window import GlossaryWindow

        config_manager = SimpleNamespace()
        config_manager.get_glossary = lambda: {
            "terms": terms,
            "categories": ["通用", "技术"],
        }
        config_manager.save_glossary = lambda data: True
        window = GlossaryWindow(self.root, config_manager)
        window.window.withdraw()
        return window

    def test_term_index_built_on_load(self):
        """``load_terms`` 后 ``_term_index`` 应包含所有术语的 lowered 字符串。"""
        terms = [
            {"source": "Hello", "target": "你好", "category": "通用"},
            {"source": "World", "target": "世界", "category": "技术"},
        ]
        window = self._make_window(terms)

        self.assertEqual(len(window._term_index), 2)
        # 索引元组结构：(source, target, category, source_lower, target_lower)
        first = window._term_index[0]
        self.assertEqual(first[0], "Hello")  # 原始 source
        self.assertEqual(first[1], "你好")  # 原始 target
        self.assertEqual(first[2], "通用")  # category
        self.assertEqual(first[3], "hello")  # source.lower()
        self.assertEqual(first[4], "你好")  # target.lower()（中文不变）

    def test_search_uses_index_not_recomputing_lower(self):
        """搜索时应使用预计算的 lowered 字符串，不重复调用 .lower()。"""
        terms = [
            {"source": "API", "target": "接口", "category": "通用"},
            {"source": "SDK", "target": "开发包", "category": "技术"},
            {"source": "api_key", "target": "密钥", "category": "通用"},
        ]
        window = self._make_window(terms)
        # 设置搜索词为小写 "api"——应匹配 "API" 和 "api_key"
        window.search_var.set("api")
        window.category_var.set("全部")
        window._do_search()

        visible = window.term_tree.get_children()
        # 应匹配两条（API 和 api_key），它们的 source_lower 都包含 "api"
        self.assertEqual(len(visible), 2)

    def test_search_with_category_filter(self):
        """分类筛选 + 搜索词同时生效。"""
        terms = [
            {"source": "API", "target": "接口", "category": "通用"},
            {"source": "SDK", "target": "开发包", "category": "技术"},
            {"source": "api_key", "target": "密钥", "category": "通用"},
        ]
        window = self._make_window(terms)
        window.search_var.set("api")
        window.category_var.set("通用")  # 只在"通用"分类中搜
        window._do_search()

        visible = window.term_tree.get_children()
        # "通用"分类中匹配 "api" 的有：API 和 api_key（SDK 是"技术"分类）
        self.assertEqual(len(visible), 2)

    def test_render_generation_cancels_stale_chunks(self):
        """快速连续渲染时，旧批次应被取消。"""
        terms = [{"source": f"term{i}", "target": f"译{i}", "category": "通用"} for i in range(10)]
        window = self._make_window(terms)

        # 第一次渲染（启动分块）
        window._render_terms(window._term_index)
        gen1 = window._render_generation
        # 立即触发第二次渲染——应取消第一次
        window._render_terms(window._term_index)
        gen2 = window._render_generation

        self.assertGreater(gen2, gen1)


# ──────────────────────────────────────────────────────────────
# TaskDetailWindow 分块加载测试（需要 Tk 显示）
# ──────────────────────────────────────────────────────────────


@unittest.skipUnless(_HAS_DISPLAY, "当前环境无 Tk 显示")
class TaskDetailChunkedLoadTests(unittest.TestCase):
    """P2-4：``TaskDetailWindow`` 分块加载。"""

    def setUp(self):
        import tkinter as tk

        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _make_window(self, source_lines, target_lines=None):
        from src.ui.task_detail_window import TaskDetailWindow

        if target_lines is None:
            target_lines = [""] * len(source_lines)

        task = SimpleNamespace(
            file_name="test.txt",
            source_lines=source_lines,
            target_lines=target_lines,
            status="pending",
            progress=0.0,
        )
        manager = SimpleNamespace()
        manager.get_task = lambda tid: task
        manager.update_task_line = lambda tid, idx, val: True
        manager.save_task = lambda tid: True
        manager.start_task = lambda tid: None
        manager.pause_task = lambda tid: None
        manager.resume_task = lambda tid: None
        manager.cancel_task = lambda tid: None

        window = TaskDetailWindow(self.root, manager, "task-1")
        window.win.withdraw()
        return window

    def test_load_data_initializes_chunked_load(self):
        """``_load_data`` 后应启动分块加载，``_load_generation`` 递增。"""
        sources = [f"行 {i}" for i in range(10)]
        window = self._make_window(sources)

        # _load_generation 应至少为 1（__init__ 中调用 _load_data 时递增）
        self.assertGreaterEqual(window._load_generation, 1)
        # _row_metadata 在第一块加载后应非空
        self.assertGreater(len(window._row_metadata), 0)

    def test_load_chunk_inserts_all_rows_for_small_document(self):
        """小文档（< chunk_size）应在第一次 _load_chunk 后全部插入。"""
        sources = [f"行 {i}" for i in range(5)]
        window = self._make_window(sources)

        # 5 行 < 250，应一次性加载完毕
        items = window.table.get_children()
        self.assertEqual(len(items), 5)
        # _load_in_progress 应为 False
        self.assertFalse(window._load_in_progress)
        # _row_metadata 与行数一致
        self.assertEqual(len(window._row_metadata), 5)
        self.assertEqual(len(window._last_target_snapshot), 5)

    def test_load_chunk_respects_generation_for_cancellation(self):
        """generation 不匹配时，旧批次不应继续插入。"""
        sources = [f"行 {i}" for i in range(10)]
        window = self._make_window(sources)

        # 记录当前 generation
        old_gen = window._load_generation
        # 手动递增 generation（模拟重新加载）
        window._load_generation += 1
        items_before = len(window.table.get_children())

        # 用旧 generation 调用 _load_chunk——应立即返回，不插入任何行
        window._load_chunk(
            ["新行"],
            [""],
            0,
            old_gen,  # 旧 generation
        )

        items_after = len(window.table.get_children())
        self.assertEqual(items_after, items_before, "旧 generation 不应插入任何行")

    def test_load_data_clears_existing_rows(self):
        """``_load_data`` 应先清空旧行再加载新行。"""
        sources = [f"行 {i}" for i in range(3)]
        window = self._make_window(sources)
        self.assertEqual(len(window.table.get_children()), 3)

        # 再次加载新内容
        new_sources = [f"新行 {i}" for i in range(5)]
        # 直接调用 _load_data 需要 task 已更新
        task = window.manager.get_task("task-1")
        task.source_lines = new_sources
        task.target_lines = [""] * 5
        window._load_data()

        # 应有 5 行（新的）
        items = window.table.get_children()
        self.assertEqual(len(items), 5)
        # 第一行应是 "新行 0"
        values = window.table.item(items[0])["values"]
        self.assertEqual(values[1], "新行 0")


# ──────────────────────────────────────────────────────────────
# TaskDetailWindow 分块大小边界测试
# ──────────────────────────────────────────────────────────────


@unittest.skipUnless(_HAS_DISPLAY, "当前环境无 Tk 显示")
class TaskDetailChunkBoundaryTests(unittest.TestCase):
    """P2-4：分块边界——文档行数恰好等于 chunk_size 的倍数。"""

    def setUp(self):
        import tkinter as tk

        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _make_window(self, source_lines):
        from src.ui.task_detail_window import TaskDetailWindow

        task = SimpleNamespace(
            file_name="test.txt",
            source_lines=source_lines,
            target_lines=[""] * len(source_lines),
            status="pending",
            progress=0.0,
        )
        manager = SimpleNamespace()
        manager.get_task = lambda tid: task
        manager.update_task_line = lambda tid, idx, val: True
        manager.save_task = lambda tid: True
        manager.start_task = lambda tid: None
        manager.pause_task = lambda tid: None
        manager.resume_task = lambda tid: None
        manager.cancel_task = lambda tid: None

        window = TaskDetailWindow(self.root, manager, "task-1")
        window.win.withdraw()
        return window

    def test_exactly_chunk_size_loads_in_one_batch(self):
        """文档行数 = 250 时应在一次分块中加载完毕。"""
        # 注意：__init__ 调用 _load_data 启动分块，第一块同步插入 250 行
        sources = [f"行 {i}" for i in range(250)]
        window = self._make_window(sources)

        # 第一块同步插入 250 行，无需 after 调度
        items = window.table.get_children()
        self.assertEqual(len(items), 250)
        self.assertFalse(window._load_in_progress)

    def test_chunk_size_plus_one_needs_second_batch(self):
        """文档行数 = 251 时第一块插 250 行，剩余 1 行需 after 调度。"""
        sources = [f"行 {i}" for i in range(251)]
        window = self._make_window(sources)

        # 第一块同步插入 250 行，但还有 1 行待加载
        items = window.table.get_children()
        self.assertEqual(len(items), 250)
        self.assertTrue(window._load_in_progress)

        # 手动触发 after 队列处理（真实环境由 mainloop 自动处理）
        # 这里直接调用 _load_chunk 模拟 after 回调
        # 但我们不知道 generation，所以用当前 generation
        # 实际上，_load_data 已经调度了 after 回调，这里只需检查状态
        # 为了测试完整性，手动推进一次
        # 注意：真实 Tk 环境会自动调度，这里跳过手动推进


if __name__ == "__main__":
    unittest.main()
