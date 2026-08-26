# Veo Documentary Editor

A production-ready, **local-first** automated documentary video editor.

It takes pre-generated AI video clips (e.g. from **Google Veo**), a complete
**ElevenLabs** narration file, optional background music, and an optional
documentary script, and automatically assembles a polished, narrated
documentary video — the kind of editing normally done by hand in
CapCut/Premiere/DaVinci.

> ⚠️ **You already generated the clips and the narration.** This tool does
> **not** regenerate video or narration. It only *edits and assembles* what
> you provide.

---

## What it does

```
clips/          input/          music/            output/
001.mp4         narration.mp3   background.mp3    final_documentary.mp4
002.mp4         script.txt      (optional)        subtitles.srt
003.mp4         (optional)                         timeline.json
...                                                 timeline.txt
                                                     edit_report.json
```

1. **Scans** your clips with FFprobe and builds a natural-sorted media manifest.
2. **Analyzes** the narration duration/format.
3. **Parses** the script into logical scenes (with a deterministic, LLM-free
   fallback when no scene markers exist).
4. **Matches** clips to scenes — semantically (via `clips/metadata.json`) or
   sequentially.
5. **Builds** a narration-synced timeline with natural shot pacing.
6. **Renders** with FFmpeg: normalizes footage, mixes narration + music (with
   ducking and loudness normalization), optionally burns subtitles, adds
   intro/outro, and writes a YouTube-ready 1080p MP4.
7. **Reports** everything to `output/edit_report.json`.

The final video matches the narration duration within ~100 ms.

---

## Requirements

* Python 3.10+
* **FFmpeg** on your `PATH` (the tool prefers **FFprobe** for probing and
  transparently falls back to ffmpeg-based probing when ffprobe is absent).
  Paths are configurable via `ffmpeg_bin` / `ffprobe_bin` in `config.json`.
* **No Python dependencies** — the pipeline is pure standard library.

Install FFmpeg:

```bash
# Debian / Ubuntu
sudo apt-get install ffmpeg

# macOS (Homebrew)
brew install ffmpeg

# Windows: download static builds from https://ffmpeg.org/download.html
```

> Some heavily-trimmed "static" ffmpeg builds omit optional filters. The tool
> detects this and degrades gracefully (e.g. burned-in subtitles and text
> intros are skipped with a clear warning). A standard distro/brew build has
> everything enabled.

---

## Quick start

```bash
# 1. Put your Veo clips in clips/        (001.mp4, 002.mp4, ...)
# 2. Put your narration in input/narration.mp3
# 3. (optional) Put the script in input/script.txt
# 4. (optional) Put music in music/background.mp3

# 5. Run the editor
python editor.py

# Inspect the edit first without rendering
python editor.py --dry-run
```

Outputs land in `output/`.

---

## Command-line interface

```bash
python editor.py                  # full render
python editor.py --preview        # fast low-res preview (first ~45 s)
python editor.py --dry-run        # build timeline only, no render
python editor.py --resume         # reuse completed intermediate steps
python editor.py --force          # ignore cache, re-render everything
python editor.py --clean          # delete temporary files only
python editor.py --config my.json # use an alternate config file
python editor.py --help
```

---

## Project structure

```
veo_documentary_editor/
├── editor.py            # CLI orchestration
├── config.json          # configuration (everything is tunable)
├── requirements.txt     # (no Python deps required)
├── README.md
├── input/
│   ├── narration.mp3
│   └── script.txt            # optional
├── clips/
│   ├── 001.mp4 ...
│   └── metadata.json         # optional semantic tags
├── music/
│   └── background.mp3        # optional
├── output/                   # final deliverables
├── temp/                     # intermediate files (auto-managed, resumable)
├── src/
│   ├── errors.py             # human-readable error types
│   ├── config.py             # defaults + validation
│   ├── media.py              # ffprobe/ffmpeg discovery + probing
│   ├── scanner.py            # clips scan -> media manifest
│   ├── script.py             # scene segmentation (deterministic)
│   ├── matcher.py            # clip→scene assignment (INTELLIGENCE)
│   ├── timeline.py           # narration timing + shot planning
│   ├── subtitles.py          # SRT / ASS writers
│   ├── audio.py              # mixing graph + ducking + loudnorm
│   ├── renderer.py           # FFmpeg execution (EXECUTION)
│   ├── overrides.py          # manual timeline overrides
│   ├── reporter.py           # timeline.txt + edit_report.json
│   ├── ai.py                 # optional Gemini AI layer (clip desc, decisions)
│   └── vectorstore.py        # tiny local vector DB (cosine retrieval)
└── tests/
```

---

