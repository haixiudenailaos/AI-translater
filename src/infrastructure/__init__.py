#!/usr/bin/env python3
"""
Infrastructure 层

EPUB 解析、段落提取、映射持久化和导出等基础设施实现。
Application 层通过端口（协议）依赖，不直接导入此层。

阶段 4（EPUB 拆分）交付：
- document_order：spine 文档迭代和章节 ID 归一化
- segment_extractor：块级标签选择和段落定位
- mapping_repository：映射文件读写和格式版本管理
- image_rewriter：图片替换和 figcaption 注入
- exporter：导出协调
"""
