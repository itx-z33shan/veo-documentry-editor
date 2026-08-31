"""Timeline construction: narration timing, scene durations, shots, subtitles.

This is the heart of the edit. It takes:
  * narration audio info (duration),
  * parsed scenes (from script.py) and matched clips (from matcher.py),
and produces a complete, deterministic timeline dict that the renderer turns
into FFmpeg commands.

Everything here is pure Python — no media processing. FFmpeg only executes
the resulting plan.
"""

import math
import random

from .errors import TimelineError

EPS = 1e-3


def _pick_shot_duration(rng, cfg):
    """Pick a shot duration in [min, max] with natural variation."""
    lo = cfg["min_clip_seconds"]
    hi = cfg["max_clip_seconds"]
    pref = cfg["preferred_clip_seconds"]
    r = rng.random()
    if r < 0.25:
        dur = rng.uniform(lo, pref)
    elif r < 0.70:
        dur = rng.uniform(pref * 0.9, pref * 1.1)
    else:
        dur = rng.uniform(pref, hi)
    return max(lo, min(hi, dur))


def _trim_offset(rng, clip_dur, target, seed):
    """Deterministic source start offset when trimming a long clip."""
    if clip_dur <= target + EPS or target < 0.5:
        return 0.0
    frac = ((seed * 2654435761) % 100) / 100.0
    return round((clip_dur - target) * frac, 3)


def _flatten_sentences(scenes):
    """Return ordered list of {text, words, scene_index} across all scenes."""
    out = []
    for scene in scenes:
        for sent in scene.get("sentences", []):
            words = len(sent.split())
            if words == 0:
                continue
            out.append({"text": sent, "words": words,
                        "scene_index": scene["index"]})
    return out


def _compute_sentence_timing(sentences, total):
    """Tile [0, total] proportionally to word counts for each sentence."""
    total_words = sum(s["words"] for s in sentences)
    if total_words <= 0:
        for s in sentences:
            s["start"], s["end"] = 0.0, total
        return
    acc = 0
    for s in sentences:
        s["start"] = total * acc / total_words
        acc += s["words"]
        s["end"] = total * acc / total_words
        # avoid zero-length cues for very small sentences
        if s["end"] - s["start"] < 0.3:
            s["end"] = s["start"] + 0.3


def _scene_timing(scenes, sentences):
    """Derive [start, end] for each scene from its sentences."""
    idx = {s["index"]: s for s in scenes}
    for scene in scenes:
        scene["start"], scene["end"] = None, None
    for s in sentences:
        sc = idx[s["scene_index"]]
        if sc["start"] is None:
            sc["start"] = s["start"]
        sc["end"] = s["end"]
    for scene in scenes:
        if scene["start"] is None:
            scene["start"] = 0.0
            scene["end"] = 0.0
        scene["start"] = round(scene["start"], 3)
        scene["end"] = round(scene["end"], 3)


def _scenes_pre_timed(scenes):
    """True when scenes already carry real [start, end] windows (e.g. SRT)."""
    return bool(scenes) and all(
        isinstance(s.get("start"), (int, float)) and
        isinstance(s.get("end"), (int, float)) and
        s["end"] > s["start"] for s in scenes)


def _tile_sentences_in_scenes(scenes, sentences):
    """Tile each scene's sentences inside its [start,end] window.

    Used when scenes already carry real boundaries (e.g. from a time-coded
    SRT) so that derived subtitle cues stay consistent with the edit.
    ``sentences`` are mutated in place (start/end assigned), mirroring
    :func:`_compute_sentence_timing`.
    """
    grouped = {}
    for s in sentences:
        grouped.setdefault(s["scene_index"], []).append(s)
    for scene in scenes:
        group = grouped.get(scene["index"])
        if not group:
            continue
        start, end = scene["start"], scene["end"]
        window = max(0.0, end - start)
        total_words = sum(s["words"] for s in group)
        if total_words <= 0:
            for s in group:
                s["start"], s["end"] = start, min(end, start + 0.3)
            continue
        acc = 0
        for s in group:
            s["start"] = start + window * acc / total_words
            acc += s["words"]
            s["end"] = start + window * acc / total_words
            if s["end"] - s["start"] < 0.3:
                s["end"] = min(end, s["start"] + 0.3)


