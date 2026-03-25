# Technical Documentation — Media Splitter by Size

## Architecture Overview

Media Splitter by Size is structured as a modular Python package with clean separation of concerns:

```
media_splitter_by_size/
├── __init__.py     → Public API surface (split_media, SplitOptions, SplitResult)
├── probe.py        → ffprobe wrapper with structured data models
├── splitter.py     → Core splitting engine with size-aware segmentation
├── console.py      → Rich terminal UI with progress tracking
└── cli.py          → CLI argument parsing and interactive mode
```

### Data Flow

```
Input File → probe.py (ffprobe) → ProbeResult
                                      ↓
                                splitter.py
                                      ↓
                              Calculate segment duration
                              from bitrate + max size
                                      ↓
                              ┌─── ffmpeg segment ──────────────────┐
                              │       │                             │
                              │       ├── Monitor progress pipe     │
                              │       │   (sub-segment samples)     │
                              │       │                             │
                              │       ├── Early abort if projected  │
                              │       │   size exceeds limit        │
                              │       ↓                             │
                              │   Verify size + estimate bitrate    │
                              │       │                             │
                              │       ├── Retry if too large ───────┘
                              │       ├── Retry if < 98% fill ──────┘
                              │       ↓
                              │  Output file (near-max target)
                              │       ↓
                              └── Next segment (adaptive bitrate) ←┘
                                      ↓
                                SplitResult
```

---

## Module Reference

### `probe.py` — Media File Analysis

This module wraps `ffprobe` to extract structured information about media files.

#### Classes

##### `StreamInfo`

Immutable dataclass representing one stream (track) in a media file.

| Field | Type | Description |
|-------|------|-------------|
| `index` | `int` | Stream index (0-based) |
| `codec_name` | `str` | Short codec name (e.g., `h264`, `aac`, `mov_text`) |
| `codec_long_name` | `str` | Full codec description |
| `codec_type` | `str` | Stream type: `video`, `audio`, `subtitle`, `data`, `attachment` |
| `codec_tag_string` | `str` | FourCC or codec tag (e.g., `avc1`, `mp4a`) |
| `bit_rate` | `int` | Bitrate in bits/sec (0 if unknown) |
| `duration` | `float` | Stream duration in seconds |
| `language` | `str` | ISO 639 language code (e.g., `eng`, `por`, `und`) |
| `handler_name` | `str` | Handler name from container metadata |
| `profile` | `str` | Codec profile (e.g., `Main`, `LC`, `High`) |
| `extra` | `dict` | Additional fields: `width`, `height`, `sample_rate`, `channels`, `channel_layout`, `pix_fmt`, `r_frame_rate`, `avg_frame_rate`, `level`, `extradata_size` |

Properties:
- `is_video` → `bool`: True if video stream
- `is_audio` → `bool`: True if audio stream
- `is_subtitle` → `bool`: True if subtitle stream
- `display_name` → `str`: Human-readable description like `#0 video: h264 (eng) - VideoHandler @ 3527kbps`

##### `FormatInfo`

Immutable dataclass with container-level information.

| Field | Type | Description |
|-------|------|-------------|
| `filename` | `str` | Input file path |
| `format_name` | `str` | Short format names (comma-separated for multi-format) |
| `format_long_name` | `str` | Full format description |
| `duration` | `float` | Total duration in seconds |
| `size` | `int` | File size in bytes |
| `bit_rate` | `int` | Overall bitrate in bits/sec |
| `nb_streams` | `int` | Number of streams |
| `tags` | `dict` | Container-level metadata tags |

##### `ProbeResult`

Combined probe result.

| Field | Type | Description |
|-------|------|-------------|
| `format` | `FormatInfo` | Container format information |
| `streams` | `list[StreamInfo]` | All streams in the file |

Properties:
- `video_streams` → `list[StreamInfo]`: Filter video streams
- `audio_streams` → `list[StreamInfo]`: Filter audio streams
- `subtitle_streams` → `list[StreamInfo]`: Filter subtitle streams
- `total_bitrate` → `int`: Total bitrate (sum of streams, or format bitrate if streams unavailable)
- `duration` → `float`: File duration in seconds

#### Functions

##### `probe(file_path: Path) → ProbeResult`

Analyze a media file using ffprobe.

**Raises:**
- `FileNotFoundError` — Input file doesn't exist
- `RuntimeError` — ffprobe not installed or failed to parse

