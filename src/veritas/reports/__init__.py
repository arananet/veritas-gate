"""Report rendering: terminal first, then machine-readable artefacts."""

from veritas.reports.console import ConsoleReporter
from veritas.reports.markdown import render_markdown

__all__ = ["ConsoleReporter", "render_markdown"]
