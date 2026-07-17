#!/usr/bin/env python3
"""Client for user-defined OpenAI-compatible chat completion services."""

from .base_api import BaseAPI


class OpenAICompatibleAPI(BaseAPI):
    """Use the shared OpenAI-compatible transport without vendor defaults."""

    DEFAULT_BASE_URL = ""
    DEFAULT_MODEL = ""