---

### `splitter.py` — Core Splitting Engine

The heart of the application. Handles size-based media segmentation with adaptive bitrate estimation.

#### Classes

##### `SplitOptions`

Configuration dataclass for controlling split behavior.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_size_bytes` | `int` | `2000 * 1024²` | Maximum size per split in bytes |
| `output_dir` | `Path \| None` | `None` | Output directory (None = input file's directory) |
| `video_codec` | `str` | `"copy"` | Video codec passed to ffmpeg `-c:v` |
| `audio_codec` | `str` | `"copy"` | Audio codec passed to ffmpeg `-c:a` |
| `subtitle_codec` | `str` | `"copy"` | Subtitle codec passed to ffmpeg `-c:s` |
| `strict_sync` | `bool` | `False` | Opt-in boundary integrity mode; when video is `copy`, uses safe re-encode defaults to reduce artifacts/desync |
| `keyframe_interval_secs` | `float \| None` | `None` | Keyframe interval during video transcode; ignored for pure copy |
| `overlap` | `float` | `0.0` | Overlap between segments in seconds |
| `extra_ffmpeg_args` | `list[str]` | `[]` | Additional ffmpeg arguments for encoding |
| `safety_margin` | `float` | `0.005` | Fraction of max size reserved for overhead (0.0–1.0) |
| `verbose` | `bool` | `False` | Print ffmpeg commands |

##### `SplitResult`

Result dataclass returned after splitting.

| Field | Type | Description |
|-------|------|-------------|
| `input_file` | `Path` | Original input file path |
| `output_files` | `list[Path]` | Generated output file paths (ordered) |
| `total_parts` | `int` | Number of parts created |
| `elapsed_seconds` | `float` | Total wall-clock time for the operation |

##### `SplitProgressCallback`

Base class for receiving progress events. Override methods to implement custom progress reporting.

| Method | Called When |
|--------|-------------|
| `on_probe_complete(info)` | After ffprobe analysis |
| `on_split_start(part_number, start_time)` | New segment begins |
| `on_split_progress(part_number, current_time, total_duration, current_size)` | Periodic progress update |
| `on_split_complete(part_number, output_path, file_size, duration_secs)` | Segment finished |
| `on_all_complete(result)` | All splitting done |
| `on_error(message)` | Error occurred |

#### Functions

##### `split_media(input_file, ...) → SplitResult`

Main splitting function. Accepts multiple ways to specify the maximum size:

```python
# Any ONE of these:
split_media("file.mp4", max_size_mb=500)          # Float MB
split_media("file.mp4", max_size="1.5GB")          # Human string
split_media("file.mp4", max_size_bytes=524288000)  # Raw bytes
split_media("file.mp4", options=SplitOptions(...))  # Full config

