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
from src.inputs import find_narration, find_music
from src.mastering import MasterFinisher, write_master_subtitles, write_master_report
from src.scanner import scan_clips, load_clip_metadata
from src.script import segment_scenes, find_script
from src.transcription import (find_caption_srt, srt_to_plain_text,
                               parse_srt, apply_srt_scene_timing)
from src.matcher import assign_clips_to_scenes, assign_clips_with_ai
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
    p.add_argument(
        "--master", metavar="VIDEO",
        help="Finish an already edited CapCut/Premiere/DaVinci export without "
             "re-cutting its visuals. Example: --master input/master.mp4")
    p.add_argument(
        "--master-audio-mode", choices=("preserve", "replace", "rebuild"),
        help="For --master: preserve its existing mix, replace it with clean "
             "narration, or rebuild from narration plus an external music stem.")
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
        if args.master:
            return _run_master(cfg, paths, args.master,
                               audio_mode=args.master_audio_mode,
                               dry_run=args.dry_run, preview=args.preview)
        return _run(cfg, paths, preview=args.preview, dry_run=args.dry_run)
    except EditorError as exc:
        print("\nERROR: %s" % exc.message, file=sys.stderr)
        if exc.hint:
            print("  Hint: %s" % exc.hint, file=sys.stderr)
        return 1