def build_subtitle_cues(cfg, scenes, duration):
    """Build proportional subtitle cues without planning any video shots.

    This is useful when finalising an already edited master: the video edit is
    intentionally left untouched, while a final transcript still needs an SRT
    file.  The caller should label these cues as *review required* because a
    plain transcript does not contain word-level timings.

    ``scenes`` is modified only to receive its derived start/end values, just
    like :func:`build_timeline`.
    """
    total = float(duration)
    if total <= 0:
        raise TimelineError("Subtitle duration must be greater than zero.")
    sentences = _flatten_sentences(scenes)
    if not sentences:
        return []
    _compute_sentence_timing(sentences, total)
    _scene_timing(scenes, sentences)
    return _build_subtitle_cues(sentences, cfg)


def _build_subtitle_cues(sentences, cfg):
    """Group sentences into <=2 line, <=42 char cues with timings."""
    max_chars = cfg["subtitle_max_chars_per_line"] * cfg["subtitle_max_lines"]
    min_disp = cfg["subtitle_min_display_seconds"]
    cues = []
    current = []
    current_chars = 0
    current_words = 0
    for s in sentences:
        chars = len(s["text"])
        if current and current_chars + chars + 1 > max_chars:
            # flush
            cues.append(_finalize_cue(current, min_disp))
            current = []
            current_chars = 0
            current_words = 0
        current.append(s)
        current_chars += chars
        current_words += s["words"]
    if current:
        cues.append(_finalize_cue(current, min_disp))

    # Ensure no negative / overlapping cues.
    for a, b in zip(cues, cues[1:]):
        if b["start"] < a["end"] - EPS:
            b["start"] = a["end"]
    return cues


def _finalize_cue(sentences, min_disp):
    text = " ".join(s["text"] for s in sentences).strip()
    start = sentences[0]["start"]
    end = sentences[-1]["end"]
    if end - start < min_disp:
        end = start + min_disp
    return {"start": round(start, 3), "end": round(end, 3), "text": text}


def build_timeline(cfg, narration_info, scenes, clip_map, clips):
    """Build the full timeline dict.

    Args:
        cfg: config dict.
        narration_info: dict from media.get_audio_info (narration).
        scenes: list of scene dicts from script.segment_scenes, or a single
                default scene if no script was provided.
        clip_map: {scene_index: [clip_filename, ...]} from matcher.
        clips: media manifest list (dicts).
    """
    total = narration_info["duration"]
    if total <= 0:
        raise TimelineError("Narration has invalid duration (<= 0).")

    clips_by_name = {c["file"]: c for c in clips}

    # --- Sentence timing -------------------------------------------------
    sentences = _flatten_sentences(scenes)
    if sentences:
        if _scenes_pre_timed(scenes):
            # Real boundaries already applied (e.g. SRT cue times): keep
            # them and only tile sentence timing inside each scene window
            # so the derived subtitle cues stay consistent with the edit.
            _tile_sentences_in_scenes(scenes, sentences)
        else:
            _compute_sentence_timing(sentences, total)
            _scene_timing(scenes, sentences)
        subtitle_cues = _build_subtitle_cues(sentences, cfg)
    else:
        # No script: single scene covering the whole narration.
        scenes = _default_scene(total)
        subtitle_cues = []

    # --- Shots -----------------------------------------------------------
    rng = random.Random(cfg.get("variation_seed", 7))
    seed = cfg.get("variation_seed", 7)
    shots = []
    used_counts = {}
    reused = []
    warnings = []

    for scene in scenes:
        ordered = clip_map.get(scene["index"]) or []
        if not ordered:
            raise TimelineError(
                "Scene %s has no clips assigned." % scene["index"])
        scene_shots = _plan_scene_shots(
            scene, ordered, clips_by_name, rng, seed, cfg, used_counts, reused,
            warnings)
        shots.extend(scene_shots)

    if not shots:
        raise TimelineError("Timeline contains no shots.")

    # --- Reuse analysis --------------------------------------------------
    reuse_stats = {}
    for name in used_counts:
        if used_counts[name] > 1:
            reuse_stats[name] = used_counts[name]

    # --- Duration check --------------------------------------------------
    planned = sum(s["length"] for s in shots)
    if abs(planned - total) > 1.0:
        warnings.append(
            "Planned footage duration %.3fs differs from narration %.3fs by "
            "more than 1s; final video duration is governed by narration."
            % (planned, total))

    return {
        "duration": round(total, 3),
        "narration": {
            "file": narration_info["file"],
            "duration": round(total, 3),
            "sample_rate": narration_info.get("sample_rate"),
            "channels": narration_info.get("channels"),
            "codec": narration_info.get("codec"),
        },
        "scenes": [{
            "index": s["index"],
            "title": s.get("title"),
            "start": s.get("start", 0.0),
            "end": s.get("end", total),
            "text": s.get("text", ""),
            "clips": clip_map.get(s["index"], []),
        } for s in scenes],
        "shots": shots,
        "subtitles": subtitle_cues,
        "reused_clips": reuse_stats,
        "total_clip_uses": used_counts,
        "warnings": warnings,
    }


