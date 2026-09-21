"""FastRoute router implementation for the example artifact.

Note the discrepancy with the manuscript: the paper describes a gradient-boosted
estimator, while this implements a linear exponentially-weighted moving average.
"""

from dataclasses import dataclass, field


@dataclass
class EwmaRouter:
    """Routes to the backend with the lowest EWMA completion time."""

    alpha: float = 0.2
    estimates: dict[str, float] = field(default_factory=dict)

    def observe(self, backend: str, completion_ms: float) -> None:
        previous = self.estimates.get(backend, completion_ms)
        self.estimates[backend] = self.alpha * completion_ms + (1 - self.alpha) * previous

    def route(self, backends: list[str]) -> str:
        return min(backends, key=lambda name: self.estimates.get(name, 0.0))
