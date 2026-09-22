"""A spinner with rotating verbs for the phases that can take a while.

Between "this judge started" and "this judge finished" there is nothing to
report but the wait itself. Printing silence there is indistinguishable from a
hang, especially against a large artifact with a slow model. A spinner that
keeps changing verb says the process is alive without pretending to know more
than it does.
"""

from __future__ import annotations

import random

from rich.console import Console
from rich.status import Status

VERBS = (
    "Pondering",
    "Scrutinizing",
    "Weighing",
    "Cross-examining",
    "Mulling over",
    "Interrogating",
    "Second-guessing",
    "Poring over",
    "Deliberating on",
    "Chewing on",
    "Sizing up",
    "Litigating",
    "Puzzling over",
    "Wrangling",
    "Auditing",
)


class WorkSpinner:
    """Tracks a set of concurrently running items under one spinner line."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._status: Status | None = None
        self._running: dict[str, str] = {}
        self._note: str = ""

    def start(self) -> None:
        if self._status is None:
            self._status = self.console.status("", spinner="dots")
            self._status.start()

    def mark_running(self, name: str) -> None:
        self._running[name] = random.choice(VERBS)
        self._refresh()

    def mark_done(self, name: str, line: str) -> None:
        """Print ``line`` as this item's permanent result and drop it from the spinner."""
        self._running.pop(name, None)
        if self._status is not None:
            # Status.console.print interleaves cleanly with a live spinner:
            # it clears the line, prints, and redraws.
            self._status.console.print(line)
        else:
            self.console.print(line)
        self._refresh()

    def note(self, text: str) -> None:
        """Show a line of someone else's output beside the spinner.

        A repair agent runs for minutes; without its latest line, a spinner
        cannot be told apart from a hang.
        """
        self._note = _one_line(text)
        self._refresh()

    def clear_note(self) -> None:
        self._note = ""
        self._refresh()

    def _refresh(self) -> None:
        if self._status is None:
            return
        if self._note and not self._running:
            self._status.update(f"[dim]{self._note}[/dim]")
            return
        if not self._running:
            self._status.update("")
            return
        names = ", ".join(f"{verb} {name}" for name, verb in self._running.items())
        self._status.update(f"[dim]{names}...[/dim]")

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
        self._running.clear()


def _one_line(text: str, limit: int = 110) -> str:
    """Flatten and clip someone else's output so it cannot break the spinner line."""
    flat = " ".join(text.split())
    escaped = flat.replace("[", "\\[")
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"