def _run_master(cfg, paths, master_path, audio_mode=None, dry_run=False,
                preview=False):
    """Finish a single already-edited master without rebuilding its cuts."""
    if preview:
        raise ConfigurationError(
            "--preview is not available with --master.",
            hint="Use --dry-run to inspect the mastering plan, or finish the "
                 "master directly after checking the exported CapCut video.")

    ffmpeg, ffprobe = resolve_binaries(cfg)
    prober = Probe(ffmpeg, ffprobe)
    try:
        print("FFmpeg: %s" % ffmpeg_version(ffmpeg))
    except EditorError as exc:
        print("WARNING: %s" % exc.message)
    if ffprobe:
        print("FFprobe: %s" % ffprobe)
    else:
        print("FFprobe: not found — using ffmpeg-based probing.")

    for key in ("input_dir", "music_dir", "output_dir", "temp_dir"):
        os.makedirs(paths[key], exist_ok=True)

    # The narration is optional only in preserve mode, where it acts as a
    # useful sync reference but is deliberately not mixed into a baked mix.
    narration_path = find_narration(
        paths["input_dir"], cfg.get("narration_path"), required=False)
    requested_mode = audio_mode or cfg.get("master_audio_mode", "preserve")
    # An external music file is meaningful only for a deliberate rebuild.
    # Do not make a discovered file look as though it affected preserve mode.
    music_path = find_music(paths["music_dir"], cfg.get("music_path"),
                            required=False) if requested_mode == "rebuild" else None
    finisher = MasterFinisher(cfg, ffmpeg, prober)
    plan = finisher.prepare(master_path, audio_mode=audio_mode,
                            narration_path=narration_path,
                            music_path=music_path)

    print("\n[Master] Existing visual edit")
    master = plan["master"]
    print("  %s  (%s, %dx%d, %.3f fps)" % (
        master["file"], audio_mod.format_duration(plan["duration"]),
        master["width"], master["height"], master["fps"]))
    print("  Audio mode: %s" % plan["audio_mode"])
    if plan.get("narration"):
        narration = plan["narration"]
        print("  Narration reference: %s  (%d Hz, %d ch, %s)" % (
            narration["file"], narration["sample_rate"],
            narration["channels"], narration["codec"]))
    if plan.get("music"):
        print("  External music stem: %s" % plan["music"]["file"])
    for warning in plan["warnings"]:
        print("  WARNING: %s" % warning)

    subtitles = None
    if cfg.get("subtitle_enabled"):
        script_path = (find_caption_srt(paths["input_dir"]) or
                       find_script(paths["input_dir"]))
        subtitles, subtitle_warnings = write_master_subtitles(
            script_path, os.path.join(paths["output_dir"], "subtitles.srt"),
            cfg, plan["duration"])
        plan["warnings"].extend(subtitle_warnings)
        if subtitles:
            label = "copied" if subtitles["source"] == "provided_srt" else "generated"
            count = subtitles["cue_count"]
            count_text = "" if count is None else " (%d cues)" % count
            print("  SRT %s%s -> %s" % (label, count_text,
                                         subtitles["path"]))
        elif script_path:
            print("  WARNING: no usable subtitle cues were generated.")
    if cfg.get("subtitle_burn_in"):
        plan["warnings"].append(
            "--master writes an upload-ready SRT sidecar only; it does not "
            "burn captions into the already edited master.")

    output_name = cfg.get("master_output_name", "final_master.mp4")
    output_path = output_name if os.path.isabs(output_name) else os.path.join(
        paths["output_dir"], output_name)
    if dry_run:
        plan_path = os.path.join(paths["output_dir"], "master_plan.json")
        write_master_report(plan, None, subtitles, plan_path)
        print("\n[DRY RUN] No render performed.")
        print("  Mastering plan: %s" % plan_path)
        print("  Planned output: %s" % output_path)
        return 0

    print("\n[Master] Finishing audio and packaging video...")
    output = finisher.finish(plan, output_path)
    report_path = os.path.join(paths["output_dir"], "master_report.json")
    write_master_report(plan, output, subtitles, report_path)
    print("  Finished: %s" % output["path"])
    print("  Report:   %s" % report_path)
    if subtitles:
        print("  SRT:      %s" % subtitles["path"])
    return 0


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
    narration_path = find_narration(
        paths["input_dir"], cfg.get("narration_path"), required=True)
    narration = prober.audio(narration_path)
    dur = narration["duration"]
    print("  Narration: %s  (%d Hz, %d ch, %s)" % (
        audio_mod.format_duration(dur), narration["sample_rate"],
        narration["channels"], narration["codec"]))

    # --- Script / scenes -------------------------------------------------
    script_path = find_script(paths["input_dir"])
    caption_srt = find_caption_srt(paths["input_dir"])
    print("\n[3/6] Parsing script...")
    if script_path:
        with open(script_path, "r", encoding="utf-8-sig") as fh:
            script_text = fh.read()
        scenes = segment_scenes(script_text)
        print("  Found script with %d scene(s)." % len(scenes))
    elif caption_srt:
        script_text = srt_to_plain_text(caption_srt)
        scenes = segment_scenes(script_text)
        print("  Found time-coded SRT with %d derived scene(s)." % len(scenes))

    # When a time-coded SRT is available, drive scene boundaries from its
    # real cue times so every clip change lands on a narration beat.
    if caption_srt and scenes:
        srt_cues = parse_srt(caption_srt)
        if apply_srt_scene_timing(scenes, srt_cues):
            print("  Scene timing driven by SRT cue times (real narration sync).")
        else:
            print("  WARNING: SRT provided no usable cue times; using "
                  "estimated scene timing.")
            scan_warnings.append("SRT provided no usable cue times; "
                                 "using estimated scene timing.")
    else:
        scenes = None
        print("  No script found; using single-scene sequential edit.")

    # --- Match clips to scenes ------------------------------------------
    print("\n[4/6] Matching clips to scenes...")
    use_ai = cfg.get("ai_provider") == "gemini"
    if scenes is None:
        from src.timeline import _default_scene
        scenes = _default_scene(dur)

    if use_ai:
        print("  Using Gemini AI intelligence layer...")
        clip_map, ai_warnings = assign_clips_with_ai(scenes, clips, cfg,
                                                     metadata)
        for w in ai_warnings:
            print("  AI: %s" % w)
            scan_warnings.append("AI: " + w)
    else:
        clip_map = assign_clips_to_scenes(scenes, clips, cfg)

    if scenes and not use_ai:
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
        if caption_srt:
            srt = os.path.join(paths["output_dir"], "subtitles.srt")
            shutil.copyfile(caption_srt, srt)
            subtitle_path = srt
            print("  Copied time-coded SRT -> subtitles.srt (original timing preserved)")
        elif has_text(cues):
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
                "No subtitle text source (script.txt or caption SRT); subtitles skipped.")

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
    # A discovered music file must not override an explicit no-external-music
    # profile such as veo-embedded-bed.json.
    music_path = None
    if cfg.get("music_enabled", True):
        music_path = find_music(paths["music_dir"], cfg.get("music_path"),
                                required=False)
        if not music_path:
            print("  WARNING: music_enabled is true but no background music file "
                  "was found; continuing without an external music stem.")
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
