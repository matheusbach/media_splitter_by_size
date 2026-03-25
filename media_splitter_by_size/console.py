"""Rich console output with progress bars and detailed track information."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TaskID,
)
from rich.table import Table
from rich.text import Text

from media_splitter_by_size.probe import ProbeResult, probe
from media_splitter_by_size.splitter import (
    SplitProgressCallback,
    SplitResult,
    format_size,
    format_time,
)


class RichProgressCallback(SplitProgressCallback):
    """Progress callback that renders a rich console UI."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._total_duration: float = 0
        self._current_part_start: float = 0
        self._max_completed_seen: float = 0
        self._info: ProbeResult | None = None

    def on_probe_complete(self, info: ProbeResult) -> None:
        self._info = info
        self._total_duration = info.duration

        # Display file information
        fmt = info.format
        self.console.print()
        self.console.print(
            Panel(
                f"[bold]{fmt.filename}[/bold]",
                title="[bold cyan]Media Splitter by Size[/bold cyan]",
                border_style="cyan",
            )
        )

        # Format info table
        info_table = Table(show_header=False, box=None, padding=(0, 2))
        info_table.add_column("Key", style="bold")
        info_table.add_column("Value")
        info_table.add_row("Format", f"{fmt.format_long_name} ({fmt.format_name})")
        info_table.add_row("Duration", format_time(fmt.duration))
        info_table.add_row("Size", format_size(fmt.size))
        info_table.add_row("Bitrate", f"{fmt.bit_rate // 1000} kbps")
        self.console.print(info_table)
        self.console.print()

        # Streams table
        stream_table = Table(
            title="[bold]Streams[/bold]",
            show_lines=False,
            border_style="dim",
        )
        stream_table.add_column("#", style="dim", width=3)
        stream_table.add_column("Type", width=10)
        stream_table.add_column("Codec", width=20)
        stream_table.add_column("Details", width=35)
        stream_table.add_column("Bitrate", width=12, justify="right")
        stream_table.add_column("Language", width=8)

        type_colors = {
            "video": "green",
            "audio": "yellow",
            "subtitle": "blue",
            "data": "magenta",
        }

        for s in info.streams:
            color = type_colors.get(s.codec_type, "white")
            details_parts = []
            if "width" in s.extra and "height" in s.extra:
                details_parts.append(f"{s.extra['width']}x{s.extra['height']}")
            if "r_frame_rate" in s.extra:
                details_parts.append(f"{s.extra['r_frame_rate']} fps")
            if "sample_rate" in s.extra:
                details_parts.append(f"{s.extra['sample_rate']} Hz")
            if "channels" in s.extra:
                ch = s.extra["channels"]
                layout = s.extra.get("channel_layout", f"{ch}ch")
                details_parts.append(str(layout))
            if s.profile:
                details_parts.append(f"profile: {s.profile}")

            bitrate_str = f"{s.bit_rate // 1000} kbps" if s.bit_rate else "N/A"

            stream_table.add_row(
                str(s.index),
                Text(s.codec_type, style=color),
                s.codec_name,
                ", ".join(details_parts) if details_parts else "-",
                bitrate_str,
                s.language,
            )

        self.console.print(stream_table)
        self.console.print()

    def on_split_start(self, part_number: int, start_time: float) -> None:
        self._current_part_start = start_time
        if self._progress is None:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(bar_width=40),
                TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
                TextColumn("| Elapsed:"),
                TimeElapsedColumn(),
                TextColumn("| ETA:"),
                TimeRemainingColumn(),
                console=self.console,
                transient=False,
            )
            self._task_id = self._progress.add_task(
                "Splitting...", total=self._total_duration
            )
            self._progress.start()

        # Keep progress monotonic when retries restart a segment from an earlier time.
        self._max_completed_seen = max(self._max_completed_seen, start_time)
        if self._progress and self._task_id is not None:
            self._progress.update(self._task_id, completed=self._max_completed_seen)

    def on_split_progress(
        self, part_number: int, current_time: float, total_duration: float, current_size: int
    ) -> None:
        if self._progress and self._task_id is not None:
            # Clamp to [0, total] and never move backwards.
            bounded = max(0.0, min(current_time, self._total_duration))
            self._max_completed_seen = max(self._max_completed_seen, bounded)
            self._progress.update(self._task_id, completed=self._max_completed_seen)

    def on_split_complete(
        self, part_number: int, output_path: Path, file_size: int, duration_secs: float, fill_ratio: float = 0.0
    ) -> None:
        if self._progress and self._task_id is not None:
            completed = max(0.0, min(self._current_part_start + duration_secs, self._total_duration))
            self._max_completed_seen = max(self._max_completed_seen, completed)
            self._progress.update(self._task_id, completed=self._max_completed_seen)

        fill_pct = fill_ratio * 100
        end_time = self._current_part_start + duration_secs
        
        # Remove hour mapping up to the last decimal for precise formatting if desired, but format_time handles it
        self.console.print(
            f"  [green]Part {part_number} complete[/green]: "
            f"[bold]{output_path.name}[/bold] "
            f"({format_size(file_size)}, [bold]{fill_pct:.1f}%[/bold] fill)\n"
            f"  start {format_time(self._current_part_start)}, "
            f"end {format_time(end_time)}, "
            f"duration {format_time(duration_secs)}"
        )

    def on_all_complete(self, result: SplitResult) -> None:
        if self._progress:
            if self._task_id is not None:
                self._progress.update(self._task_id, completed=self._total_duration)
            self._progress.stop()

        self.console.print()
        summary = Table(title="[bold green]Split Complete[/bold green]", border_style="green")
        summary.add_column("Part", justify="center")
        summary.add_column("Filename")
        summary.add_column("Size", justify="right")

        total_size_bytes = 0
        total_duration_secs = 0.0
        
        # Optionally show a little spinner while we probe the outputs for the summary...
        with self.console.status("[bold cyan]Analyzing output files...[/bold cyan]"):
            for i, f in enumerate(result.output_files, 1):
                size = f.stat().st_size if f.exists() else 0
                summary.add_row(str(i), f.name, format_size(size))
                total_size_bytes += size
                
                if f.exists():
                    try:
                        p = probe(f)
                        total_duration_secs += p.duration
                    except Exception:
                        pass

        self.console.print(summary)
        self.console.print(
            f"\n[bold]{result.total_parts}[/bold] parts created in "
            f"[bold]{result.elapsed_seconds:.1f}s[/bold]"
        )
        self.console.print(
            f"Total size: [bold]{format_size(total_size_bytes)}[/bold] | "
            f"Total duration: [bold]{format_time(total_duration_secs)}[/bold]\n"
        )

    def on_error(self, message: str) -> None:
        self.console.print(f"[bold red]Error:[/bold red] {message}")
