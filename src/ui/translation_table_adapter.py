#!/usr/bin/env python3
"""PERF：翻译表格适配器。

将 ``Treeview`` 的细节收敛到一处，翻译热路径不再调用
``get_children()`` 或逐行 ``item()`` 读取。

详见 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §7.3。

设计要点：
- 行号到 item ID 使用已维护的稳定列表（``_item_ids``）。
- ``apply_target_updates`` 批量更新，只对变化的行调用 ``Treeview.item()``。
- 搜索/过滤重建 item 顺序时，由调用方同步更新映射。
- 删除或重载文档时增加 generation，旧分块加载回调不得插入新文档。
"""

from __future__ import annotations

from typing import Dict, List, Optional


class TranslationTableAdapter:
    """Treeview 与 TranslationDocument 之间的桥梁。"""

    def __init__(self, treeview) -> None:
        self._treeview = treeview
        self._item_ids: List[str] = []
        # P2-4：可选的行值缓存引用（由 MainWindow 注入）。
        # 翻译热路径更新译文时同步写入缓存，避免 apply_review_filter
        # 对 5000+ 行逐行调用 ``Treeview.item()``。
        self._row_values_cache: Optional[Dict[str, tuple]] = None

    def set_row_values_cache(self, cache: Optional[Dict[str, tuple]]) -> None:
        """P2-4：注入行值缓存。``None`` 表示禁用缓存同步。"""
        self._row_values_cache = cache

    @property
    def row_count(self) -> int:
        """当前管理的行数。"""
        return len(self._item_ids)

    def reset(self) -> None:
        """清空所有 item ID（重载文档前调用）。"""
        self._item_ids.clear()

    def append_item(self, item_id: str) -> None:
        """追加一个 item ID（分块加载时逐行调用）。"""
        self._item_ids.append(item_id)

    def set_items(self, item_ids: List[str]) -> None:
        """整体替换 item ID 列表。"""
        self._item_ids = list(item_ids)

    def item_id(self, row_index: int) -> str | None:
        """根据行号获取 item ID。越界返回 None。"""
        if 0 <= row_index < len(self._item_ids):
            return self._item_ids[row_index]
        return None

    def find_row_index(self, item_id: str) -> int:
        """根据 item ID 查找行号（线性查找）。

        仅用于低频的用户操作（选中行翻译、单元格编辑定位等），
        不在流式渲染热路径中调用。未找到返回 -1。
        """
        try:
            return self._item_ids.index(item_id)
        except ValueError:
            return -1

    def apply_target_updates(
        self,
        updates: Dict[int, str],
        *,
        skip_empty: bool = True,
    ) -> str | None:
        """批量更新译文。

        - 只对变化的行调用 ``Treeview.item()`` 写入。
        - ``skip_empty=True`` 时空译文不覆盖已有译文（R2-BUG-009）。
        - 返回最后更新的 item ID（用于滚动定位）。
        - P2-4：同步更新行值缓存（若已注入），保持 apply_review_filter 一致性。
        """
        last_item: str | None = None
        cache = self._row_values_cache
        for row_index, target in updates.items():
            item = self.item_id(row_index)
            if item is None:
                continue
            new_val = target.strip() if target else ""
            if skip_empty and not new_val:
                continue
            values = list(self._treeview.item(item, "values"))
            # values[2] 是译文列
            if len(values) > 2 and values[2] != new_val:
                values[2] = new_val
                self._treeview.item(item, values=values)
                if cache is not None:
                    source = str(values[1]) if len(values) > 1 else ""
                    cache[item] = (source, str(new_val))
            last_item = item
        return last_item

    def apply_streaming_preview(
        self,
        updates: Dict[int, str],
    ) -> str | None:
        """流式预览更新：空值也写入（实时显示中间状态）。

        与 ``apply_target_updates`` 的区别：流式预览允许空字符串
        覆盖（显示"正在生成"的中间状态），不跳过空值。
        P2-4：同步更新行值缓存（若已注入）。
        """
        last_item: str | None = None
        cache = self._row_values_cache
        for row_index, target in updates.items():
            item = self.item_id(row_index)
            if item is None:
                continue
            new_val = target.strip() if target else ""
            values = list(self._treeview.item(item, "values"))
            if len(values) > 2 and values[2] != new_val:
                values[2] = new_val
                self._treeview.item(item, values=values)
                if cache is not None:
                    source = str(values[1]) if len(values) > 1 else ""
                    cache[item] = (source, str(new_val))
            last_item = item
        return last_item

    def see(self, row_index: int) -> None:
        """滚动到指定行。"""
        item = self.item_id(row_index)
        if item is not None:
            self._treeview.see(item)

    def see_item(self, item_id: str) -> None:
        """滚动到指定 item ID（兼容旧 API）。"""
        self._treeview.see(item_id)

    def get_target(self, row_index: int) -> str:
        """读取指定行的当前译文。"""
        item = self.item_id(row_index)
        if item is None:
            return ""
        values = self._treeview.item(item, "values")
        return values[2] if len(values) > 2 else ""

    def consistency_check(self, expected_count: int) -> bool:
        """开发期一致性断言：item ID 数量等于模型行数。

        只在测试或诊断模式启用，不在每个流式事件中调用。
        """
        return len(self._item_ids) == expected_count
