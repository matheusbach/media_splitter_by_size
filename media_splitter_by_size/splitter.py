"""Core media splitting logic using ffmpeg with size-based segmentation."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from media_splitter_by_size.probe import ProbeResult, probe


# Container overhead estimates in bytes per segment (moov atom, headers, etc.)
_CONTAINER_OVERHEAD: dict[str, int] = {
    "mp4": 50_000,
    "mov": 50_000,
    "mkv": 10_000,
    "webm": 10_000,
    "avi": 12_000,
    "ts": 1_000,
    "flv": 15_000,
    "ogg": 5_000,
    "mp3": 2_000,
    "m4a": 30_000,
    "flac": 5_000,
    "wav": 1_000,
}
_DEFAULT_OVERHEAD = 30_000


@dataclass
class SegmentWriteResult:
    """Result of a single ffmpeg segment write, including sub-segment progress data."""

    samples: list[tuple[float, int]]  # (elapsed_secs, cumulative_bytes)
    aborted: bool
    hard_aborted: bool
    measured_bitrate: float | None  # bits/sec from sub-segment data, or None
    last_elapsed: float  # actual output duration from last progress sample
    checkpoint_count: int  # number of re-estimation checkpoints collected
    recommended_duration: float | None  # duration estimate from checkpoints


def _median(values: list[float]) -> float | None:
    """Return median for a non-empty list, or None for empty."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _estimate_bitrate_from_samples(samples: list[tuple[float, int]]) -> float | None:
    """Estimate bitrate from sub-segment progress samples using linear regression.

    Args:
        samples: List of (elapsed_seconds, cumulative_bytes) tuples.

    Returns:
        Estimated bitrate in bits/sec, or None if insufficient data.
    """
    filtered = [(t, s) for t, s in samples if t > 0.3]
    if len(filtered) < 2:
        return None
    n = len(filtered)
    sum_x = sum(t for t, _ in filtered)
    sum_y = sum(s for _, s in filtered)
    mean_x = sum_x / n
    mean_y = sum_y / n
    num = sum((t - mean_x) * (s - mean_y) for t, s in filtered)
    den = sum((t - mean_x) ** 2 for t, _ in filtered)
    if den == 0:
        return None
    bytes_per_sec = num / den
    if bytes_per_sec <= 0:
        return None
    return bytes_per_sec * 8  # bits/sec


def _estimate_container_overhead(extension: str) -> int:
    """Estimate container overhead for a given file extension."""
    ext = extension.lstrip(".").lower()
    return _CONTAINER_OVERHEAD.get(ext, _DEFAULT_OVERHEAD)


@dataclass
class SplitOptions:
    """Configuration options for media splitting.

    Attributes:
        max_size_bytes: Maximum size per split in bytes.
        output_dir: Directory for output files. If None, uses input file's directory.
        video_codec: Video codec for output. Use 'copy' to avoid re-encoding.
        audio_codec: Audio codec for output. Use 'copy' to avoid re-encoding.
        subtitle_codec: Subtitle codec for output. Use 'copy' to avoid re-encoding.
        strict_sync: If True, prioritizes boundary integrity over speed. When
            video is set to 'copy', it transparently switches to safe re-encode
            defaults to reduce boundary artifacts/desync risks. Disabled by
            default to avoid forced transcode unless explicitly requested.
        keyframe_interval_secs: Desired keyframe interval (in seconds) when
            video is being re-encoded. Ignored in pure stream-copy mode.
        extra_ffmpeg_args: Additional ffmpeg arguments passed to the encoding stage.
        safety_margin: Fraction (0-1) of max_size_bytes reserved as safety margin
            to account for container overhead and codec metadata. Default 0.02 (2%).
        verbose: If True, print detailed ffmpeg output.
    """

    max_size_bytes: int = 2_000 * 1024 * 1024  # 2000 MB
    output_dir: Path | None = None
    video_codec: str = "copy"
    audio_codec: str = "copy"
    subtitle_codec: str = "copy"
    strict_sync: bool = False
    keyframe_interval_secs: float | None = None
    overlap: float = 0.0
    extra_ffmpeg_args: list[str] = field(default_factory=list)
    safety_margin: float = 0.005
    verbose: bool = False


def _parse_fraction(value: str) -> float | None:
    """Parse a fraction string like '30000/1001' or a float-like value."""
    try:
        if "/" in value:
            num_str, den_str = value.split("/", 1)
            num = float(num_str)
            den = float(den_str)
            if den == 0:
                return None
            out = num / den
        else:
            out = float(value)
        if out <= 0:
            return None
        return out
    except (TypeError, ValueError):
        return None


