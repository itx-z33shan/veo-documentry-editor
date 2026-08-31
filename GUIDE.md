# Guide: Combine 70 Veo 3 Clips + ElevenLabs Narration into One Large Video

This guide walks you through combining **70 clips from Google Veo 3** and your
**ElevenLabs narration** into a single, finished, YouTube-ready documentary
video using the Veo Documentary Editor in this repository.

The tool does the boring editing work for you:

* orders and normalizes your 70 clips (same resolution / fps / aspect),
* syncs the clips to your narration so the video length **matches the
  narration length** (within ~100 ms),
* mixes narration + optional music (with auto-ducking) and normalizes the
  audio to −14 LUFS / −1.5 dBTP,
* generates `.srt` subtitles from your script,
* renders one `final_documentary.mp4` (1080p H.264/AAC, YouTube-ready).

---

## Two paths — pick the one that matches your situation

| You already assembled the 70 clips in CapCut / Premiere / DaVinci and the narration is already synced there | You are starting from the raw 70 Veo clips and want the tool to assemble them |
|---|---|
| Use **Path A — Master finishing** (Step A1–A5). It masters your existing edit without re-cutting it. | Use **Path B — Clip assembly** (Steps 1–8). The tool builds the edit for you. |

> ⚠️ If you already spent hours cutting the clips and syncing the voice in
> CapCut, **do not** run the clips back through automatic shot planning — that
> would invent new cuts on top of an edit you already approved. Use Path A.

---

# Path A — Finish an existing CapCut/Premiere export (recommended if already edited)

## A1. Place your working files (do not commit them to Git)

```text
input/master.mp4          # final visual export from CapCut/Premiere, with its audio
input/narration.mp3       # clean ElevenLabs voice-over (used as sync reference)
input/transcript.txt      # the final words actually spoken (for subtitles)
```

## A2. Choose the audio mode from your real situation

| Situation | Mode | Command |
|---|---|---|
| CapCut export already contains the synced voice + Veo music | `preserve` | `--master-audio-mode preserve` |
| The video has no audio you want to keep | `replace` | `--master-audio-mode replace` |
| You have clean narration **and** a separate licensed music stem | `rebuild` | `--master-audio-mode rebuild` + put music in `music/background.mp3` |

## A3. Run the finish

```bash
python editor.py --config profiles/master-preserve.json \
  --master input/master.mp4 --master-audio-mode preserve
```

## A4. Get the results

```text
output/final_master.mp4     # mastered video (normalized loudness, faststart)
output/subtitles.srt        # captions for YouTube
output/master_report.json   # what was done
```

## A5. Review before uploading

Watch `final_master.mp4` end to end, check `subtitles.srt` cue timing
(names, dates, numbers), then upload.

> If the CapCut export already contains the narration, never use `rebuild`
> with that export as the "music" input — you would get doubled voice/echo.
> `preserve` is the safe default.

---

# Path B — Assemble the raw 70 clips into one video

## Step 0 — Prerequisites

* **Python 3.10+**
* **FFmpeg** (with FFprobe) on your PATH:

  ```bash
  # Debian / Ubuntu
  sudo apt-get install ffmpeg

  # macOS
  brew install ffmpeg

  # Windows: download from https://ffmpeg.org/download.html and add ffmpeg.exe
  #          + ffprobe.exe to PATH
  ```

* No Python packages are required — the pipeline is pure standard library.

> ℹ️ The shipped `config.json` contains `"ffmpeg_bin": "tools/ffmpeg.exe"`.
> That is a Windows-style path; if the file doesn't exist on your machine the
> tool automatically falls back to the `ffmpeg` on your PATH. To use a
> specific install instead, set `ffmpeg_bin` / `ffprobe_bin` in `config.json`
> (or in your profile file).

## Step 1 — Put the 70 clips in `clips/`

