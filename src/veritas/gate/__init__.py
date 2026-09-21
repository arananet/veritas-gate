"""Deterministic quality gate."""

from veritas.gate.engine import evaluate_gate
from veritas.gate.policy import severity_counts

__all__ = ["evaluate_gate", "severity_counts"]