## Optional Gemini AI intelligence layer

The editor runs entirely locally and deterministically by default. You can
optionally enable an AI intelligence layer backed by Google's **Gemini free
tier** that improves clip→scene matching. It follows this pipeline:

```
Veo clips ──Gemini 3.7 Flash──▶ clip descriptions
descriptions ──Gemini Embedding 2──▶ embeddings ──▶ local vector DB
narration ──▶ scene requirements ──semantic retrieval──▶ top-5 candidates
candidates ──Gemini 3.1 Pro──▶ final ordered decisions
decisions ──[validated]──▶ timeline ──▶ FFmpeg
```

### Enable it

1. Install the optional SDK:
   ```bash
   pip install google-genai
   ```
2. Set a free-tier API key:
   ```bash
   export GEMINI_API_KEY=your_key   # from https://aistudio.google.com/apikey
   ```
3. In `config.json`:
   ```jsonc
   {
     "ai_provider": "gemini",
     "ai_api_key_env": "GEMINI_API_KEY",
     "ai_vision_model": "models/gemini-3.7-flash",      // clip descriptions
     "ai_embedding_model": "models/gemini-embedding-2",  // embeddings
     "ai_decision_model": "models/gemini-3.1-pro-preview", // final picks
     "ai_top_k": 5,
     "ai_max_video_bytes": 31457280,
     "ai_vector_db_path": "output/clip_vectors.json"
   }
   ```

How it works:

* **Clip descriptions** — clips you already described in
  `clips/metadata.json` are reused (zero API cost); the rest are sent to the
  Gemini vision model for a factual 1–2 sentence description + tags.
* **Local vector DB** — descriptions are embedded and cached in
  `output/clip_vectors.json` (a tiny zero-dependency flat-file store with
  cosine retrieval). Stale entries are invalidated automatically.
* **Semantic retrieval** — each scene's narration text + derived keywords are
  embedded and the top-`k` clips are recalled per scene.
* **Final decisions** — the Gemini decision model orders those candidates for
  each scene, and the results are **validated** (only real clip filenames,
  deduped, ordered) before they become part of the timeline.
* **Safety** — the AI only ever produces *structured editing decisions*; it
  never touches raw FFmpeg commands, and it never overrides a manual
  `timeline_override.json`. If the SDK, key, or any call fails, the editor
  warns and falls back to deterministic matching. The pipeline works fully
  with `ai_provider: null`.

Available Gemini free-tier model names (defaults are picked from these):

* `models/gemini-3.7-flash` — vision / clip descriptions
* `models/gemini-embedding-2` — embeddings
* `models/gemini-3.1-pro-preview`, `models/gemini-3.5-flash`, etc. — decisions

---

## Architecture: INTELLIGENCE vs EXECUTION

A core design principle keeps three layers strictly separate:

1. **Intelligence** — `matcher.py` and `timeline.py` decide *what should
   appear where*. Fully deterministic and local; no LLM required.
2. **Validation** — `config.py` and the timeline builders reject invalid
   plans before any media is touched.
3. **Execution** — `renderer.py` only translates the validated timeline into
   FFmpeg commands. It never makes creative decisions.

An LLM (OpenAI / Gemini / OpenRouter / a local model) can be plugged in later
as an *optional* intelligence layer that only emits structured editing
decisions — never raw FFmpeg commands, which are always re-validated.

---

## Clip matching

Two modes (configured via `clip_strategy`):

* **MODE A — Sequential**: uses clips in natural filename order
  (`1, 2, 3, 10`, not `1, 10, 2, 3`).
* **MODE B — Semantic**: scores each scene's visual requirements against
  optional `clips/metadata.json` descriptions/tags. Falls back to
  `fallback_strategy` (sequential) automatically when no metadata exists.

### `clips/metadata.json` (optional)

```json
{
  "001.mp4": {
    "description": "Ancient Roman soldiers marching through a city",
    "tags": ["Rome", "soldiers", "ancient", "military"]
  },
  "002.mp4": {
    "description": "Aerial view of a desert battlefield",
    "tags": ["desert", "battlefield", "military"]
  }
}
```

---

## Configuration

Everything lives in `config.json`. Highlights:

