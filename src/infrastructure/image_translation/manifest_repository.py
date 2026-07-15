#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片翻译结果 manifest 仓库

在保持 result_map 向后兼容的前提下升级到 v2。

兼容要求：
- 现有导出器仍只读取 result_map。
- 每次运行即使结果为空也覆盖旧 manifest，防止导出过期图片。
- v1 裸字典和带 result_map 格式继续可读，下一次成功保存升级为 v2。
- 失败原因做脱敏和长度限制。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ...domain.image_translation import ImageTranslationResult
from ...domain.translation import OperationStatus
from ...utils.logger import get_logger
from ..atomic_file import write_json_atomic

logger = get_logger(__name__)

MANIFEST_FILENAME = "image_translation_result.json"
SCHEMA_VERSION = 2
_MAX_ERROR_LEN = 300


def _sanitize_error(msg: str) -> str:
    """脱敏并截断错误信息，避免泄露 Key/Base64/鉴权响应。"""
    if not msg:
        return ""
    text = str(msg)
    # 移除常见敏感模式
    for token in ("api_key", "apikey", "API-Key", "authorization", "Bearer "):
        if token.lower() in text.lower():
            text = "[filtered: contains credential token]"
            break
    if len(text) > _MAX_ERROR_LEN:
        text = text[:_MAX_ERROR_LEN] + "...(truncated)"
    return text


@dataclass
class ManifestData:
    """解析后的 manifest 数据"""

    schema_version: int = 1
    provider: str = ""
    status: str = ""
    run_id: str = ""
    run_at: str = ""
    source_fingerprint: str = ""
    config_fingerprint: str = ""
    result_map: dict = field(default_factory=dict)
    skipped_images: list = field(default_factory=list)
    failed_images: dict = field(default_factory=dict)

    @property
    def is_v2(self) -> bool:
        return self.schema_version >= 2


class ManifestRepository:
    """图片翻译结果 manifest 原子读写及 v1/v2 兼容"""

    def __init__(self, mapping_dir: Path):
        self.mapping_dir = Path(mapping_dir)
        self.path = self.mapping_dir / MANIFEST_FILENAME

    def load(self) -> Optional[ManifestData]:
        """读取 manifest，兼容 v1 裸字典、v1 result_map 格式和 v2。

        不存在返回 None。
        """
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取 manifest 失败: %s: %s", self.path, exc)
            return None

        if not isinstance(raw, dict):
            # 异常结构，视为空
            return ManifestData()

        schema_version = raw.get("schema_version", 1)

        if "result_map" in raw:
            # v1 带 result_map 或 v2
            result_map = raw.get("result_map") or {}
            if not isinstance(result_map, dict):
                result_map = {}
            return ManifestData(
                schema_version=schema_version,
                provider=raw.get("provider", ""),
                status=raw.get("status", ""),
                run_id=raw.get("run_id", ""),
                run_at=raw.get("run_at", ""),
                source_fingerprint=raw.get("source_fingerprint", ""),
                config_fingerprint=raw.get("config_fingerprint", ""),
                result_map=result_map,
                skipped_images=raw.get("skipped_images", []) or [],
                failed_images=raw.get("failed_images", {}) or {},
            )

        # v1 裸字典：整个字典视为 result_map
        # 过滤明显非映射的字段
        result_map = {
            k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        return ManifestData(schema_version=1, result_map=result_map)

    def load_result_map(self) -> dict:
        """仅读取 result_map，供导出器使用。

        向后兼容：v1 裸字典、v1 result_map 格式、v2 均可读。
        """
        data = self.load()
        if data is None:
            return {}
        return dict(data.result_map)

    def save(
        self,
        result: ImageTranslationResult,
        *,
        source_fingerprint: str = "",
        config_fingerprint: str = "",
        run_at: str = "",
    ) -> None:
        """以 v2 格式原子写入 manifest。

        即使 result_map 为空也覆盖旧文件，避免导出过期图片。
        """
        # 脱敏失败原因
        failed = {
            path: _sanitize_error(msg)
            for path, msg in result.failed_images.items()
        }

        payload = {
            "schema_version": SCHEMA_VERSION,
            "provider": result.provider_id.value
            if hasattr(result.provider_id, "value")
            else str(result.provider_id),
            "status": result.status.value,
            "run_id": result.run_id,
            "run_at": run_at,
            "source_fingerprint": source_fingerprint,
            "config_fingerprint": config_fingerprint,
            "result_count": len(result.result_map),
            "result_map": result.result_map,
            "skipped_images": list(result.skipped_images),
            "failed_images": failed,
        }
        try:
            write_json_atomic(self.path, payload)
        except Exception as exc:
            logger.error("写入 manifest 失败: %s: %s", self.path, exc)
            raise

    def save_empty(self, *, run_at: str = "") -> None:
        """写入空结果 manifest（取消或全部失败前的清理）。"""
        payload = {
            "schema_version": SCHEMA_VERSION,
            "provider": "",
            "status": OperationStatus.FAILED.value,
            "run_id": "",
            "run_at": run_at,
            "source_fingerprint": "",
            "config_fingerprint": "",
            "result_count": 0,
            "result_map": {},
            "skipped_images": [],
            "failed_images": {},
        }
        try:
            write_json_atomic(self.path, payload)
        except Exception as exc:
            logger.error("写入空 manifest 失败: %s: %s", self.path, exc)
            raise