> 💡 **In the browser dashboard you never move or rename files by hand.**
> In Step 02 of the wizard, **Choose folder** (or dropping a folder on the
> clip card) queues every MP4/MOV/MKV/WebM/M4V inside it — subfolders
> included — in natural sort order, ignoring poster images, `.DS_Store`,
> `._*` AppleDouble files and empty stubs. Two identically named clips in
> different subfolders are stored apart (`batch-b_014.mp4`) instead of one
> silently replacing the other, an optional top-level `clips/metadata.json`
> travels with them, and re-picking the same folder only uploads what is
> missing when **Replace existing source clips** is off. See
> *Queue a whole clip folder* in the README for the exact rules.

Rename the Veo 3 downloads so natural sort order = your story order:

```text
clips/001.mp4
clips/002.mp4
clips/003.mp4
...
clips/070.mp4
```

Rules:

* Zero-padded numbers (`001` … `070`) guarantee order — the tool sorts
  naturally, so `1, 2, 3, 10` stays correct even without padding.
* Supported formats: `.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v`.
* Mixed resolutions are fine — the renderer normalizes everything to the
  configured output (`1920×1080` by default). `fit: "pad"` letterboxes,
  `fit: "crop"` fills the frame.
* Veo 3 clips are typically ~8 s each → 70 clips ≈ 9.3 min of footage. If
  your narration is longer, `loop_footage: true` (default) loops clips to
  cover it; if it's shorter, only as many clips as needed are used.
* Corrupt/unreadable clips are skipped and reported as warnings — never
  deleted.

## Step 2 — Put the narration in `input/`

The tool expects **one narration file** containing the complete voice-over:

```text
input/narration.mp3       # or .aac, .m4a, .wav, .flac, .ogg, .opus
```

Accepted stems: `narration.*`, `voice.*`, `elevenlabs.*`.

### Case 2a — You already have one full ElevenLabs file

Rename it to `input/narration.mp3` (any audio extension works). Done.

### Case 2b — You have 70 separate ElevenLabs files (one per clip)

Concatenate them **in story order** (the same order as your clips) before
running the editor.

**Linux / macOS:**

```bash
# list files in the exact order of your story
ls narration/*.mp3 | sort > concat_list.txt   # or build the list by hand
# convert "narration/001.mp3" lines to ffmpeg list format
while IFS= read -r f; do printf "file '%s'\n" "$(realpath "$f")"; done \
  < concat_list.txt > ffmpeg_list.txt

mkdir -p input
ffmpeg -f concat -safe 0 -i ffmpeg_list.txt -c copy input/narration.mp3
```

**Windows (PowerShell):**

```powershell
Get-ChildItem narration -Filter *.mp3 | Sort-Object Name |
  ForEach-Object { "file '$($_.FullName)'" } | Set-Content -Encoding ascii ffmpeg_list.txt
ffmpeg -f concat -safe 0 -i ffmpeg_list.txt -c copy input\narration.mp3
```

* If the joined file has gaps or glitches between segments, re-encode instead
  of stream-copying:

  ```bash
  ffmpeg -f concat -safe 0 -i ffmpeg_list.txt -c:a libmp3lame -q:a 2 input/narration.mp3
  ```

* Prefer generating the narration as **one long ElevenLabs project** in the
  first place — it avoids join artifacts entirely.

## Step 3 — (Recommended) Add the script / transcript

The script is used to (a) split the film into scenes, (b) match the right
clips to each scene, and (c) generate subtitles. Put it in:

```text
input/transcript.txt      # or input/script.txt
```

**Format A — explicit scene markers** (best for 70 clips: one marker per clip):

```text
[SCENE 1]
The Roman Empire seemed unstoppable...

[SCENE 2]
But centuries later, the frontier began to crumble...
```

The marker number maps to the scene; clips are matched per scene (see
Step 6 for smarter matching). Without markers, the tool splits the text on
paragraph/sentence boundaries (~20 s of narration per scene) — deterministic
and LLM-free.

**Format B — time-coded SRT** (best for perfect sync — ElevenLabs can export
word-level timestamps as an SRT): place it at `input/script.srt`. Its original
timing is preserved exactly and copied to `output/subtitles.srt`. The SRT cue
times also drive the scene boundaries: every scene — and therefore every clip
change in a 70-scene edit — starts and ends **exactly on a real narration
beat** instead of an estimate, and the scene text is derived from the SRT for
clip matching. Combining a transcript with `[SCENE …]` markers **and** the SRT
is the best setup: scenes come from the transcript, real timing from the SRT.

