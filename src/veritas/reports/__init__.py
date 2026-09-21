"""Report rendering: terminal first, then machine-readable artefacts."""

from veritas.reports.console import ConsoleReporter
from veritas.reports.loop import LoopReporter, render_loop_report
from veritas.reports.markdown import render_markdown

__all__ = ["ConsoleReporter", "LoopReporter", "render_loop_report", "render_markdown"]
