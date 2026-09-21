"""Benchmark harness for the FastRoute example.

Deliberately incomplete: it fixes no seed, runs once, and records no variance.
The example exists so Veritas Gate has something real to find.
"""

import json
import random
from pathlib import Path

BACKENDS = 8
REQUESTS = 10_000


def simulate(router: str) -> dict[str, float]:
    latencies = []
    for _ in range(REQUESTS):
        base = random.expovariate(1 / 15.0)
        penalty = 3.0 if router == "least-connections" else 1.5
        latencies.append(base + random.random() * penalty)
    latencies.sort()
    return {
        "p99_ms": round(latencies[int(len(latencies) * 0.99)], 1),
        "mean_ms": round(sum(latencies) / len(latencies), 1),
    }


def main() -> None:
    results = {name: simulate(name) for name in ("least-connections", "fastroute")}
    output = Path(__file__).resolve().parent.parent / "results" / "benchmark.json"
    output.write_text(json.dumps({"runs": 1, "seed": None, "routers": results}, indent=2))


if __name__ == "__main__":
    main()
