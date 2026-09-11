#!/usr/bin/env python3
"""PERF：翻译文档状态模型。

业务状态的唯一真相来源，取代从 ``Treeview`` 反向构造全量数据。
详见 ``TEXT_TRANSLATION_STARTUP_PERFORMANCE_REPAIR_PLAN.md`` §7。

设计要点：
- 不导入 Tkinter，纯 Python 数据结构。
- 只在 Tk 主线程修改，因此行操作不加锁。
- 后台保存只接收不可变 tuple 快照，不持有模型引用。
- 翻译工作线程不直接修改模型，而是发布事件。
- 人工译文默认优先（``manually_edited=True``），不被空响应或旧任务覆盖。
- ``version`` 单调递增，每次修改 +1，用于保存 generation 校验。
- ``snapshot_targets()`` 返回不可变 tuple，后续编辑不改变旧快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass(slots=True)
class TranslationRow:
    """单行翻译数据。

    - ``source``：原文（导入后不再修改）。
    - ``target``：译文（翻译/编辑时更新）。
    - ``manually_edited``：是否被人工编辑过。人工译文不被空响应覆盖。
    """

    source: str
    target: str = ""
    manually_edited: bool = False


class TranslationDocument:
    """翻译文档状态模型。

    所有修改只在 Tk 主线程进行，因此无需为行操作加锁。
    后台保存通过 ``snapshot_targets()`` 获取不可变快照。
    """

    def __init__(self, rows: List[TranslationRow] | None = None) -> None:
        self._rows: List[TranslationRow] = list(rows) if rows else []
        self._version: int = 0
        self._dirty_indices: set[int] = set()

    @property
    def version(self) -> int:
        """文档版本，每次修改 +1。用于保存 generation 校验。"""
        return self._version

    @property
    def row_count(self) -> int:
        """行数。"""
        return len(self._rows)

    @property
    def dirty_indices(self) -> frozenset[int]:
        """自上次保存以来被修改的行索引（只读视图）。"""
        return frozenset(self._dirty_indices)

    @property
    def has_dirty_rows(self) -> bool:
        """是否存在未保存的行。热路径判空，不拷贝 dirty 集合。"""
        return bool(self._dirty_indices)

    def row(self, index: int) -> TranslationRow:
        """获取指定行（只读访问）。"""
        return self._rows[index]

    def source_lines(self) -> List[str]:
        """返回所有原文的列表副本。"""
        return [row.source for row in self._rows]

    def target_lines(self) -> List[str]:
        """返回所有译文的列表副本。"""
        return [row.target for row in self._rows]

    def replace(
        self,
        source_lines: List[str],
        target_lines: List[str],
    ) -> None:
        """整体替换文档内容（导入/重载时调用）。

        - 原文和译文按索引严格对齐，不足的译文补空字符串。
        - 重置 version 为 1（新文档）。
        - 清空 dirty_indices（新文档初始为干净状态）。
        """
        self._rows = [
            TranslationRow(
                source=source,
                target=target_lines[index] if index < len(target_lines) else "",
            )
            for index, source in enumerate(source_lines)
        ]
        self._version += 1
        self._dirty_indices.clear()

    def update_target(
        self,
        index: int,
        value: str,
        *,
        manually_edited: bool = False,
    ) -> bool:
        """更新指定行的译文。

        - 相同值不增加 version（避免无意义 dirty）。
        - ``manually_edited=True`` 标记为人工编辑，不被空响应覆盖。
        - PERF §7.2 D1：人工编辑过的行（``manually_edited=True``）不被
          非人工来源（``manually_edited=False``）的空响应覆盖，避免翻译
          批次返回空字符串时清空用户已确认的译文。
        - P0-4：人工编辑过的行不被任何非人工来源的结果覆盖（包括非空机器结果）。
          旧 generation 的机器结果不得覆盖人工译文。
        - 返回是否实际发生了修改。
        """
        row = self._rows[index]
        # P0-4：保护人工编辑过的行不被任何非人工来源的结果覆盖
        if row.manually_edited and not manually_edited:
            return False
        if row.target == value:
            # 值未变化，但仍可能需要提升人工编辑标记
            if manually_edited and not row.manually_edited:
                row.manually_edited = True
                self._dirty_indices.add(index)
                self._version += 1
                return True
            return False
        row.target = value
        row.manually_edited = row.manually_edited or manually_edited
        self._dirty_indices.add(index)
        self._version += 1
        return True

    def update_source(self, index: int, value: str) -> bool:
        """更新指定行的原文（人工编辑原文列时调用）。

        计划约定原文导入后不再修改，但 UI 允许编辑原文列，
        此方法保持模型与视图一致。相同值不增加 version。
        """
        row = self._rows[index]
        if row.source == value:
            return False
        row.source = value
        self._dirty_indices.add(index)
        self._version += 1
        return True

    def clear_dirty(self) -> None:
        """保存成功后清除 dirty 标记。不增加 version。"""
        self._dirty_indices.clear()

    def mark_dirty(self, index: int) -> None:
        """显式标记某行为脏（例如撤销/重做后）。不增加 version。"""
        self._dirty_indices.add(index)

    def snapshot_targets(self) -> Tuple[int, Tuple[str, ...]]:
        """返回 ``(version, targets_tuple)`` 不可变快照。

        后台保存线程持有此快照，后续编辑不改变快照内容。
        """
        return self._version, tuple(row.target for row in self._rows)

    def is_manually_edited(self, index: int) -> bool:
        """指定行是否被人工编辑过。"""
        return self._rows[index].manually_edited

    def clear_manual_flag(self, index: int) -> None:
        """P0-4：清除指定行的人工编辑标记。

        重译场景必须显式调用此方法，否则旧 generation 的机器结果
        （包括非空结果）会被 ``update_target`` 拒绝写入人工编辑过的行。
        清除标记后，后续 ``update_target`` 可正常覆盖该行译文。

        注意：此方法不修改 ``target`` 值，也不增加 ``version``，
        仅重置 ``manually_edited`` 标记。
        """
        self._rows[index].manually_edited = False

    def find_missing_indices(self) -> List[int]:
        """查找原文非空但译文为空的行索引（用于查漏/续翻）。"""
        return [
            index
            for index, row in enumerate(self._rows)
            if row.source.strip() and not row.target.strip()
        ]

    def find_pending_indices(self) -> List[int]:
        """查找需要翻译的行（原文非空且译文为空）。"""
        return self.find_missing_indices()
