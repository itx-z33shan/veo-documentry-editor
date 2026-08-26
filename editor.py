#!/usr/bin/env python3
"""Veo Documentary Editor — local-first automated documentary video editor.

Turns pre-generated Veo clips + an ElevenLabs narration into a polished,
narrated documentary video using only Python + FFmpeg + FFprobe.

Usage:
    python editor.py                 # full render
    python editor.py --preview       # fast low-res preview (first N seconds)
    python editor.py --dry-run       # build timeline, do not render
    python editor.py --resume        # reuse cached intermediate steps
    python editor.py --force         # re-render everything from scratch
    python editor.py --clean         # delete temporary files only
    python editor.py --config x.json # alternate config file
    python editor.py --help

Workflow & structure are documented in README.md.
"""

import argparse
import os
import shutil
import sys

from src import audio as audio_mod
from src.config import load_config, resolved_paths
from src.errors import EditorError, MediaNotFoundError, NoClipsError, \
    ConfigurationError
from src.media import Probe, resolve_binaries, ffmpeg_version
from src.scanner import scan_clips, load_clip_metadata
from src.script import segment_scenes, find_script
from src.matcher import assign_clips_to_scenes
from src.timeline import build_timeline
from src.subtitles import write_srt, write_ass, has_text
from src.overrides import load_overrides, apply_overrides
from src.reporter import (write_timeline_txt, write_timeline_json,
                          write_report, build_report)
from src.renderer import Renderer

