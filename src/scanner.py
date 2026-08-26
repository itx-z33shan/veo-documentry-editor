"""Scan the clips/ directory and build a natural-sorted media manifest."""

import os
import re

from .media import SUPPORTED_VIDEO_EXTS
from .errors import NoClipsError, MediaProbeError, ConfigurationError


def natural_key(filename):
    """Return a key for natural sorting (1,2,3,10 instead of 1,10,2,3)."""
    parts = re.split(r"(\d+)", filename)
    key = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part))
    return key


def list_clip_files(clips_dir):
    """Return supported video files in natural filename order."""
    if not os.path.isdir(clips_dir):
        raise ConfigurationError(
            "Clips directory not found: %r" % clips_dir,
            hint="Create a clips/ folder next to editor.py and put your "
                 "Veo footage inside it.")
    names = []
    for entry in os.listdir(clips_dir):
        full = os.path.join(clips_dir, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[1].lower() in \
                SUPPORTED_VIDEO_EXTS:
            names.append(entry)
    names.sort(key=natural_key)
    return names


def load_clip_metadata(clips_dir):
    """Load optional clips/metadata.json into {filename: meta}."""
    meta_path = os.path.join(clips_dir, "metadata.json")
    if not os.path.isfile(meta_path):
        return {}
    import json
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Could not read %r: %s" % (meta_path, exc))
    if not isinstance(data, dict):
        raise ConfigurationError(
            "%r must contain a JSON object keyed by filename." % meta_path)
    return data


def scan_clips(clips_dir, prober, metadata=None):
    """Scan clips/ and return a list of clip info dicts.

    Corrupt or unsupported files are skipped and reported in ``warnings``.
    Raises NoClipsError if nothing usable remains. ``prober`` is a
    media.Probe object.
    """
    filenames = list_clip_files(clips_dir)
    metadata = metadata if metadata is not None else load_clip_metadata(clips_dir)

    manifest = []
    warnings = []
    for name in filenames:
        path = os.path.join(clips_dir, name)
        try:
            info = prober.video(path)
        except MediaProbeError as exc:
            warnings.append("Skipping %r (unreadable): %s" % (name, exc.message))
            continue
        if info["duration"] <= 0:
            warnings.append("Skipping %r (invalid duration <= 0)." % name)
            continue
        meta = metadata.get(name) or {}
        info["description"] = meta.get("description", "")
        info["tags"] = list(meta.get("tags") or [])
        manifest.append(info)

    if not manifest:
        raise NoClipsError(
            "No usable video clips were found in %r." % clips_dir,
            hint="Copy your Veo clips (*.mp4, *.mov, *.mkv, *.webm, *.m4v) "
                 "into the clips/ folder first.")

    # Rough safety check on whether there is enough total footage to cover
    # the narration is done later in the timeline builder (footage can loop).
    return manifest, warnings