def _infer_video_fps(info: ProbeResult) -> float | None:
    """Infer FPS from probe data for GOP/keyframe tuning."""
    for stream in info.streams:
        if not stream.is_video:
            continue
        avg = stream.extra.get("avg_frame_rate")
        if isinstance(avg, str):
            fps = _parse_fraction(avg)
            if fps:
                return fps
        raw = stream.extra.get("r_frame_rate")
        if isinstance(raw, str):
            fps = _parse_fraction(raw)
            if fps:
                return fps
    return None


@dataclass
class SplitResult:
    """Result of a media split operation.

    Attributes:
        input_file: Path to the original input file.
        output_files: List of generated output file paths in order.
        total_parts: Number of parts created.
        elapsed_seconds: Total time taken for the split operation.
    """

    input_file: Path
    output_files: list[Path]
    total_parts: int
    elapsed_seconds: float


class SplitProgressCallback:
    """Base class for progress callbacks. Subclass and override methods to receive events."""

    def on_probe_complete(self, info: ProbeResult) -> None:
        """Called after ffprobe analysis is complete."""

    def on_split_start(self, part_number: int, start_time: float) -> None:
        """Called when a new split part begins."""

    def on_split_progress(self, part_number: int, current_time: float, total_duration: float, current_size: int) -> None:
        """Called periodically with progress updates during a split."""

    def on_split_complete(self, part_number: int, output_path: Path, file_size: int, duration_secs: float, fill_ratio: float = 0.0) -> None:
        """Called when a split part is complete."""

    def on_all_complete(self, result: SplitResult) -> None:
        """Called when all splitting is done."""

    def on_error(self, message: str) -> None:
        """Called when an error occurs."""


def parse_size(size_str: str) -> int:
    """Parse a human-readable size string into bytes.

    Supports B, KB, MB, GB suffixes (case-insensitive).
    If no suffix given, treats as MB.

    Args:
        size_str: Size string like "500MB", "1.5GB", "2000", "500kb".

    Returns:
        Size in bytes.

    Raises:
        ValueError: If format is invalid.
    """
    size_str = size_str.strip()

    match = re.match(r"^(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|)$", size_str, re.IGNORECASE)
    if not match:
        raise ValueError(
            f"Invalid size format: '{size_str}'. "
            "Use format like '500MB', '1.5GB', '2000' (defaults to MB)."
        )

    value = float(match.group(1))
    unit = match.group(2).upper() if match.group(2) else "MB"

    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    return int(value * multipliers[unit])


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.2f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{size_bytes} B"


def format_time(seconds: float) -> str:
    """Format seconds into HH:MM:SS.mmm string."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _get_last_keyframe_before(input_path: Path, target_time: float) -> float | None:
    """Finds the timestamp of the last video keyframe before or at the target time."""
    # Look back up to 120 seconds to be safe on sparse GOPs
    start_search = max(0.0, target_time - 120.0)
    cmd = [
        "ffprobe", "-v", "error", 
        "-read_intervals", f"{start_search}%{target_time + 1.0}",
        "-select_streams", "v:0", 
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0", 
        str(input_path)
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        valid_kfs = []
        for line in p.stdout.splitlines():
            if "K" in line:
                try:
                    pts = float(line.split(",")[0])
                    if pts <= target_time + 0.1:  # Allow tiny floating tolerance
                        valid_kfs.append(pts)
                except ValueError:
                    pass
        if valid_kfs:
            return valid_kfs[-1]
    except Exception:
        pass
    return None


def _probe_duration(file_path: Path) -> float:
    """Get the actual duration of a media file via ffprobe.

    Returns duration in seconds as a float, or 0.0 on failure.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        file_path.as_posix(),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


def _build_output_path(input_path: Path, output_dir: Path, part_number: int) -> Path:
    """Build output file path for a given split part."""
    stem = input_path.stem
    suffix = input_path.suffix
    filename = f"{stem}_split_{part_number:03d}{suffix}"
    return output_dir / filename


def _parse_ffmpeg_progress(line: str) -> dict:
    """Parse a line of ffmpeg progress output into key-value pairs."""
    result = {}
    # Match time= pattern
    match = re.search(r"time=\s*(\d+):(\d+):(\d+\.\d+)", line)
    if match:
        h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
        result["time"] = h * 3600 + m * 60 + s

    # Match size= pattern
    match = re.search(r"size=\s*(\d+)(\w+)", line)
    if match:
        val = int(match.group(1))
        unit = match.group(2).lower()
        if unit == "kb" or unit == "kib":
            val *= 1024
        elif unit == "mb" or unit == "mib":
            val *= 1024**2
        result["size"] = val

    return result