# With callback:
split_media("file.mp4", max_size_mb=500, callback=my_callback)
```

Priority when multiple size params given: `max_size_bytes` > `max_size` > `max_size_mb` > options default.

##### `parse_size(size_str: str) → int`

Parse a human-readable size string to bytes. Supports: `B`, `KB`, `MB`, `GB` suffixes (case-insensitive). No suffix = MB.

##### `format_size(size_bytes: int) → str`

Format bytes to human-readable string (e.g., `"1.50 GB"`, `"500.00 MB"`).

##### `format_time(seconds: float) → str`

Format seconds to `HH:MM:SS.mmm`.

---

## Technical Edge Cases & Algorithmic Solutions

### 1. Absolute Keyframe Snapping (Overlap Prevention)
When using `video_codec = "copy"` combined with input-seeking (`ffmpeg -ss` before `-i`), native fast-seek behavior forces the requested cut to the *preceding* keyframe. Unchecked, this causes chunks to have visually overlapping or repeated seconds at the boundaries. 
* **The Solution:** The engine utilizes a pre-pass `ffprobe` scraper (`_get_last_keyframe_before`) to read the exact PTS time flags (`K`) around the target boundary constraint. It forcibly snaps the target duration (`snap_duration`) precisely to that integer, ensuring the next cycle starts *exactly* where the last cycle ended without duplicate frames.
* **Terminal Override:** To prevent "ghost loops" where a file within seconds of its end snaps backwards indefinitely, if the target is within `0.5s` of the total run duration, the bounds snapping is bypassed completely, enabling the clip to absorb the final fractions of a second naturally.

### 2. Variable Bitrate (VBR) Dynamic Projection
High-variance VBR videos encode unpredictably. To ensure size boundaries are strictly upheld, a subprocess listener reads `ffmpeg -progress pipe:1` logs constantly.
* **Early Abort:** If an ongoing split projects to physically exceed the byte cap, it is aggressively aborted.
* **Dynamic Margins & Projection Override:** Because initial frames often spike in size, a flat abort-cap would cause false-alarms, incorrectly assuming massive file overflows and punishing the duration down. Therefore, a sliding quadratic margin scales linearly along the time track (`dynamic_margin = 1.02 + 0.40 * (1.0 - progress_ratio)**2`). This prevents runaway variables at the start of encoding (0-10%) by tolerating high burst margins, but tightens safely as it approaches the end. Furthermore, if the clip is >80% physically finished processing but projects a slight overflow, the early-abort algorithm shuts off completely (`pass`), allowing the absolute physical byte size to serve as the undeniable truth, avoiding "false abort" penalties.

### 3. Keyframe Desert Bypassing (Search Stagnation Fix)
In streams with large irregular gaps between `K` frames (Keyframe Deserts), conventional binary searches (averaging `lower` and `upper` durations) frequently snap backwards to the exact same starting point (`snapped_duration in tested_durations`), trapping the convergence loop in an artificial ceiling (yielding files with only `90-93%` fill). 
By structurally proving that a snapped-down loop indicates an empty subset of `[snapped_duration, current]`, the logic aggressively bumps the `duration_lower` bound forward and shifts the mid-point up `(duration_lower + duration_upper) / 2` without re-testing identical file states. This securely pushes the chunks to absolute 98%+ maximization regardless of how sparsely the keyframes were placed.

### 4. State Isolation during Fallback Operations
When variables spike heavily, the retry loop activates deep mathematical "emergency fallbacks". To ensure reporting logic and boundary states don't corrupt during `break`/`continue` loop conditions, properties like `actual_size` assert a rigid hard-read to the OS `st_size` properties immediately before escaping boundaries, guaranteeing the user console always displays the absolute disk truth rather than memory artifacts. Furthermore, soft projections no longer immediately bind a permanent `duration_upper`, avoiding convergence traps that would otherwise artificially crush sizes over a single bitrate spike.

### 5. Temporal Segment Overlap (`--overlap`)
The user can optionally specify an overlap duration (e.g. 30s) to be maintained between adjacent parts.
* **Math Safety Constraints:** To prevent extreme regressions where a file that ended prematurely or is too small begins overlapping backwards onto itself, the progression logic enforces `advance_duration = max(min_advance, actual_seg_duration - options.overlap)`. We apply a strict math minimum boundary `max(1.0, actual_seg_duration * 0.1)` guaranteeing that the pointer functionally yields real forward progress to prevent infinite negative looping.
* **Tail-end Breaking:** When the logic recognizes a finished physical read spanning the final frame of the whole video (with `0.5s` of grace), it aggressively terminates progress via `break`, explicitly skipping any mathematical overlap recalculations.

### 6. Dynamic Fill Scaling
A fixed `%` threshold (e.g. 98%) translates completely differently based on scale. In a `5MB` segment, missing 2% signifies 100KB; but on a `2GB` movie split, missing 2% signifies throwing away `40MB` of usable space. Splitting constraints dynamically alter their completion acceptance thresholds directly correlated to the target byte dimension. `1GB` and above must adhere strictly to `99.2%` capacities with aggressive `99.8%` targeting loops, whereas smaller items utilize `96/98.5` ratios to preserve loop velocity.

---

### `console.py` — Rich Console Output

##### `RichProgressCallback(SplitProgressCallback)`

Implements progress reporting with [Rich](https://rich.readthedocs.io/):

- Renders file information in a styled panel
- Displays a table of all detected streams with metadata
- Shows a live progress bar with percentage, elapsed time, and ETA
- Keeps progress monotonic (never moves backwards), even when a segment is retried
- Logs each split part completion (with file name, size, fill %, and exact time spanning details: `start`/`end`/`duration`)
- Produces a summary table when all splits complete (triggering a final real-time `probe` pass over the exported outputs to display a completely verified aggregated size and exact combined elapsed timeline).

---

### `cli.py` — Command-Line Interface

##### `build_parser() → ArgumentParser`

Constructs the argparse parser with all CLI arguments.

##### `interactive_mode(console: Console) → None`

Runs the interactive guided mode using Rich prompts:
1. Prompts for input file (validates existence)
2. Prompts for maximum size (validates format)
3. Prompts for output directory
4. Asks if custom codecs are wanted
5. Shows configuration summary
6. Asks for confirmation

##### `main(argv: list[str] | None = None) → int`

Entry point. Returns 0 on success, 1 on error.

If called with no arguments and stdin is a TTY, automatically enters interactive mode.

---

## Splitting Algorithm — Detailed

### 1. Segment Duration Calculation

```
effective_max = max_size_bytes × (1 - safety_margin) - container_overhead
segment_duration = (effective_max × 8) / total_bitrate
```

Where:
- `safety_margin` defaults to 0.005 (0.5%) — kept small because the convergence loop handles accuracy
- `container_overhead` is estimated per format (e.g., 50KB for MP4, 10KB for MKV)
- `total_bitrate` is the sum of all stream bitrates (or estimated from file size / duration)

### 2. Segment Extraction

Each segment is extracted with:

```
ffmpeg -y -ss {start} -i {input} -t {duration} \
    -map 0 \
    -c:v {video_codec} -c:a {audio_codec} -c:s {subtitle_codec} \
    -avoid_negative_ts make_zero \
    -copy_unknown \
    -map_metadata 0 \
    -progress pipe:1 -nostats \
    {output}
