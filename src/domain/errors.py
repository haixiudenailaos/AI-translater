#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领域异常定义

这些异常表达业务语义，不绑定具体基础设施（如 httpx、ebooklib）。
应用层和基础设施层共同捕获/抛出。

设计原则：
- 取消、失败和指纹不匹配等业务错误使用领域异常，而非返回空列表或 None。
- 异常携带足够的上下文（failed_indices、partial_lines、指纹值）供上层决策。
- 不在此处导入任何项目内模块，保持领域层纯净。
"""

from typing import List, Optional


class TranslationRequestError(Exception):
    """翻译请求失败异常

    当重试耗尽后抛出，调用方必须区分失败与空译文。
    不要使用空列表表示失败。

    Attributes:
        failed_indices: 该批次内失败的行索引（0-based，相对于批次起始）
        status_code: HTTP 状态码（如适用），用于诊断
    """

    def __init__(self, message: str, failed_indices: Optional[List[int]] = None,
                 status_code: Optional[int] = None):
        super().__init__(message)
        self.failed_indices = failed_indices or []
        self.status_code = status_code


class TranslationCancelled(Exception):
    """翻译被用户取消异常

    取消发生在批次内时抛出，替代旧实现返回伪造的空译文列表。
    调用方必须捕获并走 CANCELLED 路径：
    - 不得发送最终成功批次回调
    - 不得用空字符串覆盖已确认的译文

    Attributes:
        partial_lines: 取消前已确认的部分译文（流式场景下可能有部分行已完成）
    """

    def __init__(self, message: str = "翻译已被用户取消",
                 partial_lines: Optional[List[str]] = None):
        super().__init__(message)
        self.partial_lines = partial_lines or []


class EpubFingerprintMismatchError(Exception):
    """EPUB 源文件指纹不匹配异常（R2-BUG-006）

    导出前校验源 EPUB 内容哈希，不一致时抛出。
    调用方应提示用户重新关联源文件，避免用旧译文导出到新结构。

    Attributes:
        expected: 导入时记录的指纹
        actual: 当前源文件的实际指纹
    """

    def __init__(self, message: str = "源 EPUB 文件已变化，请重新关联",
                 expected: Optional[str] = None, actual: Optional[str] = None):
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class SegmentMappingError(Exception):
    """段落映射错误（R2-BUG-004/005）

    当试图按位置复用译文但原文已变化，或章节 ID 无法唯一定位时抛出。

    Attributes:
        segment_id: 受影响的段落 ID
        reason: 具体原因（"source_changed" / "ambiguous_id" / "missing_locator"）
    """

    def __init__(self, message: str, segment_id: Optional[str] = None,
                 reason: Optional[str] = None):
        super().__init__(message)
        self.segment_id = segment_id
        self.reason = reason


class ImageTranslationCancelled(Exception):
    """图片翻译被用户取消异常

    Provider 在取消令牌触发后抛出，替代返回伪造的成功结果。
    Service 捕获后走 CANCELLED 路径，不调用完成回调。
    """

    def __init__(self, message: str = "图片翻译已被用户取消"):
        super().__init__(message)


class ImageTranslationConfigError(Exception):
    """图片翻译配置错误（如未知语言、引擎未安装、模型缺失）

    在执行前校验失败时抛出，不静默回退为中文或切换 AI Provider。
    """
    pass
