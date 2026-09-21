# Example: a deliberately flawed paper

This artifact is wrong on purpose. It exists so `veritas evaluate` has real
problems to find, and so the end-to-end test has a stable target.

Planted defects:

| Defect | Where |
| --- | --- |
| Headline number contradicts the artifact (paper says 41ms p99, `results/benchmark.json` says 48.3ms) | `paper/main.md` Results vs `results/benchmark.json` |
| Method mismatch: paper describes gradient boosting, code implements an EWMA | `paper/main.md` Method vs `experiments/router.py` |
| Single run, no seed, no variance, presented as an improvement | `results/benchmark.json`, `scripts/benchmark.py` |
| Unsupported generality claim ("applies to any request-routing workload") | `paper/main.md` Abstract, Conclusion |
| Unsupported secondary claims (mean latency, bursty robustness) with no supporting data | `paper/main.md` Results |
| References that cannot be verified from the artifact | `paper/main.md` References |
| No reproducibility information: no environment, dependencies, hardware or commands | throughout |
