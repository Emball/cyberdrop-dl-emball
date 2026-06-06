from __future__ import annotations

import asyncio
import collections
import dataclasses
import random
import time
from typing import Any, Final, final

from rich.console import Group
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.table import Column
from rich.text import Text

from cyberdrop_dl.progress import create_test_live


# How many recent skip events to keep in the live log
_MAX_LOG_ROWS: Final = 5

# Labels and colours for each skip reason counter
_REASONS: Final = (
    ("header_hash",   "cyan",    "Header hash"),
    ("partial_hash",  "magenta", "Partial hash (16 MB)"),
    ("fingerprint",   "blue",    "Fingerprint"),
    ("fuzzy",         "yellow",  "Fuzzy name/size"),
    ("exact_name",    "white",   "Exact filename"),
)


@dataclasses.dataclass(slots=True)
class DedupeStats:
    header_hash:  int = 0
    partial_hash: int = 0
    fingerprint:  int = 0
    fuzzy:        int = 0
    exact_name:   int = 0

    @property
    def total(self) -> int:
        return self.header_hash + self.partial_hash + self.fingerprint + self.fuzzy + self.exact_name


@final
class DedupePanel:
    """Live dedup monitor.

    Top section: one bar per skip-reason showing count + % of all skips.
    Bottom section: scrolling log of the most recent _MAX_LOG_ROWS skip events.
    """

    def __init__(self) -> None:
        columns = (
            "[progress.description]{task.description}",
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>6.2f}%",
            "•",
            TextColumn("{task.completed:,}", justify="right", table_column=Column(min_width=4)),
        )
        self._progress = Progress(*columns, expand=True)
        self._stats = DedupeStats()
        self._total: int = 0

        # One task per reason
        self._task_ids: dict[str, TaskID] = {}
        for key, colour, label in _REASONS:
            tid = self._progress.add_task(f"[{colour}]{label}", total=1, completed=0)
            self._task_ids[key] = tid

        # Scrolling log of recent events (deque keeps it bounded)
        self._log: collections.deque[Text] = collections.deque(maxlen=_MAX_LOG_ROWS)

        self._panel = Panel(
            Group(self._progress, _LogRenderable(self._log)),
            title="[bold]Dedup[/bold]",
            border_style="cyan",
            padding=(1, 1),
        )

    # ------------------------------------------------------------------
    # Public API — call these from download/crawler code
    # ------------------------------------------------------------------

    def record(self, reason: str, filename: str, matched: str | None = None) -> None:
        """Increment the counter for *reason* and append a log entry.

        Args:
            reason:   one of header_hash / partial_hash / fingerprint / fuzzy / exact_name
            filename: the incoming file that was skipped
            matched:  the existing file it was matched against (optional)
        """
        if not hasattr(self._stats, reason):
            return

        setattr(self._stats, reason, getattr(self._stats, reason) + 1)
        self._refresh_bars()
        self._append_log(reason, filename, matched)

    @property
    def stats(self) -> DedupeStats:
        return self._stats

    # ------------------------------------------------------------------
    # Rich rendering
    # ------------------------------------------------------------------

    def __rich__(self) -> Panel:
        self._panel.subtitle = f"Total skipped: [white]{self._stats.total:,}"
        return self._panel

    def __json__(self) -> dict[str, Any]:
        return dataclasses.asdict(self._stats)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_bars(self) -> None:
        total = max(self._stats.total, 1)
        if total != self._total:
            for key, tid in self._task_ids.items():
                self._progress.update(tid, total=total, completed=getattr(self._stats, key))
            self._total = total

    def _append_log(self, reason: str, filename: str, matched: str | None) -> None:
        colour = {k: c for k, c, _ in _REASONS}.get(reason, "white")
        label  = {k: l for k, _, l in _REASONS}.get(reason, reason)

        # Truncate long filenames so the panel stays narrow
        def _trunc(s: str, n: int = 38) -> str:
            return s if len(s) <= n else "…" + s[-(n - 1):]

        ts = time.strftime("%H:%M:%S")
        line = Text(overflow="ellipsis", no_wrap=True)
        line.append(f"{ts} ", style="dim")
        line.append(f"[{label}] ", style=colour)
        line.append(_trunc(filename), style="bold white")
        if matched:
            line.append(" ← ", style="dim")
            line.append(_trunc(matched), style="dim white")

        self._log.append(line)

    # ------------------------------------------------------------------
    # Simulate (for standalone testing)
    # ------------------------------------------------------------------

    async def simulate(self) -> None:
        reasons = [k for k, *_ in _REASONS]
        files   = ["video_1080p.mp4", "clip_final_v2.mp4", "stream_hls.mp4",
                   "TEDDY-CHAN_hls_1080p.mp4", "archive_part1.zip", "photo_001.jpg"]
        matches = ["video_1080p_orig.mp4", None, "clip_final.mp4", "TEDDY-CHAN.mp4", None, "photo_001_old.jpg"]

        for _ in range(20):
            i = random.randrange(len(files))
            self.record(random.choice(reasons), files[i], matches[i])
            await asyncio.sleep(random.uniform(0.3, 1.2))


# ------------------------------------------------------------------
# Helper renderable for the scrolling log section
# ------------------------------------------------------------------

class _LogRenderable:
    """Renders the deque of Text lines as a mini table."""

    def __init__(self, log: collections.deque[Text]) -> None:
        self._log = log

    def __rich_console__(self, console: object, options: object) -> Any:  # type: ignore[override]
        from rich.rule import Rule
        yield Rule(style="dim cyan")
        if not self._log:
            yield Text("  No duplicates skipped yet.", style="dim")
            return
        for line in self._log:
            yield line


if __name__ == "__main__":
    panel = DedupePanel()
    with create_test_live(panel, json=False):
        asyncio.run(panel.simulate())