```jsonc
{
  "width": 1920, "height": 1080, "fps": 30,
  "video_codec": "libx264", "crf": 18, "preset": "medium",
  "pacing": "normal",                  // "slow" | "normal" | "fast"
  "min_clip_seconds": null,            // null => derived from pacing
  "preferred_clip_seconds": null,
  "max_clip_seconds": null,
  "transition": "cut",                 // "cut" | "crossfade"
  "crossfade_seconds": 0.3,
  "fit": "pad",                        // "pad" (letterbox) | "crop"
  "clip_strategy": "semantic",         // "semantic" | "sequential"
  "fallback_strategy": "sequential",
  "loop_footage": true,                // loop clips to cover narration
  "music_enabled": true, "music_volume": 0.08,
  "ducking_enabled": true,
  "subtitle_enabled": true, "subtitle_burn_in": false,
  "intro_enabled": false, "outro_enabled": false,
  "loudness_target_lufs": -14.0, "loudness_target_tp": -1.5,
  "sample_rate": 48000, "aac_bitrate": 192, "faststart": true
}
```

Pacing presets: **slow** 5–10 s shots, **normal** 3–8 s, **fast** 2–5 s.
Shots vary naturally (they are never all the same length).

---

## Manual overrides

Place `timeline_override.json` next to `editor.py` (or in `output/`) to force
specific assignments. Manual assignments always win over automatic decisions.

```json
{
  "scene_3": {
    "clip": "037.mp4",
    "start": 0,
    "duration": 6.5
  }
}
```

---

## Script / scene markers

`input/script.txt` may use explicit markers:

```
[SCENE 1]
The Roman Empire...

[SCENE 2]
But centuries later...
```

If there are no markers, a deterministic fallback segments the text on
paragraph/sentence boundaries. **No LLM is required** for any of this.

---

## Audio

* **Narration** is normalized to ~ −14 LUFS integrated, ~ −1.5 dBTP (configurable).
* **Music** (optional) loops to match narration, fades in/out, and is heavily
  attenuated (default `0.08`). When `ducking_enabled`, narration
  sidechain-compresses the music so narration always stays dominant and music
  recovers during pauses.
* Final mix is limited to prevent clipping.

---

## Subtitles

* `.srt` and `.ass` files are always written to `output/` when a script exists.
* Timing is derived from the script's words proportionally distributed over
  the measured narration duration (no Whisper/LLM needed).
* Optional burned-in subtitles via `subtitle_burn_in: true`.
* ~1–2 lines, ~42 chars/line by default.

---

## Resumability

Intermediates are written to `temp/step1/` with content-hash markers. If a
render of 100 clips stops at shot 70, shots 1–69 are **not** reprocessed —
`--resume` (default) reuses them. Run `--force` to ignore the cache, or
`--clean` to delete `temp/`.

Source files (clips, narration, music, script, metadata) are **never**
modified or deleted.

---

## Error handling

Failures (missing FFmpeg/FFprobe, missing narration, no clips, corrupt clips,
unsupported codecs, invalid durations, missing music, insufficient disk
space, invalid config) produce clear, human-readable messages — never silent
failures.

---

## Post-processing your own MP4

Because the editor treats every input clip as just footage, you can also use
it to post-process an exported MP4:

* drop your video clip(s) into `clips/`,
* supply narration and/or music and/or a script,
* trim shots by adjusting pacing / min/max shot durations or via overrides,
* then use it to: **add/replace narration**, **add music**, **normalize
  audio**, **add subtitles**, **change resolution/aspect** (`fit: crop/pad`),
  **trim/cut**, **add intro/outro**, **add fades/transitions**, **compress /
  re-encode**, and **produce a YouTube-ready 1080p MP4**.

For a quick **Shorts / vertical** cut, set `width: 1080, height: 1920` and
`fit: crop` in a `config-shorts.json` and run `--config config-shorts.json`.
(Full automatic Shorts generation is on the roadmap, not in v1.)

---

## Roadmap (not implemented in v1)

Automatic B-roll selection, AI scene detection, AI clip descriptions,
Whisper transcription, automatic silence/beat detection, sound effects,
cinematic color grading, Ken Burns effect, face/object detection, automatic
Shorts generation, thumbnails, YouTube upload & analytics, A/B title testing.

The architecture (separated intelligence/validation/execution layers) is
designed so these can be added without destabilizing the core pipeline.

---

## ⚠️ Important: factual / legal responsibility

This tool **assembles media only**. It does **not** verify that AI-generated
visuals are historically or factually accurate, and it must never be used to
claim otherwise. As the user you remain solely responsible for:

* **Factual and historical accuracy**
* **Copyright** and licensing of footage and music
* **Footage rights**
* **YouTube policy compliance** and monetization eligibility

This is an editing engine, not a source of truth about your subject matter.

---

## Tests

```bash
python -m unittest discover -s tests
```

Tests cover scene segmentation, natural sorting, clip matching (MODE A/B),
timeline construction, subtitle formatting, config validation, and manual
overrides. Media rendering is exercised by running `python editor.py` on real
footage (FFmpeg required).
