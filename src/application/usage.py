"""UXF-012: privacy-preserving per-project translation usage accounting."""

from dataclasses import dataclass


@dataclass
class UsageStatistics:
    requests: int = 0
    retries: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        total = self.requests + self.cache_hits
        return self.cache_hits / total if total else 0.0

    def record_request(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
    ) -> None:
        self.requests += 1
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        if input_price_per_million is not None and output_price_per_million is not None:
            self.estimated_cost += (
                input_tokens * input_price_per_million + output_tokens * output_price_per_million
            ) / 1_000_000

    def record_retry(self) -> None:
        self.retries += 1

    def record_cache_hit(self) -> None:
        self.cache_hits += 1

    def record_metrics_delta(
        self,
        current: dict[str, int | float],
        previous: dict[str, int | float] | None = None,
    ) -> None:
        """Merge cumulative API counters without storing request content."""
        baseline = previous or {}

        def delta(key: str) -> int:
            return max(0, int(current.get(key, 0)) - int(baseline.get(key, 0)))

        self.requests += delta("successful_requests")
        self.retries += delta("retries")
        self.cache_hits += delta("cache_hits")
        self.input_tokens += delta("input_tokens_estimated")
        self.output_tokens += delta("output_tokens_estimated")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": self.cache_hit_rate,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
        }