SCRIPT_PATH = os.path.abspath(__file__)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="editor.py",
        description="Veo Documentary Editor — build narrated documentaries "
                    "from Veo clips and ElevenLabs narration with FFmpeg.")
    p.add_argument("--preview", action="store_true",
                   help="Render a fast low-resolution preview of the first "
                        "N seconds instead of the full film.")
    p.add_argument("--dry-run", action="store_true",
                   help="Scan, calculate narration duration and build the "
                        "timeline, but do not render.")
    p.add_argument("--resume", action="store_true",
                   help="Reuse completed intermediate steps where inputs are "
                        "unchanged (default behaviour).")
    p.add_argument("--force", action="store_true",
                   help="Ignore cached intermediates and re-render everything.")
    p.add_argument("--clean", action="store_true",
                   help="Delete the temp/ directory and exit.")
    p.add_argument("--config", default=None, help="Path to config.json.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # --clean exits before touching anything else.
    if args.clean:
        cfg = load_config(args.config)
        paths = resolved_paths(cfg)
        if os.path.isdir(paths["temp_dir"]):
            shutil.rmtree(paths["temp_dir"])
            print("Removed temporary directory: %s" % paths["temp_dir"])
        else:
            print("No temp directory to clean.")
        return 0

    cfg = load_config(args.config)
    if args.resume:
        cfg["resume"] = True
    if args.force:
        cfg["force"] = True
    paths = resolved_paths(cfg)

    print("Veo Documentary Editor")
    print("=" * 46)
    try:
        return _run(cfg, paths, preview=args.preview, dry_run=args.dry_run)
    except EditorError as exc:
        print("\nERROR: %s" % exc.message, file=sys.stderr)
        if exc.hint:
            print("  Hint: %s" % exc.hint, file=sys.stderr)
        return 1


def _run(cfg, paths, preview=False, dry_run=False):
    # --- Binaries --------------------------------------------------------
    ffmpeg, ffprobe = resolve_binaries(cfg)
    prober = Probe(ffmpeg, ffprobe)
    try:
        ver = ffmpeg_version(ffmpeg)
        print("FFmpeg: %s" % ver)
    except EditorError as exc:
        print("WARNING: %s" % exc.message)
    if ffprobe:
        print("FFprobe: %s" % ffprobe)
    else:
        print("FFprobe: not found — using ffmpeg-based probing.")

    # --- Directories -----------------------------------------------------
    for key in ("clips_dir", "input_dir", "music_dir", "output_dir",
                "temp_dir"):
        os.makedirs(paths[key], exist_ok=True)

    # --- Scan clips ------------------------------------------------------
    print("\n[1/6] Scanning clips...")
    metadata = load_clip_metadata(paths["clips_dir"])
    clips, scan_warnings = scan_clips(paths["clips_dir"], prober, metadata)
    for w in scan_warnings:
        print("  WARNING: %s" % w)
    print("  Found %d usable clips." % len(clips))
    for c in clips:
        print("    %-10s %6.2fs  %dx%d  %.1ffps  %s%s" % (
            c["file"], c["duration"], c["width"], c["height"], c["fps"],
            c["codec"], "  [audio]" if c["has_audio"] else ""))

    # --- Narration -------------------------------------------------------
    print("\n[2/6] Analyzing narration...")
    narration_path = os.path.join(paths["input_dir"], "narration.mp3")
    if not os.path.isfile(narration_path):
        raise MediaNotFoundError(
            "Narration file not found: %r" % narration_path,
            hint="Place your ElevenLabs narration as input/narration.mp3.")
    narration = prober.audio(narration_path)
    dur = narration["duration"]
    print("  Narration: %s  (%d Hz, %d ch, %s)" % (
        audio_mod.format_duration(dur), narration["sample_rate"],
        narration["channels"], narration["codec"]))

    # --- Script / scenes -------------------------------------------------
    script_path = find_script(paths["input_dir"])
    print("\n[3/6] Parsing script...")
    if script_path:
        with open(script_path, "r", encoding="utf-8") as fh:
            script_text = fh.read()
        scenes = segment_scenes(script_text)
        print("  Found script with %d scene(s)." % len(scenes))
    else:
        scenes = None
        print("  No script found; using single-scene sequential edit.")

    # --- Match clips to scenes ------------------------------------------
    print("\n[4/6] Matching clips to scenes...")
    if scenes is None:
        from src.timeline import _default_scene
        scenes = _default_scene(dur)
        clip_map = assign_clips_to_scenes(scenes, clips, cfg)
    else:
        clip_map = assign_clips_to_scenes(scenes, clips, cfg)
        for sc in scenes:
            n = len(clip_map.get(sc["index"], []))
            print("  %s  -> %d candidate clip(s)" % (sc["title"], n))

    # --- Build timeline --------------------------------------------------
    print("\n[5/6] Building timeline...")
    timeline = build_timeline(cfg, narration, scenes, clip_map, clips)
    clips_by_name = {c["file"]: c for c in clips}

    # Manual overrides.
    overrides = load_overrides(cfg, paths)
    if overrides:
        print("  Applying manual overrides from timeline_override.json...")
        ov_warnings = apply_overrides(timeline, overrides, clips_by_name)
        for w in ov_warnings:
            print("  WARNING: %s" % w)
            scan_warnings.append(w)

    # --- Write timeline artifacts (always) -------------------------------
    timeline_json = os.path.join(paths["output_dir"], "timeline.json")
    timeline_txt = os.path.join(paths["output_dir"], "timeline.txt")
    write_timeline_json(timeline, timeline_json)
    write_timeline_txt(timeline, timeline_txt)
    print("  Timeline duration: %.3f s (%d shots, %d scenes)"
          % (timeline["duration"], len(timeline["shots"]),
             len(timeline["scenes"])))
    for w in timeline.get("warnings", []):
        print("  WARNING: %s" % w)
        scan_warnings.append(w)

    # --- Subtitles -------------------------------------------------------
    subtitle_path = None
    cues = timeline["subtitles"]
    if cfg.get("subtitle_enabled"):
        if has_text(cues):
            srt = os.path.join(paths["output_dir"], "subtitles.srt")
            write_srt(cues, srt)
            ass = os.path.join(paths["output_dir"], "subtitles.ass")
            write_ass(cues, ass, cfg, cfg["width"], cfg["height"])
            subtitle_path = srt
            print("  Wrote %d subtitle cues -> subtitles.srt / .ass"
                  % len(cues))
        else:
            print("  WARNING: subtitle_enabled is true but no script/transcript "
                  "is available, so no subtitle text can be generated.")
            scan_warnings.append(
                "No subtitle text source (script.txt); subtitles skipped.")

    # Reused clips report.
    reused = timeline["reused_clips"]

    # --- Dry run ---------------------------------------------------------
    if dry_run:
        print("\n[DRY RUN] No rendering performed.")
        _print_dry_run(timeline, reused, clips)
        return 0

    # --- Preview ---------------------------------------------------------
    mode = "preview" if preview else "full"
    output_file = "preview.mp4" if preview else "final_documentary.mp4"
    output_path = os.path.join(paths["output_dir"], output_file)

    if preview:
        timeline = _truncate_timeline(timeline, cfg.get("preview_seconds", 45))

    # --- Render ----------------------------------------------------------
    print("\n[6/6] Rendering (%s)... " % mode)
    renderer = Renderer(cfg, paths, ffmpeg, prober, mode=mode)
    music_path = os.path.join(paths["music_dir"], "background.mp3")
    if cfg.get("music_enabled") and not os.path.isfile(music_path):
        print("  WARNING: music_enabled is true but music/background.mp3 is "
              "missing; continuing without music.")
        scan_warnings.append("Background music requested but not found.")
    render_time = renderer.render(timeline, clips_by_name, narration_path,
                                  music_path, subtitle_path, output_path)
    for w in renderer.warnings():
        scan_warnings.append(w)
        print("  WARNING: %s" % w)
    print("  Finished rendering %s in %.1fs." % (output_path, render_time))

    # --- Report ----------------------------------------------------------
    errors = []
    report = build_report(cfg, timeline, clips, narration_path, music_path,
                          output_path, render_time, scan_warnings, errors,
                          reused, mode=mode)
    report_path = os.path.join(paths["output_dir"], "edit_report.json")
    write_report(report, report_path)
    print("\nDone. Outputs in %s:" % paths["output_dir"])
    for f in ("final_documentary.mp4", "subtitles.srt", "subtitles.ass",
              "timeline.json", "timeline.txt", "edit_report.json"):
        if os.path.isfile(os.path.join(paths["output_dir"], f)):
            print("  - %s" % f)
    if preview:
        print("  (preview.mp4 generated — run without --preview for the "
              "full render)")

    # Cleanup prompt.
    _prompt_cleanup(cfg, paths)
    return 0


def _print_dry_run(timeline, reused, clips):
    print("\n------------------- TIMELINE REVIEW -------------------")
    for scene in timeline["scenes"]:
        print("[%s]  %6.2fs -> %6.2fs" % (scene["title"], scene["start"],
                                          scene["end"]))
    print("--------------------------------------------------------")
    total_source = sum(c["duration"] for c in clips)
    print("Total narration duration : %.3f s" % timeline["duration"])
    print("Total source footage     : %.3f s" % total_source)
    print("Shots planned            : %d" % len(timeline["shots"]))
    print("Distinct clips used      : %d" % len(timeline["reused_clips"]) +
          " (reused)" if timeline["reused_clips"] else "")
    print("Reused clips             : %s"
          % (reused or "none"))
    print("Subtitle cues            : %d" % len(timeline["subtitles"]))
    print("\nSee output/timeline.txt and output/timeline.json for details.")


def _truncate_timeline(timeline, seconds):
    """Return a copy of the timeline cut to ``seconds`` for previews."""
    import copy
    t = copy.deepcopy(timeline)
    shots = []
    for s in t["shots"]:
        if s["start"] >= seconds - 1e-3:
            break
        s = dict(s)
        s["end"] = min(s["end"], seconds)
        s["length"] = max(0.0, round(s["end"] - s["start"], 3))
        shots.append(s)
    t["shots"] = shots
    t["duration"] = round(seconds, 3)
    t["subtitles"] = [c for c in t["subtitles"] if c["start"] < seconds - 0.1]
    return t


def _prompt_cleanup(cfg, paths):
    if not os.path.isdir(paths["temp_dir"]):
        return
    try:
        resp = input("\nDelete temporary files in %s? [y/N] " % paths["temp_dir"])
    except (EOFError, KeyboardInterrupt):
        return
    if resp.strip().lower() in ("y", "yes"):
        shutil.rmtree(paths["temp_dir"], ignore_errors=True)
        print("Temporary files removed.")


if __name__ == "__main__":
    sys.exit(main())