def _default_scene(total):
    return [{
        "index": 1,
        "title": "Scene 1",
        "sentences": [],
        "text": "",
        "start": 0.0,
        "end": total,
    }]


def _plan_scene_shots(scene, ordered, clips_by_name, rng, seed, cfg,
                      used_counts, reused, warnings):
    """Fill the scene's [start,end] window with planned shots."""
    s_start = scene.get("start", 0.0)
    s_end = scene.get("end", 0.0)
    if s_end <= s_start:
        return []
    scene_dur = s_end - s_start

    shots = []
    remaining = scene_dur
    pos = s_start
    clip_i = 0
    guard = 0
    while remaining > EPS and guard < 100000:
        guard += 1
        target = _pick_shot_duration(rng, cfg)
        target = min(target, remaining)

        name = ordered[clip_i % len(ordered)]
        clip_i += 1
        clip = clips_by_name.get(name)
        if clip is None:
            warnings.append("Clip %r missing; skipping." % name)
            continue
        clip_dur = clip["duration"]

        used_counts[name] = used_counts.get(name, 0) + 1
        if used_counts[name] > 1:
            reused.append(name)

        shot = {
            "start": round(pos, 3),
            "end": round(pos + target, 3),
            "clip": name,
            "scene": scene["index"],
            "src_start": 0.0,
            "length": round(target, 3),
            "loop": 0,
            "reused": used_counts[name] > 1,
            "loop_used": False,
        }

        if clip_dur >= target - EPS:
            shot["src_start"] = _trim_offset(rng, clip_dur, target, seed)
        elif cfg.get("loop_footage") and clip_dur > 0:
            repeats = int(math.ceil(target / clip_dur))
            repeats = min(repeats, cfg.get("max_loop_count", 20))
            loop_len = repeats * clip_dur
            if loop_len >= target - EPS:
                shot["loop"] = repeats
                shot["loop_used"] = True
            else:
                # even max loops don't reach target; play what we can
                shot["loop"] = repeats
                shot["loop_used"] = True
                shot["end"] = round(pos + loop_len, 3)
                shot["length"] = round(loop_len, 3)
        else:
            # clip shorter than target and looping disabled -> trim as-is
            shot["end"] = round(pos + min(target, clip_dur), 3)
            shot["length"] = round(min(target, clip_dur), 3)

        shots.append(shot)
        remaining = s_end - shot["end"]
        pos = shot["end"]

    return shots