```

Key ffmpeg flags:
- `-ss` after `-i` in stream-copy mode: More precise boundaries and less keyframe overlap drift
- Avoid forcing non-keyframe copying at segment start: reduces visual artifacts in the first frames
- Copy-mode behavior remains source-dependent at keyframe boundaries; strict-sync mode is available for boundary-safe re-encode when needed
- Integrity-first boundary mode (`strict_sync=True`, opt-in): if video codec is `copy`, uses `libx264` (and `aac` when audio codec is also `copy`) to reduce boundary artifacts, freezes, and desync risks
- Keyframe forcing on transcode paths: applies `-g`, `-keyint_min`, `-sc_threshold 0`, and `-force_key_frames` using the configured interval (strict mode defaults to 2.0s)
- `-map 0`: Maps ALL streams from input (video, audio, subtitle, data, attachments)
- `-avoid_negative_ts make_zero`: Prevents timestamp drift at segment boundaries
- `-copy_unknown`: Preserves unrecognized stream types
- `-map_metadata 0`: Copies all metadata from input
- `-progress pipe:1 -nostats`: Enables progress reporting via stdout for sub-segment monitoring

### 3. Sub-Segment Size Monitoring

During each segment write, the algorithm monitors ffmpeg's progress pipe to collect `(elapsed_time, cumulative_size)` samples in real time. It also performs dense cut-point re-estimation using up to **100 checkpoints per segment** (estimated by duration and updated by size at each checkpoint). This enables:

1. **Early abort**: If the current size already exceeds `max_size_bytes`, the ffmpeg process is killed immediately. If the projected final size (based on instantaneous bitrate) exceeds the limit by more than 0.2%, the process is also killed. This avoids wasting time writing a segment that will be discarded.
2. **Checkpoint-based duration recommendation**: At each checkpoint, the algorithm computes the duration that would hit the max size using current size/time data. A robust median over recent checkpoints yields a recommended next cut duration, bounded around the current test duration to prevent runaway jumps.
3. **Precise bitrate estimation**: The collected samples are used to compute a local bitrate via linear regression (least squares fit over data points with elapsed time > 0.3s to skip initialization overhead). This measured bitrate is more accurate than a simple `file_size / duration` ratio, especially for variable bitrate content.

```
Projection check (every total_size= update from ffmpeg):
    instant_bitrate = (current_size × 8) / elapsed_time
    projected_size = (instant_bitrate × requested_duration) / 8
    if projected_size > max_size_bytes × 1.002 → ABORT (kill process)
    if current_size > max_size_bytes → ABORT (kill process)