def split_media(
    input_file: str | Path,
    max_size_mb: float | None = None,
    max_size: str | None = None,
    max_size_bytes: int | None = None,
    output_dir: str | Path | None = None,
    options: SplitOptions | None = None,
    callback: SplitProgressCallback | None = None,
) -> SplitResult:
    """Split a media file into parts that do not exceed a maximum file size.

    All tracks (video, audio, subtitle, data) are preserved. By default uses
    codec copy (no re-encoding) for optimal performance. All output parts
    maintain perfect synchronization between tracks.

    Args:
        input_file: Path to the input media file.
        max_size_mb: Maximum size per split in megabytes. Convenience parameter.
        max_size: Maximum size as human-readable string (e.g., '500MB', '1.5GB').
        max_size_bytes: Maximum size in raw bytes. Takes priority if given.
        output_dir: Directory for output files. Defaults to input file's directory.
        options: Full SplitOptions for advanced configuration.
        callback: Optional callback for progress reporting.

    Returns:
        SplitResult with information about the generated parts.

    Raises:
        FileNotFoundError: If input file doesn't exist.
        RuntimeError: If ffmpeg fails.
        ValueError: If parameters are invalid.
    """
    input_path = Path(input_file).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if options is None:
        options = SplitOptions()

    # Resolve max size from the various input methods
    if max_size_bytes is not None:
        options.max_size_bytes = max_size_bytes
    elif max_size is not None:
        options.max_size_bytes = parse_size(max_size)
    elif max_size_mb is not None:
        options.max_size_bytes = int(max_size_mb * 1024**2)

    if options.max_size_bytes <= 0:
        raise ValueError("Maximum size must be positive")

    # Resolve output directory
    if output_dir is not None:
        options.output_dir = Path(output_dir).resolve()
    if options.output_dir is None:
        options.output_dir = input_path.parent

    options.output_dir.mkdir(parents=True, exist_ok=True)

    # Probe input file
    info = probe(input_path)
    if callback:
        callback.on_probe_complete(info)

    # Calculate effective max size accounting for container overhead and safety margin
    overhead = _estimate_container_overhead(input_path.suffix)
    effective_max = int(options.max_size_bytes * (1 - options.safety_margin)) - overhead

    if effective_max <= 0:
        raise ValueError(
            f"Max size ({format_size(options.max_size_bytes)}) is too small after "
            f"accounting for container overhead and safety margin"
        )

    # Calculate segment duration based on bitrate
    total_bitrate = info.total_bitrate
    if total_bitrate <= 0:
        # Estimate from file size and duration
        if info.duration > 0:
            total_bitrate = int((info.format.size * 8) / info.duration)
        else:
            raise ValueError("Cannot determine bitrate of input file")

    # Segment duration in seconds, based on bits
    segment_duration = (effective_max * 8) / total_bitrate

    # Ensure segment is at least 1 second
    if segment_duration < 1.0:
        raise ValueError(
            f"Max size ({format_size(options.max_size_bytes)}) is too small for the "
            f"media bitrate ({total_bitrate // 1000} kbps). "
            f"Minimum ~{format_size(int(total_bitrate / 8))} needed for 1 second."
        )

    total_duration = info.duration
    if total_duration <= 0:
        raise ValueError("Cannot determine duration of input file")

    # If the file already fits, just copy it
    if info.format.size <= options.max_size_bytes:
        output_path = _build_output_path(input_path, options.output_dir, 1)
        _run_ffmpeg_segment(
            input_path, output_path, 0, total_duration, options, info, callback, 1, total_duration
        )
        elapsed = 0.0
        result = SplitResult(
            input_file=input_path,
            output_files=[output_path],
            total_parts=1,
            elapsed_seconds=elapsed,
        )
        if callback:
            callback.on_all_complete(result)
        return result

    # Split into segments with bidirectional convergence
    # The algorithm tries to fill each segment as close to max_size_bytes as possible.
    # Sub-segment size monitoring allows early abort and precise bitrate estimation,
    # converging to 98-99.5% fill per segment.
    start_time = time.monotonic()
    output_files: list[Path] = []
    current_pos = 0.0
    part_number = 1
    max_retries = 35
    
    # Aim for near-maximum fill while preserving strict no-oversize guarantee.
    # The threshold scales based on max_size_bytes because small files have fewer
    # keyframe steps (less granularity) while large files have thousands of keyframes.
    if options.max_size_bytes >= 1000 * 1024**2:
        fill_threshold = 0.992  # 99.2% for >= 1 GB
        target_fill_ratio = 0.998
    elif options.max_size_bytes >= 100 * 1024**2:
        fill_threshold = 0.985  # 98.5% for >= 100 MB
        target_fill_ratio = 0.995
    else:
        fill_threshold = 0.96   # 96.0% for tiny files
        target_fill_ratio = 0.985
        
    # Track measured bitrate across segments for better initial estimates
    last_measured_bitrate: float | None = None

    has_video = any(s.is_video for s in info.streams)
    is_stream_copy = (not has_video or options.video_codec == "copy")

    def snap_duration(duration: float, current_pos: float) -> float:
        # If the requested duration reaches the end of the video (within a small margin),
        # do not snap to a keyframe. We want to capture the remaining tail entirely.
        if duration >= remaining_duration - 0.5:
            return duration
            
        if has_video and is_stream_copy:
            kf = _get_last_keyframe_before(input_path, current_pos + duration)
            if kf is not None and kf > current_pos:
                return kf - current_pos
        return duration

    while current_pos < total_duration - 0.1:
        remaining_duration = total_duration - current_pos
        current_seg_duration = min(segment_duration, remaining_duration)
        output_path = _build_output_path(input_path, options.output_dir, part_number)

        if callback:
            callback.on_split_start(part_number, current_pos)

        # Convergence loop: refine segment duration to maximize space usage
        # without exceeding max_size_bytes
        retry = 0
        duration_lower = 0.0  # known-good lower bound (fits)
        duration_upper = 0.0  # known-bad upper bound (too large), 0 = unknown
        upper_is_hard = False # True only if the limit was physically reached, avoiding traps by soft projections
        ref_duration = current_seg_duration  # actual duration; updated by probe
        write_result: SegmentWriteResult | None = None
        tested_durations = set()
        actual_size = 0  # EXPLICITLY RESET actual_size SO IT NEVER CARRIES OVER FROM PREVIOUS PART

        while retry < max_retries:
            # OPTIMIZATION: Snap the target duration precisely to an input keyframe boundary
            # to prevent overlapping temporal segments when using stream fast-seek.
            snapped_duration = snap_duration(current_seg_duration, current_pos)
            if snapped_duration in tested_durations:
                # Because snap_duration snaps *down* (backwards in time), hitting a tested duration
                # means the entire span [snapped_duration, current_seg_duration] is empty of keyframes.
                # It would result in the exact same output file as the tested duration!
                if duration_upper > 0 and current_seg_duration < duration_upper - 0.5:
                    # Safely move our lower bound up to skip this "keyframe desert" and test higher
                    duration_lower = max(duration_lower, current_seg_duration)
                    current_seg_duration = (duration_lower + duration_upper) / 2
                    retry += 1
                    continue
                elif duration_upper == 0 or not upper_is_hard:
                    # We are trying to grow but found no new keyframes, or our ceiling is just a soft estimation.
                    # Push aggressively explicitly forward and ignore fake projection ceilings!
                    duration_lower = max(duration_lower, current_seg_duration)
                    current_seg_duration = current_seg_duration * 1.05
                    duration_upper = 0.0  # Clear any soft upper bounds preventing exploration
                    retry += 1
                    continue
                else:
                    # We've converged exactly to the best keyframe before the TRULY hard upper bound!
                    if duration_lower > 0:
                        current_seg_duration = duration_lower
                        output_path.unlink(missing_ok=True)
                        break
            else:
                current_seg_duration = snapped_duration

            tested_durations.add(current_seg_duration)

            write_result = _run_ffmpeg_segment(
                input_path, output_path,
                current_pos, current_seg_duration,
                options, info, callback, part_number, total_duration,
                max_size_bytes=options.max_size_bytes,
            )

            if write_result.aborted:
                # If aborted proactively due to variable bitrate spikes, be less
                # punishing on the duration reduction to avoid microscopic segments.
                if not write_result.hard_aborted:
                     # For soft aborts, the projection is only an estimate.
                     # We only establish a hard upper bound if we processed at least 50%
                     # of the segment, meaning the VBR projection is highly reliable.
                     if write_result.last_elapsed >= current_seg_duration * 0.5:
                         if duration_upper == 0 or current_seg_duration < duration_upper:
                             duration_upper = current_seg_duration

                     if write_result.last_elapsed < current_seg_duration * 0.3:
                         current_seg_duration = current_seg_duration * 0.95
                     elif write_result.recommended_duration and write_result.recommended_duration > 0:
                         ideal_duration = write_result.recommended_duration
                         if duration_lower > 0:
                             current_seg_duration = (duration_lower + min(ideal_duration, duration_upper)) / 2
                         else:
                             current_seg_duration = min(ideal_duration, duration_upper) * 0.999
                     else:
                         current_seg_duration = current_seg_duration * 0.95
                else:
                    # Hard abort (physically exceeded limit)
                    duration_upper = current_seg_duration
                    upper_is_hard = True
                    if write_result.recommended_duration and write_result.recommended_duration > 0:
                        ideal_duration = write_result.recommended_duration
                        if duration_lower > 0:
                            current_seg_duration = (duration_lower + min(ideal_duration, duration_upper)) / 2
                        else:
                            current_seg_duration = min(ideal_duration, duration_upper) * 0.999
                    elif write_result.measured_bitrate and write_result.measured_bitrate > 0:
                        # Use sub-segment bitrate to compute ideal duration directly
                        ideal_duration = (options.max_size_bytes * 8) / write_result.measured_bitrate
                        if duration_lower > 0:
                            current_seg_duration = (duration_lower + min(ideal_duration * 0.995, duration_upper)) / 2
                        else:
                            current_seg_duration = ideal_duration * 0.995
                    elif duration_lower > 0:
                        current_seg_duration = (duration_lower + duration_upper) / 2
                    else:
                        current_seg_duration = current_seg_duration * 0.9

                current_seg_duration = min(current_seg_duration, remaining_duration)
                retry += 1
                output_path.unlink(missing_ok=True)
                continue

            actual_size = output_path.stat().st_size
            fill_ratio = actual_size / options.max_size_bytes

            # Use the last elapsed time from ffmpeg's progress pipe as actual
            # output duration. This replaces a separate ffprobe subprocess call
            # that was spawning on every retry — even for segments about to be
            # discarded — adding seconds of latency per iteration.
            ref_duration = write_result.last_elapsed if write_result.last_elapsed > 0 else current_seg_duration

            if actual_size > options.max_size_bytes:
                # Too large — set upper bound and reduce
                duration_upper = current_seg_duration
                upper_is_hard = True
                if duration_lower > 0:
                    # Binary search: midpoint between known good and known bad
                    current_seg_duration = (duration_lower + duration_upper) / 2
                else:
                    # Use sub-segment bitrate if available for more precise reduction
                    if write_result.recommended_duration and write_result.recommended_duration > 0:
                        current_seg_duration = write_result.recommended_duration * 0.999
                    elif write_result.measured_bitrate and write_result.measured_bitrate > 0:
                        ideal_duration = (options.max_size_bytes * 8) / write_result.measured_bitrate
                        current_seg_duration = ideal_duration * 0.995
                    else:
                        ratio = options.max_size_bytes / actual_size
                        current_seg_duration = ref_duration * ratio * 0.995
                retry += 1
                output_path.unlink(missing_ok=True)

            elif fill_ratio < fill_threshold and current_seg_duration < remaining_duration - 0.5:
                # Too small and there's remaining content — try extending
                duration_lower = current_seg_duration
                if duration_upper > 0:
                    # Binary search between known good and known bad
                    current_seg_duration = (duration_lower + duration_upper) / 2
                else:
                    # Use sub-segment bitrate if available for more precise extension
                    if write_result.recommended_duration and write_result.recommended_duration > 0:
                        current_seg_duration = write_result.recommended_duration
                    elif write_result.measured_bitrate and write_result.measured_bitrate > 0:
                        ideal_duration = (options.max_size_bytes * 8) / write_result.measured_bitrate
                        current_seg_duration = ideal_duration * target_fill_ratio
                    else:
                        target_ratio = target_fill_ratio
                        current_seg_duration = ref_duration * (target_ratio / fill_ratio)
                    current_seg_duration = min(current_seg_duration, remaining_duration)
                retry += 1
                output_path.unlink(missing_ok=True)

            else:
                # Good enough — within threshold or it's the last segment
                break

        # Hard cap: never accept a segment bigger than max_size_bytes.
        # If the retry loop exhausted max_retries and the file was deleted
        # (too large / too small / aborted), it will be regenerated here.
        emergency_retries = 0
        while True:
            if not output_path.exists():
                write_result = _run_ffmpeg_segment(
                    input_path, output_path,
                    current_pos, current_seg_duration,
                    options, info, callback, part_number, total_duration,
                    max_size_bytes=options.max_size_bytes,
                )

                if write_result.aborted:
                    if write_result.recommended_duration and write_result.recommended_duration > 0:
                        current_seg_duration = write_result.recommended_duration * 0.999
                    else:
                        current_seg_duration *= 0.97
                    emergency_retries += 1
                    if emergency_retries >= 6:
                        fallback_tries = 0
                        while fallback_tries < 12:
                            current_seg_duration = max(0.2, current_seg_duration * 0.85)
                            current_seg_duration = snap_duration(current_seg_duration, current_pos)
                            output_path.unlink(missing_ok=True)
                            _run_ffmpeg_segment(
                                input_path, output_path,
                                current_pos, current_seg_duration,
                                options, info, callback, part_number, total_duration,
                                max_size_bytes=options.max_size_bytes,
                                allow_projection_abort=False,
                            )
                            if output_path.exists() and output_path.stat().st_size <= options.max_size_bytes:
                                break
                            fallback_tries += 1
                        if not output_path.exists() or output_path.stat().st_size > options.max_size_bytes:
                            raise RuntimeError(
                                "Failed to create a segment within the configured size limit "
                                "after emergency + fallback retries."
                            )
                        actual_size = output_path.stat().st_size
                        break
                    continue

            actual_size = output_path.stat().st_size
            if actual_size <= options.max_size_bytes:
                break

            # Proportional duration reduction with a small safety factor.
            ratio = options.max_size_bytes / actual_size
            current_seg_duration = max(0.2, current_seg_duration * ratio * 0.99)
            current_seg_duration = snap_duration(current_seg_duration, current_pos)
            output_path.unlink(missing_ok=True)
            emergency_retries += 1
            if emergency_retries >= 6:
                fallback_tries = 0
                while fallback_tries < 12:
                    current_seg_duration = max(0.2, current_seg_duration * 0.85)
                    current_seg_duration = snap_duration(current_seg_duration, current_pos)
                    output_path.unlink(missing_ok=True)
                    _run_ffmpeg_segment(
                        input_path, output_path,
                        current_pos, current_seg_duration,
                        options, info, callback, part_number, total_duration,
                        max_size_bytes=options.max_size_bytes,
                        allow_projection_abort=False,
                    )
                    if output_path.exists() and output_path.stat().st_size <= options.max_size_bytes:
                        break
                    fallback_tries += 1
                if not output_path.exists() or output_path.stat().st_size > options.max_size_bytes:
                    raise RuntimeError(
                        "Failed to enforce max segment size after emergency + fallback retries. "
                        f"Last size: {format_size(actual_size)} / limit: {format_size(options.max_size_bytes)}"
                    )
                actual_size = output_path.stat().st_size
                break

        # Use the requested duration (current_seg_duration) to advance position,
        # not the probed output duration.  With input-seeking (-ss before -i) and
        # codec copy, ffmpeg snaps to the nearest preceding keyframe, so the
        # output file may start slightly *before* current_pos.  The probed
        # duration reflects the output's own timeline (keyframe → keyframe) and
        # would cause current_pos to drift, producing overlapping or repeated
        # content between consecutive splits.  The requested duration correctly
        # represents the intended advance from the original current_pos.
        actual_seg_duration = current_seg_duration

        if callback:
            callback.on_split_complete(part_number, output_path, actual_size, actual_seg_duration, actual_size / options.max_size_bytes)

        output_files.append(output_path)
        
        # If this segment successfully reached the end of the video, we are done!
        # Do not process overlap that would just create tiny fragments.
        if current_pos + actual_seg_duration >= total_duration - 0.5:
            break
        
        # Advance position, accounting for overlap
        advance_duration = actual_seg_duration
        if options.overlap > 0:
            # Ensure we don't go backwards or get stuck by subtracting too much overlap
            # We must advance by at least 1 second or 10% of the segment duration, whichever is larger
            min_advance = max(1.0, actual_seg_duration * 0.1)
            advance_duration = max(min_advance, actual_seg_duration - options.overlap)
        
        current_pos += advance_duration
        part_number += 1

        # Track measured bitrate from sub-segment data
        if write_result and write_result.measured_bitrate and write_result.measured_bitrate > 0:
            last_measured_bitrate = write_result.measured_bitrate

        # Re-estimate segment duration for next segment based on actual result.
        # Prefer sub-segment measured bitrate over simple size/duration ratio.
        remaining_duration = total_duration - current_pos
        if remaining_duration > 0.1 and actual_size > 0 and actual_seg_duration > 0:
            if last_measured_bitrate and last_measured_bitrate > 0:
                segment_duration = (effective_max * 8) / last_measured_bitrate
            else:
                actual_bitrate = (actual_size * 8) / actual_seg_duration
                segment_duration = (effective_max * 8) / actual_bitrate
            segment_duration = min(segment_duration, remaining_duration)

    elapsed = time.monotonic() - start_time
    result = SplitResult(
        input_file=input_path,
        output_files=output_files,
        total_parts=len(output_files),
        elapsed_seconds=elapsed,
    )

    if callback:
        callback.on_all_complete(result)

    return result


