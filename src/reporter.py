"""Produce human-readable timeline.txt and machine-readable edit_report.json."""

import json
import os
import time


def _fmt(seconds):
    seconds = max(0.0, float(seconds))
    m, s = divmod(int(seconds), 60)
    return "%02d:%06.3f" % (m, s)


def write_timeline_txt(timeline, path):
    """Write a readable SCENE list (see section 21)."""
    lines = []
    lines.append("VEO DOCUMENTARY EDITOR — TIMELINE")
    lines.append("Total narration duration: %.3f s" % timeline["duration"])
    lines.append("Shots: %d" % len(timeline["shots"]))
    lines.append("")
    shots = timeline["shots"]
    for i, shot in enumerate(shots, 1):
        flag = "  [REUSED]" if shot.get("reused") else ""
        loop = "  [loop x%d]" % shot["loop"] if shot.get("loop") else ""
        lines.append("SHOT %03d  (Scene %s)" % (i, shot.get("scene")))
        lines.append("%s - %s" % (_fmt(shot["start"]), _fmt(shot["end"])))
        lines.append("%s%s%s" % (shot["clip"], flag, loop))
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    return path


def build_report(cfg, timeline, clips, narration_path, music_path, output_path,
                 render_time, warnings, errors, reused_clips, mode="full"):
    """Assemble the edit_report.json dict."""
    total_source = sum(c["duration"] for c in clips)
    distinct_used = {s["clip"] for s in timeline["shots"]}
    reused_names = list(reused_clips.keys())

    # Final output metadata if it exists.
    out_info = None
    if output_path and os.path.isfile(output_path):
        out_info = {
            "file": os.path.basename(output_path),
            "size_bytes": os.path.getsize(output_path),
            "path": output_path,
        }

    return {
        "tool": "Veo Documentary Editor",
        "mode": mode,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "narration": timeline["narration"],
        "final_video_duration": timeline["duration"],
        "scene_count": len(timeline["scenes"]),
        "shot_count": len(timeline["shots"]),
        "clip_count_total": len(clips),
        "clips_used_distinct": len(distinct_used),
        "reused_clips": {k: v for k, v in reused_clips.items()},
        "reused_clip_count": len(reused_names),
        "total_source_footage_seconds": round(total_source, 3),
        "output": out_info,
        "output_resolution": "%dx%d" % (cfg["width"], cfg["height"]),
        "fps": cfg["fps"],
        "video_codec": cfg["video_codec"],
        "crf": cfg["crf"],
        "preset": cfg["preset"],
        "audio": {
            "codec": "aac",
            "bitrate_kbps": cfg.get("aac_bitrate", 192),
            "loudness_target_lufs": cfg.get("loudness_target_lufs"),
            "true_peak_dbtp": cfg.get("loudness_target_tp"),
            "sample_rate": cfg.get("sample_rate"),
            "music_enabled": bool(music_path and os.path.isfile(music_path)),
            "music_volume": cfg.get("music_volume"),
            "ducking_enabled": cfg.get("ducking_enabled"),
        },
        "subtitle_enabled": cfg.get("subtitle_enabled"),
        "subtitle_burn_in": cfg.get("subtitle_burn_in"),
        "transition": cfg.get("transition"),
        "clip_strategy": cfg.get("clip_strategy"),
        "render_time_seconds": round(render_time, 3),
        "warnings": warnings,
        "errors": errors,
    }


def write_report(report, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    return path


def write_timeline_json(timeline, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(timeline, fh, indent=2, ensure_ascii=False)
    return path
