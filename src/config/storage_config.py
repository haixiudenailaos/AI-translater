#!/usr/bin/env python3
"""数据目录（storage）配置段定义与归一化。

对应 docs/USER_CUSTOMIZABLE_DATA_DIRECTORIES_IMPLEMENTATION_GUIDE.md §3.2：

- ``data_root``：用户选择的数据根目录；为空表示使用平台默认目录。
- 新设置页只写入 ``data_root``，缓存、翻译中间记录和译文备份目录均由
  它自动派生。
- ``cache_dir`` / ``translation_records_dir`` / ``translation_backups_dir`` /
  ``logs_dir``：保留用于读取旧版独立目录配置；新设置页保存时会清空前三
  个覆盖字段，使数据统一跟随 ``data_root``。
- ``path_mode``：记录当前解释规则，便于将来升级配置格式。

本模块只负责配置段的默认值和类型归一化，不做任何路径解析；
路径解析优先级由 ``src.infrastructure.storage_paths`` 统一实现。
"""

from __future__ import annotations

import copy
from typing import Any, Dict

STORAGE_SCHEMA_VERSION = 1

# 当前唯一的路径解释模式：data_root 为默认父目录，各子目录可显式覆盖。
PATH_MODE_ROOT_WITH_OVERRIDES = "root_with_overrides"

#: storage 配置段的字段名集合（归一化时只保留这些键，丢弃未知键）。
STORAGE_PATH_FIELDS = (
    "data_root",
    "cache_dir",
    "translation_records_dir",
    "translation_backups_dir",
    "logs_dir",
)

DEFAULT_STORAGE_CONFIG: Dict[str, Any] = {
    "schema_version": STORAGE_SCHEMA_VERSION,
    "data_root": "",
    "cache_dir": "",
    "translation_records_dir": "",
    "translation_backups_dir": "",
    "logs_dir": "",
    "path_mode": PATH_MODE_ROOT_WITH_OVERRIDES,
}


def normalize_storage_config(existing: Any) -> Dict[str, Any]:
    """归一化 storage 配置段。

    与 ``ConfigManager._normalize_onboarding_config`` 同一模式：顶层
    ``load_app_config`` 只做浅合并，嵌套段必须单独做默认值合并与类型
    校正，避免缺失字段或损坏配置导致后续解析失败。

    规则：
    - 非 dict 输入（缺失/损坏）→ 返回默认值副本。
    - 路径字段必须是字符串，否则回退为空字符串（跟随默认）。
    - 路径字段去除首尾空白；``schema_version`` 必须是 int；
      ``path_mode`` 只接受已知值。
    - 未知键直接丢弃，避免旧版本遗留字段污染保存结果。
    """
    default = copy.deepcopy(DEFAULT_STORAGE_CONFIG)
    if not isinstance(existing, dict):
        return default

    merged = default
    for field in STORAGE_PATH_FIELDS:
        value = existing.get(field, "")
        if isinstance(value, str):
            merged[field] = value.strip()
        else:
            merged[field] = ""

    schema_version = existing.get("schema_version")
    merged["schema_version"] = (
        schema_version
        if isinstance(schema_version, int) and not isinstance(schema_version, bool)
        else STORAGE_SCHEMA_VERSION
    )

    path_mode = existing.get("path_mode")
    merged["path_mode"] = (
        path_mode if path_mode == PATH_MODE_ROOT_WITH_OVERRIDES else PATH_MODE_ROOT_WITH_OVERRIDES
    )
    return merged
