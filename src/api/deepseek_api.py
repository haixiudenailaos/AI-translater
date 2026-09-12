#!/usr/bin/env python3
"""
Deepseek API接口模块
继承 BaseAPI，仅保留差异配置（默认URL和模型名）。
"""

from ..utils.logger import get_logger
from .base_api import BaseAPI

logger = get_logger(__name__)


class DeepseekAPI(BaseAPI):
    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-flash"
    DEFAULT_MAX_KEEPALIVE = 10
    DEFAULT_MAX_CONNECTIONS = 20
    DEFAULT_TIMEOUT = 60.0