> No script at all → the film becomes one continuous scene. It still works,
> but you lose scene-based clip matching and subtitles. For a 70-clip film
> a transcript with `[SCENE …]` markers is strongly recommended.

## Step 4 — (Optional) Background music

Put a licensed music stem at:

```text
music/background.mp3      # or .m4a, .wav, ...
```

The tool loops it to fit the narration, fades it in/out, keeps it at a low
level (`music_volume: 0.08`), and sidechain-ducks it under the voice so the
narration always stays dominant.

## Step 5 — Pick the right config profile

| Your clips | What to run |
|---|---|
| Veo clips **have their own audio** (Veo 3 generates music/ambience) and you have clean narration | `python editor.py --config profiles/veo-embedded-bed.json` — keeps clip audio quietly (0.12) and ducks it under the narration; no external music needed |
| Clips are **silent**, you have a separate music track | `python editor.py` (default `config.json`) with `music/background.mp3` present |
| Clips are **silent**, no music | Create `config-no-music.json` with just `{"music_enabled": false}` and run `python editor.py --config config-no-music.json` |

Profile files only override what they set; everything else falls back to
sane defaults. Key defaults you may want to tweak:

```jsonc
{
  "width": 1920, "height": 1080, "fps": 30,   // output format
  "fit": "pad",                               // "pad" = letterbox, "crop" = fill
  "pacing": "normal",                         // "slow" 5–10 s | "normal" 3–8 s | "fast" 2–5 s per shot
  "transition": "cut",                        // "cut" or "crossfade" (0.3 s)
  "loop_footage": true,                       // loop short clips to cover narration
  "subtitle_burn_in": false,                  // true = bake captions into the video
  "loudness_target_lufs": -14.0,              // YouTube target
  "crf": 18, "preset": "medium"               // quality vs. render speed
}
```

## Step 6 — Dry run first (no rendering)

```bash
python editor.py --dry-run
```

This scans your clips, measures the narration, builds the timeline, and
writes:

```text
output/timeline.json      # machine-readable shot plan
output/timeline.txt       # human-readable shot plan
output/edit_report.json   # summary, warnings, per-scene clip choices
```

Check `output/timeline.txt` — verify the clip order, shot durations, and that
the total duration matches your narration. Fix clip names/order or
`timeline_override.json` (see Step 9) before spending time on a full render.

## Step 7 — Render the final video

```bash
python editor.py
```

* Output: **`output/final_documentary.mp4`** — 1080p H.264/AAC, −14 LUFS,
  `faststart` (streams immediately on YouTube).
* Sidecars: `output/subtitles.srt` (+ `.ass`), `output/timeline.json`,
  `output/edit_report.json`.
* Duration matches the narration within ~100 ms.

Useful variants:

```bash
python editor.py --preview      # fast low-res render of the first ~45 s — sanity check before the full render
python editor.py --resume       # default: reuses completed intermediate shots
python editor.py --force        # ignore cache, re-render everything
python editor.py --clean        # delete temp/ intermediates only
```

Render time scales with clip count, length, resolution, and preset. 70 clips
at 1080p `medium` can take a while — the pipeline is resumable, so an
interrupted run continues from the last finished shot instead of restarting.

## Step 8 — Polish and publish

* **Burn subtitles in** (if you want them baked): set `"subtitle_burn_in": true`
  and re-render.
* **Vertical / Shorts cut**: create `config-shorts.json` with
  `{"width": 1080, "height": 1920, "fit": "crop"}` and run
  `python editor.py --config config-shorts.json`.
* **Upload**: check `edit_report.json` for warnings, spot-check the first and
  last 30 seconds, then upload the MP4 + `subtitles.srt` to YouTube.

---

## Step 9 — Advanced (optional)

### Smarter clip matching with `clips/metadata.json`

Default matching is sequential (clip N → scene N). To match *semantically*,
describe each clip:

