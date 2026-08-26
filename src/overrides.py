"""Apply manual timeline_override.json entries on top of automatic decisions.

Manual overrides always win. Supported per-scene override:

    {
      "scene_3": {
        "clip": "037.mp4",
        "start": 0,
        "duration": 6.5
      }
    }

``scene_N`` refers to the scene index N from the timeline. When present, the
override replaces the clip(s) used inside that scene's shots and, optionally,
their duration. ``start`` is accepted for compatibility and offsets the first
shot of the scene.
"""

import json
import os

from .errors import OverrideError, ConfigurationError

DEFAULT_OVERRIDE_PATH = "timeline_override.json"


def load_overrides(cfg, paths):
    """Read timeline_override.json if present (search cwd then config)."""
    candidates = [os.path.join(os.getcwd(), DEFAULT_OVERRIDE_PATH)]
    base = paths.get("output_dir")
    if base:
        candidates.append(os.path.join(base, DEFAULT_OVERRIDE_PATH))
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigurationError(
                    "Could not read override file %r: %s" % (path, exc))
            if not isinstance(data, dict):
                raise ConfigurationError(
                    "Override file %r must be a JSON object." % path)
            return data
    return None


def apply_overrides(timeline, overrides, clips_by_name):
    """Mutate the timeline's shots per the override dict. Returns warnings."""
    warnings = []
    if not overrides:
        return warnings
    shots = timeline["shots"]
    for key, ov in overrides.items():
        if not key.lower().startswith("scene_"):
            warnings.append("Ignoring unknown override key %r." % key)
            continue
        try:
            scene_idx = int(key.split("_", 1)[1])
        except ValueError:
            warnings.append("Ignoring malformed override key %r." % key)
            continue
        if not isinstance(ov, dict):
            warnings.append("Override for %r must be an object." % key)
            continue

        scene_shots = [s for s in shots if s["scene"] == scene_idx]
        if not scene_shots:
            warnings.append("Override %r: scene %d not found." % (key, scene_idx))
            continue

        clip = ov.get("clip")
        if clip:
            if clip not in clips_by_name:
                warnings.append("Override %r: clip %r not in manifest." % (
                    key, clip))
                continue
            for s in scene_shots:
                s["clip"] = clip
                s["reused"] = True  # flagged as manually assigned

        dur = ov.get("duration")
        if dur is not None:
            try:
                dur = float(dur)
            except (TypeError, ValueError):
                warnings.append("Override %r: invalid duration." % key)
                dur = None
            if dur and dur > 0:
                for s in scene_shots:
                    s["length"] = round(dur, 3)
                    s["end"] = round(s["start"] + dur, 3)

        start = ov.get("start")
        if start is not None:
            try:
                start = float(start)
            except (TypeError, ValueError):
                start = None
            if start is not None and scene_shots:
                delta = start - scene_shots[0]["start"]
                for s in scene_shots:
                    s["start"] = round(s["start"] + delta, 3)
                    s["end"] = round(s["end"] + delta, 3)

    return warnings
