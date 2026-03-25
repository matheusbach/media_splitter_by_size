# Media Splitter by Size

![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![ffmpeg](https://img.shields.io/badge/dependency-ffmpeg-orange.svg)

Split video and audio files into parts with a maximum file size, while preserving all streams (video, audio, subtitles, data) and keeping synchronization.

Built on ffmpeg. Default mode avoids forced transcode for speed. Optional `strict sync` mode can be enabled when boundary integrity is more important than processing time.

## Why this project

- Split large files for uploads with strict size limits
- Preserve all streams with `-map 0` (including subtitles/data when present)
- Keep each part below the target size with an adaptive convergence loop
- Enforce a strict hard cap so accepted parts never exceed the configured max size
- Optional boundary-integrity mode (opt-in) for stricter segment edge reliability
- Optional keyframe insertion control during transcode for cleaner segment starts
- Display a monotonic progress bar (no backward jumps during retries)
- Use as CLI tool or as a Python library

## Install

### Requirements

- Python 3.12+
- ffmpeg and ffprobe available in PATH

### Local setup

```bash
git clone https://github.com/matheusbach/media_splitter_by_size.git
cd media_splitter_by_size
uv sync
```

### Alternative

```bash
pip install .
```

## Quick start

### CLI

```bash
# Default target: 2000MB
media-splitter input.mp4

# Split into 500MB parts
media-splitter input.mp4 -s 500MB

# Custom output directory
media-splitter input.mp4 -s 1.5GB -o /path/to/output

# Interactive mode
media-splitter --interactive

# Run without install
uv run python -m media_splitter_by_size.cli input.mp4 -s 500MB
```

### Python API

```python
from media_splitter_by_size import split_media

result = split_media("input.mp4", max_size="500MB")
print(result.total_parts)
for file in result.output_files:
    print(file)
```

## Size formats

| Input | Meaning |
|---|---|
| `5000000B` | Bytes |
| `500KB` | Kibibytes (`1024`) |
| `500MB` | Mebibytes (`1024^2`) |
| `1.5GB` | Gibibytes (`1024^3`) |
| `2000` | Default unit is MB |

## Common options

```text
-s, --max-size         Maximum size per output file (default: 2000MB)
-o, --output-dir       Output directory
-vc, --video-codec     Video codec (default: copy)
-ac, --audio-codec     Audio codec (default: copy)
-sc, --subtitle-codec  Subtitle codec (default: copy)
--safety-margin        Safety margin factor (default: 0.005)
--overlap              Overlap segments in seconds (--overlap 30)
--extra-args           Extra ffmpeg args
-i, --interactive      Guided interactive mode
-v, --verbose          Show detailed ffmpeg output
--strict-sync          Enable integrity-first boundaries (may use safe re-encode)
--keyframe-interval    Keyframe interval (seconds) during video transcode
```

Use `media-splitter -h` for the full help message.

## Output naming

Generated files follow this pattern:

```text
input.mp4
input_split_001.mp4
input_split_002.mp4
input_split_003.mp4
...
```

## Re-encode examples

```bash
# H.264 + AAC
media-splitter input.mkv -s 500MB -vc libx264 -ac aac

# H.265 + Opus
media-splitter input.mp4 -s 300MB -vc libx265 -ac libopus

# Extra ffmpeg settings
media-splitter input.mp4 -s 500MB -vc libx264 --extra-args -crf 23 -preset medium

# Force a denser keyframe cadence during transcode (helps boundary playback)
media-splitter input.mp4 -s 500MB --strict-sync --keyframe-interval 1.5
```

## How it works

1. Probe file info and streams with ffprobe
2. Estimate segment duration from bitrate and target size
3. Write segment and monitor ffmpeg progress in real time
4. Re-estimate ideal cut duration up to 100 times per segment from live size readings
5. Retry with adjusted duration until fill is near the maximum target (typically >=98%)
6. Apply a strict post-check hard cap to reject any oversize output (with conservative fallback rewrites for stubborn VBR cases)
7. Repeat until the full source is split

Boundary integrity mode is opt-in. When enabled and video codec is `copy`, the splitter applies safe re-encode defaults for boundary reliability (`libx264` video, `aac` audio when audio is also `copy`), reducing artifacts/freezes/desync at segment starts and ends.

To enable this mode, use `--strict-sync`.

When strict sync or any video transcode is active, you can tune keyframe cadence with `--keyframe-interval` (default in strict mode: `2.0s`).

In pure copy mode, split boundaries still depend on source keyframe layout. If you see brief frozen starts in specific outputs, enable `--strict-sync` for boundary-safe re-encode on those runs.

Full algorithm notes are in [docs/TECHNICAL.md](docs/TECHNICAL.md).

## Project layout

```text
media_splitter_by_size/
├── media_splitter_by_size/
│   ├── __init__.py
│   ├── cli.py
│   ├── console.py
│   ├── probe.py
│   └── splitter.py
├── docs/
│   └── TECHNICAL.md
├── main.py
├── pyproject.toml
├── README.md
└── LICENSE
```

## Contributing

1. Create a branch
2. Make your changes
3. Run a local split test
4. Open a pull request

## License

MIT. See [LICENSE](LICENSE).

## Author

Matheus Bach: [github.com/matheusbach](https://github.com/matheusbach)