```

### 4. Bidirectional Convergence Loop

After each segment write (or early abort), the algorithm decides whether to accept, shrink, or extend:

1. **Aborted early**: Uses the measured bitrate from sub-segment samples to compute the ideal duration directly: `ideal = (max_size × 8) / measured_bitrate`, applies a 0.5% safety factor, and retries
2. **Too large** (exceeds `max_size_bytes`): Sets an upper bound on duration; if sub-segment bitrate is available, computes ideal duration from it; otherwise reduces proportionally with a 0.5% safety factor
3. **Too small** (below 98% of `max_size_bytes` and more content remains): Sets a lower bound; prefers checkpoint-recommended duration, then bitrate estimate, then proportional extension targeting ~99.5% fill
4. **Good enough** (>= 98% fill or last segment): Accepts the result in the convergence stage
5. **Hard cap pass**: After convergence, a strict enforcement loop rewrites oversize outputs until they fit the configured limit. If emergency retries are exhausted, a conservative fallback rewrite loop disables projection-based abort and shrinks duration more aggressively.

When both upper and lower duration bounds are known, the algorithm uses **binary search** (midpoint between bounds) for fast convergence. Otherwise, it uses **sub-segment bitrate estimation** (preferred) or **proportional estimation** as fallback.

```
if aborted:
    upper_bound = current_duration
    ideal = (max_size × 8) / measured_bitrate
    next = ideal × 0.995                        # if no lower bound
    next = midpoint(lower, min(ideal, upper))    # if lower bound known

elif actual_size > max_size_bytes:
    upper_bound = current_duration
    next = midpoint(lower, upper)                         # if both bounds known
    next = (max_size × 8) / measured_bitrate × 0.995      # if bitrate available
    next = duration × (max / actual) × 0.995              # otherwise (proportional)

elif fill_ratio < 0.98 and more_content_remains:
    lower_bound = current_duration
    next = midpoint(lower, upper)                         # if both bounds known
    next = (max_size × 8) / measured_bitrate × 0.995      # if bitrate available
    next = duration × (0.995 / fill_ratio)                # otherwise (proportional)

else:
    accept segment
```

Up to 35 refinement iterations (`max_retries = 35`) per segment, plus bounded emergency retries for strict hard-cap enforcement. Dense checkpoint recommendations usually reduce trial-and-error by guiding retries toward the best cut point faster.

This approach targets near-maximum dynamic fill (scaling up to >=99.2% for >1GB outputs) while preserving a strict no-oversize guarantee.

### 5. Adaptive Bitrate

After each successful segment, the algorithm preferentially uses the **measured bitrate from sub-segment data** (computed via linear regression over many progress samples) for the next segment's initial estimate. This provides a much more granular estimate than a simple ratio:

```
# Preferred: sub-segment measured bitrate (many data points)
if measured_bitrate available:
    next_segment_duration = (effective_max × 8) / measured_bitrate

# Fallback: simple ratio from completed segment
else:
    actual_bitrate = (actual_size × 8) / actual_duration
    next_segment_duration = (effective_max × 8) / actual_bitrate