def _build_ffmpeg_cmd(
    input_path: Path,
    output_path: Path,
    start_time: float,
    duration: float,
    options: SplitOptions,
    info: ProbeResult,
) -> list[str]:
    """Build the ffmpeg command for extracting a segment."""
    cmd = [
        "ffmpeg",
        "-y",  # overwrite output
    ]

    # Set codecs per stream type
    has_video = any(s.is_video for s in info.streams)
    has_audio = any(s.is_audio for s in info.streams)
    has_subtitle = any(s.is_subtitle for s in info.streams)

    # Integrity-first mode: avoid fragile split boundaries in stream-copy video.
    force_sync_transcode = options.strict_sync and has_video and options.video_codec == "copy"
    effective_video_codec = "libx264" if force_sync_transcode else options.video_codec
    effective_audio_codec = "aac" if (force_sync_transcode and has_audio and options.audio_codec == "copy") else options.audio_codec
    effective_subtitle_codec = options.subtitle_codec

    # ALWAYS put -ss before -i for input seeking!
    # If placed after -i in stream-copy mode, FFmpeg drops packets until the target time.
    # This causes the first keyframe to be lost if it precedes the cut point,
    # resulting in a frozen video until the next keyframe, and can break the first segment entirely.
    cmd.extend([
        "-ss", str(start_time),
        "-i", str(input_path),
        "-t", str(duration),
        "-map", "0",  # map ALL streams
    ])

    if has_video:
        cmd.extend(["-c:v", effective_video_codec])
    if has_audio:
        cmd.extend(["-c:a", effective_audio_codec])
    if has_subtitle:
        cmd.extend(["-c:s", effective_subtitle_codec])

    if force_sync_transcode:
        # Provide stable and reasonably fast defaults for boundary-safe transcode.
        if "-crf" not in options.extra_ffmpeg_args:
            cmd.extend(["-crf", "18"])
        if "-preset" not in options.extra_ffmpeg_args:
            cmd.extend(["-preset", "veryfast"])

    # Optional keyframe forcing for transcode paths (ignored in pure copy mode).
    keyframe_interval = options.keyframe_interval_secs
    if keyframe_interval is None and force_sync_transcode:
        keyframe_interval = 2.0

    if has_video and effective_video_codec != "copy" and keyframe_interval and keyframe_interval > 0:
        fps = _infer_video_fps(info) or 30.0
        gop = int(max(12, min(300, round(fps * keyframe_interval))))
        cmd.extend([
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-force_key_frames", f"expr:gte(t,n_forced*{keyframe_interval:.3f})",
        ])

    # For non-video data streams, always copy
    for s in info.streams:
        if s.codec_type not in ("video", "audio", "subtitle"):
            cmd.extend([f"-c:{s.index}", "copy"])

    # Avoid negative timestamps and ensure sync
    cmd.extend(["-avoid_negative_ts", "make_zero"])

    # Copy unknown streams
    cmd.extend(["-copy_unknown"])

    # Map metadata
    cmd.extend(["-map_metadata", "0"])

    # Add extra args
    if options.extra_ffmpeg_args:
        cmd.extend(options.extra_ffmpeg_args)

    # Progress reporting
    cmd.extend(["-progress", "pipe:1", "-nostats"])

    cmd.append(str(output_path))
    return cmd


