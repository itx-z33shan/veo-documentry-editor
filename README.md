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
clips/          input/            music/              output/
001.mp4         narration.aac     background.m4a     final_documentary.mp4
002.mp4         transcript.txt    (optional)          subtitles.srt
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

### Keep local installs and source media out of Git

Do **not** commit or push an FFmpeg installation, `site-packages`, `.venv`,
API keys, CapCut exports, Veo clips, narration, or music. FFmpeg is a local
system prerequisite and the media folders are intentionally ignored. The
optional `google-genai` package is only needed for Gemini clip matching; it is
not needed to master a finished video. Commit source code, configuration
profiles, and dependency instructions—not locally installed binaries.

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
# 2. Put your narration in input/narration.*  (.aac, .mp3, .m4a, .wav, ...)
# 3. (optional) Put the final script/transcript in input/transcript.txt
# 4. (optional) Put a separate music stem in music/background.*

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

# Finish one already-edited CapCut/Premiere/DaVinci export safely
python editor.py --master input/master.mp4 --master-audio-mode preserve
python editor.py --master input/master.mp4 --master-audio-mode replace
python editor.py --master input/master.mp4 --master-audio-mode rebuild
python editor.py --help
```

---

## Local finishing dashboard

Prefer a guided browser workflow instead of typing commands? Launch the
built-in local dashboard—no Flask, Node, cloud upload, or additional Python
package is required:

```bash
python web.py
```

Then open **http://127.0.0.1:8765** in your browser. It provides a five-step
wizard:

1. choose Finished CapCut Master, Raw Clips + Voice, or Clean Stem Rebuild;
2. drag/drop the master, AAC narration, transcript/SRT, and optional clips or
   music;
3. choose safe YouTube/Facebook finishing settings;
4. run a dry check, see live editor/FFmpeg logs, then start the render; and
5. preview/download the MP4, SRT, and JSON report.

### Automatic local captions when no script is available

If you do not upload a script, transcript, or time-coded SRT, keep **Generate
missing captions locally with Whisper** enabled in Step 03. The dashboard will
transcribe the clean narration when available, or the embedded audio inside
the master MP4 when no separate narration file is supplied. It writes a timed
SRT draft during the dry run so you can review it before final rendering.

Install the optional local transcription engine once:

```bash
pip install -r requirements-transcription.txt
```

The selected Whisper model downloads to your local model cache on its first
use. Audio stays local; no Gemini key or cloud transcription API is used.
Review names, dates, numbers, punctuation, and cue timing before publishing.

The dashboard is intentionally **local-only by default** and has no login.
Keep it on `127.0.0.1` for normal use. It runs only structured workflows and
safe configuration fields—never browser-supplied FFmpeg commands or API keys.
For a trusted LAN or an Arena preview you can explicitly bind another host:

```bash
python web.py --host 0.0.0.0 --port 8000
```

Do not expose that unauthenticated mode directly to the public internet.

---

## Finishing an existing CapCut master (recommended for your current case)

If you already have an 11-minute CapCut export with its 70+ visual edits and
ElevenLabs narration synced, **do not run it back through automatic shot
planning**. That would invent new cuts on top of an edit you have already
approved. Use the master-finishing workflow instead.

Place these untracked working files locally:

```text
input/master.mp4          # final CapCut visual export with its embedded mix
input/narration.aac       # clean ElevenLabs voice; used as a sync reference
input/transcript.txt      # final words actually spoken
```

Then run:

```bash
python editor.py --config profiles/master-preserve.json \
  --master input/master.mp4 --master-audio-mode preserve