```json
{
  "001.mp4": { "description": "Ancient Roman soldiers marching through a city", "tags": ["Rome", "soldiers"] },
  "002.mp4": { "description": "Aerial view of a desert battlefield", "tags": ["desert", "battlefield"] }
}
```

With `"clip_strategy": "semantic"`, each scene's narration text is scored
against these descriptions (deterministic, local, no API needed). Without
metadata it automatically falls back to sequential.

### Optional Gemini free-tier matching (70+ clips)

With `ai_provider: "gemini"` in config and `GEMINI_API_KEY` exported in your
shell, the tool can describe unlabelled clips and choose the best clip per
scene. The first pass uploads eligible clips once and caches descriptions in
`output/ai_clip_descriptions.json` — a quota interruption can be resumed
later without re-sending completed clips. If the key, SDK, or quota fails,
it warns and falls back to deterministic matching. Start with a dry run.
Note: free-tier quotas vary; don't expect all 70 clips to be accepted in one
session.

### Manual override of any shot

Place `timeline_override.json` next to `editor.py` to force specific
assignments (manual always wins):

```json
{
  "scene_3": { "clip": "037.mp4", "start": 0, "duration": 6.5 }
}
```

### Browser dashboard instead of the CLI

```bash
python web.py                          # http://127.0.0.1:8765 (local only)
# for a sandbox/remote preview:
python web.py --host 0.0.0.0 --port 8000
```

Five-step wizard: choose workflow → drop files → settings → dry check +
live logs → render + download. It can also generate missing captions locally
with Whisper (`pip install -r requirements-transcription.txt`) when you have
no script.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Required tool 'ffmpeg' was not found` | Install FFmpeg (Step 0) or set `ffmpeg_bin` in `config.json`. |
| `Narration file not found` | Put one complete narration file at `input/narration.mp3` (Step 2). |
| `No clips found` | Put the 70 videos in `clips/` (Step 1); supported: `.mp4 .mov .mkv .webm .m4v`. |
| Timeline is shorter/longer than expected | Check `output/timeline.txt`; the film length = narration length. Use `loop_footage` or adjust `pacing` (Step 5). |
| Clips in the wrong order | Rename with zero-padded numbers (`001…070`) matching your story order. |
| Subtitles skipped / not burned in | A stripped-down FFmpeg build may lack filter support — install a full build (Step 0). |
| Render stopped halfway | Just re-run — `--resume` (default) continues from the last finished shot. |
| Duplicated/echoed voice | You ran `rebuild` with a file that already contains narration. Use `preserve` for a CapCut export, or `rebuild` only with a clean music stem (Path A). |
| Gemini warnings / slow first pass | Expected on free tier. Cache survives interruptions; or disable AI (`ai_provider: null`) and use `metadata.json` / sequential matching. |
| Not enough disk space | Intermediates live in `temp/`; free space before the render, then `python editor.py --clean` afterwards. |

---

## CLI cheat sheet

```bash
python editor.py                          # full render of the clip workflow
python editor.py --dry-run                # plan only, no render
python editor.py --preview                # fast low-res first ~45 s
python editor.py --config my.json         # alternate config / profile
python editor.py --resume / --force       # reuse or ignore cached intermediates
python editor.py --clean                  # delete temp/
python editor.py --master input/master.mp4 --master-audio-mode preserve|replace|rebuild
python web.py                             # browser dashboard at 127.0.0.1:8765
```

---

## Final checklist

- [ ] Python 3.10+ and FFmpeg (with ffprobe) installed
- [ ] `clips/001.mp4 … clips/070.mp4` in story order
- [ ] `input/narration.mp3` = the complete voice-over (concatenated if needed)
- [ ] `input/transcript.txt` with `[SCENE 1] … [SCENE 70]` markers (recommended)
- [ ] `music/background.mp3` (optional, unless using `veo-embedded-bed.json`)
- [ ] Right config/profile chosen (Step 5)
- [ ] `python editor.py --dry-run` — timeline looks right
- [ ] `python editor.py --preview` — first 45 s look and sound right
- [ ] `python editor.py` — full render
- [ ] Review `output/final_documentary.mp4` + `output/subtitles.srt`
- [ ] Upload MP4 + SRT to YouTube
