#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Application 层

定义用例编排所需的端口（协议）和适配器。Application 层只依赖 domain 层，
不直接依赖 httpx、ebooklib 或 tkinter 等基础设施。

- ports.py：定义 TranslationProvider / UiScheduler 等协议
- translator_provider.py：把现有 TranslatorEngine 适配为 TranslationProvider
"""
