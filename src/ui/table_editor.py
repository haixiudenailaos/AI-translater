#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用 Treeview 单元格编辑组件
可复用于 MainWindow 和 TaskDetailWindow。
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional, Callable


class TableCellEditor:
    """为 ttk.Treeview 提供双击编辑能力。

    用法：
        editor = TableCellEditor(tree, editable_columns={1, 2}, on_save=my_callback)
        tree.bind('<Double-Button-1>', editor.on_double_click)

    on_save(item_id, col_index, old_value, new_value) 在值变更时被调用。
    """

    def __init__(self, tree: ttk.Treeview, *,
                 editable_columns: Optional[set] = None,
                 on_save: Optional[Callable] = None,
                 font: tuple = ("微软雅黑", 10)):
        self.tree = tree
        self.editable_columns = editable_columns or {1, 2}
        self.on_save = on_save
        self.font = font

        self.edit_entry: Optional[tk.Entry] = None
        self.editing_item: Optional[str] = None
        self.editing_column: Optional[int] = None

    def on_double_click(self, event):
        """双击单元格进入编辑"""
        if self.edit_entry:
            self.save()

        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return

        column = self.tree.identify_column(event.x)
        col_idx = int(column.replace('#', '')) - 1
        if col_idx not in self.editable_columns:
            return

        item = self.tree.identify_row(event.y)
        if not item:
            return

        values = self.tree.item(item)['values']
        if not values or col_idx >= len(values):
            return

        bbox = self.tree.bbox(item, column)
        if not bbox:
            return

        self.editing_item = item
        self.editing_column = col_idx

        self.edit_entry = tk.Entry(self.tree, font=self.font,
                                   relief=tk.SOLID, borderwidth=1)
        self.edit_entry.insert(0, values[col_idx])
        self.edit_entry.select_range(0, tk.END)
        self.edit_entry.focus_set()
        self.edit_entry.place(x=bbox[0], y=bbox[1],
                              width=bbox[2], height=bbox[3])

        self.edit_entry.bind('<Return>', lambda e: self.save())
        self.edit_entry.bind('<Escape>', lambda e: self.cancel())
        self.edit_entry.bind('<FocusOut>', lambda e: self.save())

    def save(self):
        """保存编辑并销毁 Entry"""
        if not self.edit_entry or not self.editing_item:
            return

        new_value = self.edit_entry.get()
        values = list(self.tree.item(self.editing_item)['values'])
        old_value = values[self.editing_column]
        values[self.editing_column] = new_value
        self.tree.item(self.editing_item, values=values)

        item_id = self.editing_item
        col_idx = self.editing_column

        self._destroy_entry()

        if old_value != new_value and self.on_save:
            self.on_save(item_id, col_idx, old_value, new_value)

    def cancel(self):
        """取消编辑"""
        self._destroy_entry()

    def _destroy_entry(self):
        if self.edit_entry:
            self.edit_entry.destroy()
            self.edit_entry = None
        self.editing_item = None
        self.editing_column = None
