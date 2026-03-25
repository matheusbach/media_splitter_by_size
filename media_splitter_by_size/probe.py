"""Probe media files using ffprobe to extract stream and format information."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class StreamInfo:
    """Information about a single stream in a media file."""

    index: int
    codec_name: str
    codec_long_name: str
    codec_type: str  # video, audio, subtitle, data, attachment
    codec_tag_string: str
    bit_rate: int  # bits per second, 0 if unknown
    duration: float  # seconds
    language: str
    handler_name: str
    profile: str
    extra: dict = field(default_factory=dict)

    @property
    def is_video(self) -> bool:
        return self.codec_type == "video"

    @property
    def is_audio(self) -> bool:
        return self.codec_type == "audio"

    @property
    def is_subtitle(self) -> bool:
        return self.codec_type == "subtitle"

    @property
    def display_name(self) -> str:
        lang = f" ({self.language})" if self.language != "und" else ""
        handler = f" - {self.handler_name}" if self.handler_name else ""
        bitrate = f" @ {self.bit_rate // 1000}kbps" if self.bit_rate else ""
        return f"#{self.index} {self.codec_type}: {self.codec_name}{lang}{handler}{bitrate}"


@dataclass(frozen=True)
class FormatInfo:
    """Information about the container format of a media file."""

    filename: str
    format_name: str
    format_long_name: str
    duration: float  # seconds
    size: int  # bytes
    bit_rate: int  # bits per second
    nb_streams: int
    tags: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    """Complete probe result for a media file."""

    format: FormatInfo
    streams: list[StreamInfo]

    @property
    def video_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.is_video]

    @property
    def audio_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.is_audio]

    @property
    def subtitle_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.is_subtitle]

    @property
    def total_bitrate(self) -> int:
        """Total bitrate across all streams in bits/sec."""
        stream_sum = sum(s.bit_rate for s in self.streams)
        if stream_sum > 0:
            return stream_sum
        return self.format.bit_rate

    @property
    def duration(self) -> float:
        return self.format.duration


def probe(file_path: Path) -> ProbeResult:
    """Probe a media file and return structured information about its streams and format.

    Args:
        file_path: Path to the media file to probe.

    Returns:
        ProbeResult with format and stream information.

    Raises:
        FileNotFoundError: If the file does not exist.
        RuntimeError: If ffprobe fails to analyze the file.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe not found. Please install ffmpeg: https://ffmpeg.org/download.html"
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed for {file_path}: {e.stderr}")

    data = json.loads(result.stdout)

    fmt_data = data.get("format", {})
    format_info = FormatInfo(
        filename=fmt_data.get("filename", str(file_path)),
        format_name=fmt_data.get("format_name", "unknown"),
        format_long_name=fmt_data.get("format_long_name", "unknown"),
        duration=float(fmt_data.get("duration", 0)),
        size=int(fmt_data.get("size", 0)),
        bit_rate=int(fmt_data.get("bit_rate", 0)),
        nb_streams=int(fmt_data.get("nb_streams", 0)),
        tags=fmt_data.get("tags", {}),
    )

    streams = []
    for s in data.get("streams", []):
        tags = s.get("tags", {})
        streams.append(StreamInfo(
            index=int(s.get("index", 0)),
            codec_name=s.get("codec_name", "unknown"),
            codec_long_name=s.get("codec_long_name", "unknown"),
            codec_type=s.get("codec_type", "unknown"),
            codec_tag_string=s.get("codec_tag_string", ""),
            bit_rate=int(s.get("bit_rate", 0)),
            duration=float(s.get("duration", fmt_data.get("duration", 0))),
            language=tags.get("language", "und"),
            handler_name=tags.get("handler_name", ""),
            profile=s.get("profile", ""),
            extra={
                k: s[k] for k in (
                    "width", "height", "sample_rate", "channels",
                    "channel_layout", "pix_fmt", "r_frame_rate",
                    "avg_frame_rate", "level", "extradata_size",
                ) if k in s
            },
        ))

    return ProbeResult(format=format_info, streams=streams)
