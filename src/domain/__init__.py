# -*- coding: utf-8 -*-
"""
领域层（domain）

定义业务模型、状态、规则和端口。不依赖任何项目外层模块
（不导入 httpx、ebooklib、tkinter 等），可独立单元测试。

模块：
- translation: 翻译操作的状态、选项、进度和结果
- errors: 领域异常（翻译取消、请求失败、EPUB 指纹不匹配等）
"""