```

This stream-copies the visual master when no final visual fade is requested,
normalizes/limits its embedded audio to the configured delivery target,
creates `output/final_master.mp4`, and writes `output/subtitles.srt` plus
`output/master_report.json`.

### Pick the audio mode from the real source topology

| Situation | Mode | What happens |
|---|---|---|
| CapCut export already contains the synced voice and the Veo clip music | `preserve` | Masters the existing combined mix. The separate ElevenLabs AAC is checked as a reference **and is not mixed again**, so it cannot create doubled voice/echo. |
| Existing video has no audio you want to keep | `replace` | Removes the master audio and uses only the clean narration. |
| You have a clean narration **and a separate licensed music stem** | `rebuild` | Removes the master audio, ducks the separate music under narration, then masters the final mix. |
| You have the original many Veo/CapCut clips, each with useful embedded music/ambience, plus clean ElevenLabs narration | regular clip workflow | Keep the clips in `clips/`, use `profiles/veo-embedded-bed.json`, and run `python editor.py`. |

Never choose `rebuild` using a CapCut export as the “music” input: it may
already contain the narration and would reintroduce the duplicate-voice
problem. If the original clips are available, the clip workflow is the better
way to reconstruct a mix from their embedded audio.

### Embedded-music Veo clips + separate ElevenLabs narration

For raw source clips that contain their own background music/ambience but no
voice, use:

```bash
# clips/001.mp4 ... clips/070.mp4      (Veo clips with their own audio)
# input/narration.aac                  (ElevenLabs voice)
# input/transcript.txt                 (final script)
python editor.py --config profiles/veo-embedded-bed.json
```

That profile retains the clip audio quietly (`clip_audio_volume: 0.12`), adds
the narration, produces an SRT sidecar only, targets −14 LUFS / −1.5 dBTP,
and creates a 1080p H.264/AAC web-ready master. It intentionally uses hard
cuts. With embedded clip audio, global visual crossfades can discard the clip
bed; use a separate music stem if you want crossfades with continuous music.

### Captions and delivery

A supplied time-coded `input/script.srt` is copied unchanged. A plain
`input/transcript.txt` or `input/script.txt` produces an SRT whose cue timing
is estimated from word count over the measured duration. It is useful as a
starting point, but **review the timings against the spoken narration** before
uploading.

For a 1920×1080 H.264/yuv420p CapCut export, the included profiles preserve
that compatible visual format (or re-encode it when a final fade is enabled),
use 48 kHz AAC at 256 kbps, `+faststart`, −14 LUFS target, and a −1.5 dBTP
ceiling. That is a sensible single landscape master for both YouTube and
Facebook. Upload the matching `subtitles.srt` separately on each platform. Do
not assume an SRT is correct until names, numbers, pauses, and final cue timing
have been checked.

### Transitions and final QC

Individual CapCut transitions are baked into `master.mp4`; a finishing pass
can add only an optional beginning/end fade, not safely reinterpret 70+
individual transitions. To change a specific transition, use the CapCut
project or original clips.

For documentary pacing, keep most cuts hard. Reserve 0.2–0.35 second
dissolves for a genuine time/place/mood change and a short dip-to-black for a
chapter break. Before publishing, watch the complete 11-minute file once and
check narration sync at the **end**, abrupt music changes, clicks at clip
boundaries, black/flash frames, subtitle spelling/timing, and speech clarity
on both phone/laptop speakers and headphones.

---

## Project structure

```
veo_documentary_editor/
├── editor.py            # CLI orchestration
├── web.py               # local browser dashboard launcher
├── web/static/          # guided dashboard HTML/CSS/JS (no Node required)
├── config.json          # configuration (everything is tunable)
├── requirements.txt     # (no Python deps required)
├── requirements-transcription.txt # optional local faster-whisper captions
├── README.md
├── profiles/            # reusable finishing / audio-topology configs
├── input/
│   ├── narration.aac        # any supported audio extension is accepted
│   └── script.txt            # optional
├── clips/
│   ├── 001.mp4 ...
│   └── metadata.json         # optional semantic tags
├── music/
│   └── background.m4a        # optional; any supported audio extension
├── output/                   # final deliverables
├── temp/                     # intermediate files (auto-managed, resumable)
├── src/
│   ├── errors.py             # human-readable error types
│   ├── config.py             # defaults + validation
│   ├── media.py              # ffprobe/ffmpeg discovery + probing
│   ├── inputs.py             # flexible .aac/.m4a/.mp3/.wav input discovery
│   ├── mastering.py          # safe finishing of one existing video master
│   ├── dashboard.py          # local upload/inspection/render web API
│   ├── transcription.py      # optional local Whisper transcription + timed SRT
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
  "sample_rate": 48000, "aac_bitrate": 192, "faststart": true,

  "master_audio_mode": "preserve", // "preserve" | "replace" | "rebuild"
  "master_fade_seconds": 0.0
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

* **Narration** is normalized before mixing so it remains clear and provides a
  stable ducking key.
* **Music** (optional) loops to match narration, fades in/out, and is heavily
  attenuated (default `0.08`). When `ducking_enabled`, narration
  sidechain-compresses the music so narration always stays dominant and music
  recovers during pauses.
* The **final combined mix** is normalized to the configured target (default
  ~−14 LUFS integrated / −1.5 dBTP), then limited to prevent clipping.

---

## Subtitles

* A supplied time-coded `.srt` keeps its original timing and is copied to
  `output/subtitles.srt`.
* A plain script/transcript creates proportionally timed `.srt` and `.ass`
  files over the measured narration duration.
* The local dashboard can optionally generate a word-timed SRT draft with
  local Whisper when no script exists; review it before publishing.
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

For an already edited export, use `--master` rather than putting the single
MP4 into `clips/`. The master workflow preserves the visual timeline and makes
the crucial audio choice explicit: preserve its baked mix, replace it with a
clean narration, or rebuild it only when separate music is available.

Use the normal clip workflow only when you genuinely want to assemble or
re-cut source footage. It can then add/replace narration, add a separate music
stem, normalize audio, generate subtitles, change resolution/aspect
(`fit: crop/pad`), add intro/outro, and produce a YouTube/Facebook-ready MP4.

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
