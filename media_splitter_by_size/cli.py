"""Command-line interface and interactive mode for media_splitter_by_size."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

from media_splitter_by_size import __version__
from media_splitter_by_size.console import RichProgressCallback
from media_splitter_by_size.splitter import (
    SplitOptions,
    format_size,
    parse_size,
    split_media,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="media-splitter",
        description=(
            "Split any media file into parts based on a maximum file size. "
            "Preserves all tracks with perfect synchronization. Uses ffmpeg."
        ),
        epilog=(
            "Examples:\n"
            "  media-splitter input.mp4\n"
            "  media-splitter input.mkv -s 500MB\n"
            "  media-splitter input.mp4 -s 1.5GB -o /tmp/output\n"
            "  media-splitter input.mp4 -s 700MB --video-codec libx264 --audio-codec aac\n"
            "  media-splitter --interactive\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "input",
        nargs="?",
        help="Input media file path",
    )
    parser.add_argument(
        "-s", "--max-size",
        default="2000MB",
        help="Maximum size per split part (e.g., 500MB, 1.5GB, 2000, 100KB). Default: 2000MB",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="Output directory. Defaults to the input file's directory",
    )
    parser.add_argument(
        "--video-codec", "-vc",
        default="copy",
        help="Video codec (e.g., copy, libx264, libx265, libvpx-vp9). Default: copy",
    )
    parser.add_argument(
        "--audio-codec", "-ac",
        default="copy",
        help="Audio codec (e.g., copy, aac, libopus, libmp3lame). Default: copy",
    )
    parser.add_argument(
        "--subtitle-codec", "-sc",
        default="copy",
        help="Subtitle codec (e.g., copy, srt, ass). Default: copy",
    )
    parser.add_argument(
        "--strict-sync",
        action="store_true",
        help=(
            "Enable integrity-first boundary mode. "
            "When video codec is copy, safe re-encode may be used to reduce "
            "artifacts/desync at segment boundaries."
        ),
    )
    parser.add_argument(
        "--keyframe-interval",
        type=float,
        default=None,
        help=(
            "Desired keyframe interval in seconds when video is re-encoded "
            "(ignored in pure copy mode). Example: 2.0"
        ),
    )
    parser.add_argument(
        "--safety-margin",
        type=float,
        default=0.005,
        help="Safety margin fraction (0-1) for container overhead. Default: 0.005 (0.5%%)",
    )
    parser.add_argument(
        "--overlap",
        nargs="?",
        type=float,
        const=30.0,
        default=0.0,
        help="Overlap between segments in seconds (e.g. 30). Default disabled. If flag is passed without value, defaults to 30.",
    )
    parser.add_argument(
        "--extra-args",
        nargs=argparse.REMAINDER,
        default=[],
        help="Extra ffmpeg arguments (placed after all other args)",
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Run in interactive mode with guided prompts",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed ffmpeg commands and output",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser


def interactive_mode(console: Console) -> None:
    """Run interactive mode with guided user prompts."""
    console.print()
    console.print("[bold cyan]Media Splitter by Size - Interactive Mode[/bold cyan]")
    console.print("[dim]Answer the questions below to configure the split.[/dim]\n")

    # Input file
    while True:
        input_file = Prompt.ask("[bold]Input file path[/bold]")
        input_path = Path(input_file).resolve()
        if input_path.exists():
            break
        console.print(f"[red]File not found: {input_path}[/red]")

    # Max size
    while True:
        size_str = Prompt.ask(
            "[bold]Maximum size per part[/bold]",
            default="2000MB",
        )
        try:
            max_size_bytes = parse_size(size_str)
            console.print(f"  [dim]= {format_size(max_size_bytes)}[/dim]")
            break
        except ValueError as e:
            console.print(f"[red]{e}[/red]")

    # Output directory
    output_dir_str = Prompt.ask(
        "[bold]Output directory[/bold]",
        default=str(input_path.parent),
    )
    output_dir = Path(output_dir_str).resolve()

    # Codec options
    use_custom_codecs = Confirm.ask(
        "[bold]Customize output codecs?[/bold] (default: copy = no re-encoding)",
        default=False,
    )

    video_codec = "copy"
    audio_codec = "copy"
    subtitle_codec = "copy"
    strict_sync = False

    if use_custom_codecs:
        video_codec = Prompt.ask(
            "[bold]Video codec[/bold] (copy, libx264, libx265, libvpx-vp9, ...)",
            default="copy",
        )
        audio_codec = Prompt.ask(
            "[bold]Audio codec[/bold] (copy, aac, libopus, libmp3lame, ...)",
            default="copy",
        )
        subtitle_codec = Prompt.ask(
            "[bold]Subtitle codec[/bold] (copy, srt, ass, ...)",
            default="copy",
        )

    strict_sync = Confirm.ask(
        "[bold]Prioritize perfect boundary sync/integrity over speed?[/bold]",
        default=False,
    )

    keyframe_interval_secs: float | None = None
    if strict_sync:
        keyframe_text = Prompt.ask(
            "[bold]Keyframe interval in seconds (for strict sync transcode)[/bold]",
            default="2.0",
        )
        try:
            parsed_interval = float(keyframe_text)
            if parsed_interval <= 0:
                raise ValueError("must be > 0")
            keyframe_interval_secs = parsed_interval
        except ValueError:
            console.print("[yellow]Invalid keyframe interval; using default 2.0s.[/yellow]")
            keyframe_interval_secs = 2.0

    # Overlap
    overlap = 0.0
    if Confirm.ask("[bold]Add overlap between segments?[/bold]", default=False):
        overlap_str = Prompt.ask(
            "[bold]Overlap duration in seconds[/bold]",
            default="30",
        )
        try:
            overlap = float(overlap_str)
            if overlap < 0:
                overlap = 0.0
        except ValueError:
            console.print("[yellow]Invalid overlap; using default 30.0s.[/yellow]")
            overlap = 30.0

    # Summary
    console.print()
    console.print("[bold]Configuration Summary:[/bold]")
    console.print(f"  Input:     {input_path}")
    console.print(f"  Max size:  {format_size(max_size_bytes)}")
    console.print(f"  Output:    {output_dir}")
    console.print(f"  Codecs:    v={video_codec}  a={audio_codec}  s={subtitle_codec}")
    console.print(f"  Strict:    {'enabled' if strict_sync else 'disabled'}")
    if strict_sync:
        console.print(f"  Keyframe:  {keyframe_interval_secs:.3f}s")
    if overlap > 0:
        console.print(f"  Overlap:   {overlap}s")
    console.print()

    if not Confirm.ask("[bold]Proceed with split?[/bold]", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        return

    options = SplitOptions(
        max_size_bytes=max_size_bytes,
        output_dir=output_dir,
        video_codec=video_codec,
        audio_codec=audio_codec,
        subtitle_codec=subtitle_codec,
        strict_sync=strict_sync,
        keyframe_interval_secs=keyframe_interval_secs,
        overlap=overlap,
    )

    callback = RichProgressCallback(console)
    split_media(input_path, options=options, callback=callback)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    console = Console()

    # Interactive mode
    if args.interactive or args.input is None:
        if args.input is None and not args.interactive:
            # No input file and no --interactive flag: show help or enter interactive
            if sys.stdin.isatty():
                interactive_mode(console)
                return 0
            else:
                parser.print_help()
                return 1
        interactive_mode(console)
        return 0

    # CLI mode
    input_path = Path(args.input).resolve()
    if not input_path.exists():
        console.print(f"[bold red]Error:[/bold red] File not found: {input_path}")
        return 1

    try:
        max_size_bytes = parse_size(args.max_size)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        return 1

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None

    options = SplitOptions(
        max_size_bytes=max_size_bytes,
        output_dir=output_dir,
        video_codec=args.video_codec,
        audio_codec=args.audio_codec,
        subtitle_codec=args.subtitle_codec,
        strict_sync=args.strict_sync,
        keyframe_interval_secs=args.keyframe_interval,
        overlap=args.overlap,
        safety_margin=args.safety_margin,
        extra_ffmpeg_args=args.extra_args or [],
        verbose=args.verbose,
    )

    callback = RichProgressCallback(console)

    try:
        split_media(input_path, options=options, callback=callback)
    except (RuntimeError, ValueError) as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