```

The measured bitrate is tracked across segments (`last_measured_bitrate`), so even the first iteration of a new segment benefits from the sub-segment data of the previous one. The convergence loop then refines it further if needed.

---

## Container Overhead Estimates

Overhead bytes subtracted from max size per format:

| Format | Overhead | Reason |
|--------|----------|--------|
| MP4/MOV | 50 KB | moov atom, ftyp, mdat headers |
| MKV | 10 KB | EBML header, segment info |
| WebM | 10 KB | Similar to MKV |
| AVI | 12 KB | RIFF header, index |
| TS | 1 KB | Minimal container overhead |
| FLV | 15 KB | FLV header, script data |
| OGG | 5 KB | OGG pages overhead |
| MP3 | 2 KB | ID3 tags |
| M4A | 30 KB | moov atom for audio |
| FLAC | 5 KB | Metadata blocks |
| Other | 30 KB | Conservative default |

---

## Codec Metadata Handling

When using `copy` mode (default), ffmpeg preserves:
- **H.264/H.265**: SPS/PPS NAL units in the extradata, ensuring each segment starts with proper sequence headers
- **AAC**: AudioSpecificConfig in extradata, preserved in each segment's container
- **Subtitles**: Format-specific metadata (e.g., tx3g atoms for mov_text)

When re-encoding, ffmpeg writes fresh codec initialization data at the start of each segment, so metadata is inherently correct.

### Known Limitations

- **mov_text subtitles** cannot be muxed into MKV/WebM containers. The splitter automatically retries without subtitles if this codec error is detected
- **DTS timestamps**: In rare cases with very long B-frame sequences, the first frame of a segment may have slight reorder; this is handled by ffmpeg's `-avoid_negative_ts`
- Keyframe boundaries: In `copy` mode, segments could overlap due to fast-seek keyframe snapping. The tool mitigates this completely by automatically polling `ffprobe` prior to each segment cut and internally forcing the `duration` values to land mathematically EXACTLY on keyframe grid boundaries. This zeroes temporal drift, removes freezes, and achieves mathematically perfect sequential playback without re-encoding!

---

## Track Synchronization Details

### The Problem
Media files contain multiple independent tracks (video, audio, subtitles) that must remain synchronized. Each track has its own timestamps (PTS — Presentation Timestamps). When cutting, all tracks must be cut at the exact same point to prevent:
- Audio/video desync
- Missing or duplicated frames
- Subtitle timing drift

### The Solution
This splitter uses ffmpeg's built-in multiplexer synchronization:

1. **Single seek point**: One `-ss` value for all streams — ffmpeg handles per-stream alignment
2. **Single duration**: One `-t` value — ffmpeg truncates all streams at the same wall-clock duration
3. **Timestamp normalization**: `-avoid_negative_ts make_zero` resets all stream timestamps to 0 at segment start
4. **All-stream mapping**: `-map 0` ensures no stream is accidentally dropped

The result is that each output segment is a self-contained, properly synchronized media file.

---

## Error Handling

| Error | Behavior |
|-------|----------|
| Input file not found | `FileNotFoundError` raised immediately |
| ffprobe not installed | `RuntimeError` with installation instructions |
| ffprobe parse failure | `RuntimeError` with ffprobe stderr |
| ffmpeg not installed | `RuntimeError` from subprocess |
| Segment too large | Automatic retry with reduced duration (up to 8 attempts), with early abort to save time |
| Subtitle codec incompatible | Automatic retry without subtitle streams |
| Max size too small | `ValueError` with minimum size hint |
| Zero/negative duration | `ValueError` with descriptive message |
| Unknown bitrate | Falls back to `file_size / duration` estimation |

---

## Performance Characteristics

### Copy Mode (default)
- **Speed**: 10–100× faster than real-time (I/O bound)
- **Quality**: Lossless — bit-identical streams
- **CPU usage**: Minimal (only demux/remux)
- **Disk I/O**: Sequential read + write (approximately 2× file size total)

### Re-encoding Mode
- **Speed**: Varies by codec (libx264 ~2-10× real-time, libx265 ~0.5-3× real-time)
- **Quality**: Depends on codec settings (CRF, preset)
- **CPU usage**: High (encoder-dependent)
- **Size accuracy**: Better — re-encoded segments match bitrate targets more precisely

### Memory Usage
- Constant regardless of file size — ffmpeg processes data in streaming fashion
- Probe data is minimal (few KB per stream)
- No temporary files beyond the output segments

---

## Python API Examples

### Basic Split

```python
from media_splitter_by_size import split_media

result = split_media("video.mp4", max_size_mb=500)
print(f"{result.total_parts} parts in {result.elapsed_seconds:.1f}s")
for f in result.output_files:
    print(f"  {f}")
```

### Custom Progress Callback

```python
from media_splitter_by_size.splitter import SplitProgressCallback, split_media

class MyProgress(SplitProgressCallback):
    def on_split_start(self, part, start_time):
        print(f"Starting part {part}...")

    def on_split_complete(self, part, path, size, duration):
        print(f"Part {part}: {size / 1024**2:.0f} MB")

    def on_all_complete(self, result):
        print(f"Done! {result.total_parts} parts")

split_media("video.mp4", max_size_mb=500, callback=MyProgress())
```

### Re-encoding with Custom Options

```python
from pathlib import Path
from media_splitter_by_size import split_media, SplitOptions

options = SplitOptions(
    max_size_bytes=200 * 1024**2,  # 200 MB
    output_dir=Path("/tmp/output"),
    video_codec="libx264",
    audio_codec="aac",
    extra_ffmpeg_args=["-crf", "23", "-preset", "medium"],
    safety_margin=0.05,  # 5% margin for re-encoding uncertainty
)

result = split_media("input.mkv", options=options)
```

### Probe-only (No Split)

```python
from media_splitter_by_size.probe import probe
from pathlib import Path

info = probe(Path("video.mp4"))
print(f"Duration: {info.duration:.0f}s")
print(f"Bitrate: {info.total_bitrate // 1000} kbps")
for s in info.streams:
    print(f"  {s.display_name}")
```
