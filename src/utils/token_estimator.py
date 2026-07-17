"""Small dependency-free token estimates for batching and telemetry."""

import math


def estimate_tokens(text: str) -> int:
    """Conservatively estimate tokens without a model-specific tokenizer."""
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars))