def _run_ffmpeg_segment(
    input_path: Path,
    output_path: Path,
    start_time: float,
    duration: float,
    options: SplitOptions,
    info: ProbeResult,
    callback: SplitProgressCallback | None,
    part_number: int,
    total_duration: float,
    max_size_bytes: int | None = None,
    target_checkpoints: int = 100,
    allow_projection_abort: bool = True,
) -> SegmentWriteResult:
    """Run ffmpeg to extract a single segment with sub-segment size monitoring.

    Collects (elapsed_time, cumulative_size) samples from ffmpeg's progress pipe.
    If max_size_bytes is set, aborts early when the segment is projected to exceed
    the limit, avoiding a full write-then-check cycle.

    Returns:
        SegmentWriteResult with progress samples, abort status, and measured bitrate.
    """
    cmd = _build_ffmpeg_cmd(input_path, output_path, start_time, duration, options, info)

    if options.verbose:
        import sys
        print(f"  [cmd] {' '.join(cmd)}", file=sys.stderr)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    samples: list[tuple[float, int]] = []
    checkpoint_ideals: list[float] = []
    aborted = False
    hard_aborted = False
    current_elapsed = 0.0
    current_size = 0
    last_checkpoint_index = -1
    checkpoint_step = max(duration / max(1, target_checkpoints), 0.05)

    def _update_checkpoint_projection() -> None:
        nonlocal last_checkpoint_index
        if max_size_bytes is None or current_elapsed <= 0 or current_size <= 0:
            return
        # Ignore early unstable samples to avoid runaway duration estimates.
        if current_elapsed < max(0.25, duration * 0.05):
            return
        if current_size < 32 * 1024:
            return
        checkpoint_index = int(current_elapsed / checkpoint_step)
        if checkpoint_index <= last_checkpoint_index:
            return
        checkpoint_index = min(checkpoint_index, max(0, target_checkpoints - 1))
        last_checkpoint_index = checkpoint_index
        # Duration that would hit max_size_bytes assuming current avg bytes/sec.
        checkpoint_ideals.append((max_size_bytes * current_elapsed) / current_size)

    if process.stdout:
        for line in process.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    time_us = int(line.split("=")[1])
                    current_elapsed = time_us / 1_000_000
                    _update_checkpoint_projection()
                    current_time = start_time + current_elapsed
                    if callback:
                        callback.on_split_progress(part_number, current_time, total_duration, current_size)
                except (ValueError, IndexError):
                    pass
            elif line.startswith("total_size="):
                try:
                    current_size = int(line.split("=")[1])
                    if callback:
                        callback.on_split_progress(part_number, start_time + current_elapsed, total_duration, current_size)

                    # Record sample for bitrate estimation
                    if current_elapsed > 0:
                        samples.append((current_elapsed, current_size))
                        _update_checkpoint_projection()

                    # Early abort checks
                    if max_size_bytes is not None:
                        # Immediate abort: already exceeded limit
                        if current_size > max_size_bytes:
                            process.kill()
                            process.wait()
                            aborted = True
                            hard_aborted = True
                            break

                        # Projected abort: estimate final size from sub-segment data
                        if allow_projection_abort and current_elapsed > max(duration * 0.15, 5.0) and len(samples) >= 5:
                            # Avoid false aborts for segments that are almost finished. 
                            # If we are > 80% done, let it finish physically to guarantee accurate threshold detection.
                            if (current_elapsed / duration) > 0.80:
                                pass
                            else:
                                instant_bitrate = (current_size * 8) / current_elapsed
                                projected_size = int((instant_bitrate * duration) / 8)
                                
                                # Use wider margin early (to absorb VBR action spikes), tightening safely towards the end
                                progress_ratio = current_elapsed / duration
                                dynamic_margin = 1.02 + 0.40 * (1.0 - progress_ratio)**2
                                
                                if projected_size > max_size_bytes * dynamic_margin:
                                    process.kill()
                                    process.wait()
                                    aborted = True
                                    break
                except (ValueError, IndexError):
                    pass

    if aborted:
        output_path.unlink(missing_ok=True)

    if not aborted:
        process.wait()

        if process.returncode != 0:
            stderr = process.stderr.read() if process.stderr else ""
            # If subtitle codec fails (common with mov_text->mkv), retry without subtitles
            if "subtitle" in stderr.lower() or "tx3g" in stderr.lower():
                cmd_no_sub = [c for c in cmd if c != "-copy_unknown"]
                new_cmd = []
                skip_next = False
                for c in cmd_no_sub:
                    if skip_next:
                        skip_next = False
                        continue
                    if c == "-c:s":
                        skip_next = True
                        continue
                    new_cmd.append(c)

                # Replace -map 0 with individual stream maps (skip subtitles)
                final_cmd = []
                for c in new_cmd:
                    if c == "-map" and len(final_cmd) == 0 or (final_cmd and final_cmd[-1] != "-map"):
                        final_cmd.append(c)
                    elif final_cmd and final_cmd[-1] == "-map" and c == "0":
                        final_cmd.pop()
                        for s in info.streams:
                            if not s.is_subtitle:
                                final_cmd.extend(["-map", f"0:{s.index}"])
                    else:
                        final_cmd.append(c)

                process2 = subprocess.Popen(
                    final_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                process2.wait()
                if process2.returncode != 0:
                    stderr2 = process2.stderr.read() if process2.stderr else ""
                    raise RuntimeError(f"ffmpeg failed: {stderr2}")
            else:
                raise RuntimeError(f"ffmpeg failed: {stderr}")

    measured_bitrate = _estimate_bitrate_from_samples(samples)
    recommended_duration = None
    # Prioritize recent checkpoints for responsiveness to bitrate shifts.
    if checkpoint_ideals:
        tail = checkpoint_ideals[-min(25, len(checkpoint_ideals)) :]
        med = _median(tail)
        if med and med > 0:
            candidate = med * 0.998
            # Keep recommendation near the currently tested duration so retries
            # converge smoothly and never jump to absurd values.
            min_candidate = max(0.2, duration * 0.6)
            max_candidate = max(min_candidate, duration * 1.2)
            recommended_duration = min(max(candidate, min_candidate), max_candidate)

    return SegmentWriteResult(
        samples=samples, aborted=aborted, hard_aborted=hard_aborted,
        measured_bitrate=measured_bitrate,
        last_elapsed=current_elapsed,
        checkpoint_count=len(checkpoint_ideals),
        recommended_duration=recommended_duration,
    )
