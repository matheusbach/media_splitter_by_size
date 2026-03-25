"""
Media Splitter by Size - Split any media file into parts based on maximum file size.

Preserves all tracks (video, audio, subtitle, data) with perfect synchronization.
Uses ffmpeg for reliable media processing with codec copy (no re-encoding) by default.

Usage as library:
    from media_splitter_by_size import split_media

    parts = split_media("input.mp4", max_size_mb=500)
    parts = split_media("input.mkv", max_size_mb=200, output_dir="/tmp/output")
"""

from media_splitter_by_size.splitter import split_media, SplitOptions, SplitResult

__version__ = "1.0.0"
__all__ = ["split_media", "SplitOptions", "SplitResult", "__version__"]
