#!/usr/bin/env python3
"""双语 EPUB 导出的领域契约。

只使用标准库类型，不依赖 ebooklib、BeautifulSoup 或 Tk。双语导出是导出
时的呈现方式：``EpubDocumentSnapshot`` 是主线程提交编辑后捕获的不可变模型
快照，后台导出只读取该快照，不修改项目映射与翻译数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class EpubTextMode(str, Enum):
    """EPUB 正文字幕呈现模式。"""

    TRANSLATED = "translated"
    BILINGUAL = "bilingual"


@dataclass(frozen=True, slots=True)
class EpubExportOptions:
    """导出呈现选项；默认值与既有纯译文导出完全一致。"""

    text_mode: EpubTextMode = EpubTextMode.TRANSLATED
    target_language_tag: str | None = None


@dataclass(frozen=True, slots=True)
class EpubDocumentSnapshot:
    """主线程捕获的不可变文档快照。

    ``session_id`` 与 ``document_version`` 标识捕获点，不代表磁盘保存版本。
    后台只能使用这里保存的字符串元组，不能继续访问 ``TranslationDocument``。
    """

    session_id: str
    document_version: int
    source_path: Path
    mapping_dir: Path
    source_lines: tuple[str, ...]
    target_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BilingualExportReport:
    """本次双语导出的段落统计。

    ``total_segments = paired_segments + untranslated_segments + preserved_segments``。
    """

    total_segments: int
    paired_segments: int
    untranslated_segments: int
    preserved_segments: int


@dataclass(frozen=True, slots=True)
class BilingualTargetEntry:
    """一个应写入 XHTML 的译文目标容器（pair 标记 + 纯文本）。"""

    pair_id: str
    text: str
    source_text: str


@dataclass(frozen=True, slots=True)
class BilingualDocumentPlan:
    """单章的双语渲染计划，供写出后校验使用。"""

    document_name: str
    targets: tuple[BilingualTargetEntry, ...]
    preserved_pair_ids: tuple[str, ...]
    untranslated_pair_ids: tuple[str, ...]
    original_ids: tuple[str, ...]
    # ebooklib 会用包内目录数据重新生成导航文档，其 DOM 不能按原内容校验。
    generated: bool = False


@dataclass(frozen=True, slots=True)
class BilingualRenderPlan:
    """全书双语渲染计划，是 exporter 与 validator 之间的私有契约。"""

    stylesheet_name: str
    documents: tuple[BilingualDocumentPlan, ...]
    report: BilingualExportReport


# 明确的本地语言名称到 BCP 47 标签的映射；未知值省略，不猜测、不调用 API。
_LANGUAGE_TAGS: dict[str, str] = {
    "中文": "zh",
    "汉语": "zh",
    "简体中文": "zh-Hans",
    "繁体中文": "zh-Hant",
    "繁體中文": "zh-Hant",
    "英文": "en",
    "英语": "en",
    "日文": "ja",
    "日语": "ja",
    "日語": "ja",
    "韩文": "ko",
    "韩语": "ko",
    "韓語": "ko",
    "法文": "fr",
    "法语": "fr",
    "德文": "de",
    "德语": "de",
    "西班牙语": "es",
    "俄语": "ru",
    "葡萄牙语": "pt",
    "意大利语": "it",
}

_LANGUAGE_TAG_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")


def resolve_language_tag(value: str | None) -> str | None:
    """把配置里的本地语言名称解析为语言标签；无法确定时返回 ``None``。"""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    mapped = _LANGUAGE_TAGS.get(normalized) or _LANGUAGE_TAGS.get(normalized.lower())
    if mapped is not None:
        return mapped
    if _LANGUAGE_TAG_PATTERN.match(normalized):
        return normalized
    return None
