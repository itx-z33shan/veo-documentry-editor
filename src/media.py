"""FFprobe/FFmpeg discovery and media inspection helpers.

Everything in this module shells out to the system FFmpeg/FFprobe binaries.
The editor performs no video/audio analysis of its own — FFmpeg stays the
source of truth for all media metadata.

``ffprobe`` is preferred and gives structured JSON. When ``ffprobe`` is not
available on the system (an FFmpeg-only install), the tool transparently
falls back to parsing ``ffmpeg -i`` output. Both paths return identical dict
shapes, so the rest of the code never cares which was used.
"""

import json
import os
import re
import shutil
import subprocess

from .errors import EnvironmentError_, MediaProbeError

SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

# Channel-layout -> channel count mapping for the ffmpeg-parse fallback.
_CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4, "5.0": 5,
             "5.1": 6, "6.1": 7, "7.1": 8}


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_binary(name, configured):
    """Resolve an ffmpeg/ffprobe binary path (explicit config > PATH)."""
    if configured:
        candidates = [configured]
        if not os.path.isabs(configured):
            # Also accept paths relative to the repository root, so the
            # bundled tools/ layout works from any working directory.
            candidates.append(os.path.join(_REPO_ROOT, configured))
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return os.path.abspath(candidate)
    found = shutil.which(name)
    if found:
        return os.path.abspath(found)
    return None


def resolve_binaries(cfg):
    """Return (ffmpeg, ffprobe) absolute binary paths.

    ffprobe is optional: returns None if not found (the code falls back to
    ffmpeg-based probing). ffmpeg is required.
    """
    ffmpeg = find_binary("ffmpeg", cfg.get("ffmpeg_bin"))
    if ffmpeg is None:
        raise EnvironmentError_(
            "Required tool 'ffmpeg' was not found. Install FFmpeg or set "
            "'ffmpeg_bin' in config.json.",
            hint="apt-get install ffmpeg  |  brew install ffmpeg  |  "
                 "https://ffmpeg.org/download.html")
    ffprobe = find_binary("ffprobe", cfg.get("ffprobe_bin"))
    return ffmpeg, ffprobe


def _run(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise EnvironmentError_("Failed to run: %s (%s)" % (cmd[0], exc))
    return proc


def _parse_rational(value, default=0.0):
    if not value:
        return default
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(value))
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        return num / den if den else default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------
# ffprobe-based probing
# ----------------------------------------------------------------------
def _probe_ffprobe(path, ffprobe_bin):
    cmd = [ffprobe_bin, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    proc = _run(cmd)
    if proc.returncode != 0 or not proc.stdout.strip():
        tail = proc.stderr.strip().splitlines()
        detail = tail[-1] if tail else "unknown probe error"
        raise MediaProbeError(
            "Could not read media file %r: %s" % (path, detail),
            hint="Is the file corrupt or in an unsupported format?")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise MediaProbeError("Could not parse FFprobe output for %r" % path)


def _pick_stream(data, codec_type):
    for s in data.get("streams", []):
        if s.get("codec_type") == codec_type:
            return s
    return None


def _video_from_ffprobe(path, ffprobe_bin):
    data = _probe_ffprobe(path, ffprobe_bin)
    v = _pick_stream(data, "video")
    if v is None:
        raise MediaProbeError("No video stream found in %r" % path)
    fmt = data.get("format", {})
    duration = _parse_rational(v.get("duration")) or _parse_rational(
        fmt.get("duration"))
    fps = _parse_rational(v.get("avg_frame_rate")) or _parse_rational(
        v.get("r_frame_rate"))
    width = int(v.get("width") or 0)
    height = int(v.get("height") or 0)
    return {
        "file": os.path.basename(path),
        "path": os.path.abspath(path),
        "duration": round(duration, 4),
        "width": width,
        "height": height,
        "fps": round(fps, 4),
        "codec": v.get("codec_name"),
        "has_audio": _pick_stream(data, "audio") is not None,
        "aspect": (width / height) if height else None,
        "nb_frames": v.get("nb_frames"),
    }


def _audio_from_ffprobe(path, ffprobe_bin):
    data = _probe_ffprobe(path, ffprobe_bin)
    a = _pick_stream(data, "audio")
    if a is None:
        raise MediaProbeError("No audio stream found in %r" % path)
    fmt = data.get("format", {})
    duration = _parse_rational(a.get("duration")) or _parse_rational(
        fmt.get("duration"))
    return {
        "file": os.path.basename(path),
        "path": os.path.abspath(path),
        "duration": round(duration, 4),
        "sample_rate": int(a.get("sample_rate") or 0),
        "channels": int(a.get("channels") or 0),
        "codec": a.get("codec_name"),
    }


# ----------------------------------------------------------------------
# ffmpeg -i based probing (fallback when ffprobe is unavailable)
# ----------------------------------------------------------------------
def _ffmpeg_info_lines(path, ffmpeg_bin):
    cmd = [ffmpeg_bin, "-hide_banner", "-i", path]
    proc = _run(cmd)
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    if "Stream #" not in text and "Video:" not in text:
        raise MediaProbeError(
            "Could not read media file %r with ffmpeg." % path)
    return text


def _parse_duration(text):
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        return 0.0
    h, mm, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mm * 60 + s


def _parse_video_stream(text):
    """Return {codec, width, height, fps} from the video stream line."""
    # Stream #0:0(und): Video: h264 (High) (avc1...), yuv420p, 1920x1080 ...,
    #  30 fps, ...
    m = re.search(
        r"Stream #.*?Video:\s*([^,]+).*?(\d{2,5})x(\d{2,5}).*?"
        r"((?:\d+(?:\.\d+)?(?:/\d+)?))\s*fps", text)
    if not m:
        return None
    codec = m.group(1).split()[0]
    width, height = int(m.group(2)), int(m.group(3))
    fps = _parse_rational(m.group(4))
    return {"codec": codec, "width": width, "height": height, "fps": fps}


def _parse_audio_stream(text):
    """Return {codec, sample_rate, channels} from the audio stream line."""
    m = re.search(
        r"Stream #.*?Audio:\s*([^,]+),?\s*(\d+)\s*Hz,\s*([a-z0-9.\s+]+?)(?:,|$)",
        text)
    if not m:
        return None
    codec = m.group(1).split()[0]
    sample_rate = int(m.group(2))
    layout = m.group(3).strip().lower()
    if "5.1" in layout or "5.0" in layout:
        channels = 6 if "5.1" in layout else 5
    else:
        channels = _CHANNELS.get(layout, 2 if "stereo" in layout else 1)
    return {"codec": codec, "sample_rate": sample_rate, "channels": channels}


def _video_from_ffmpeg(path, ffmpeg_bin):
    text = _ffmpeg_info_lines(path, ffmpeg_bin)
    v = _parse_video_stream(text)
    if v is None:
        raise MediaProbeError("No video stream found in %r" % path)
    a = _parse_audio_stream(text)
    return {
        "file": os.path.basename(path),
        "path": os.path.abspath(path),
        "duration": round(_parse_duration(text), 4),
        "width": v["width"],
        "height": v["height"],
        "fps": round(v["fps"], 4),
        "codec": v["codec"],
        "has_audio": a is not None,
        "aspect": (v["width"] / v["height"]) if v["height"] else None,
        "nb_frames": None,
    }


def _audio_from_ffmpeg(path, ffmpeg_bin):
    text = _ffmpeg_info_lines(path, ffmpeg_bin)
    a = _parse_audio_stream(text)
    if a is None:
        raise MediaProbeError("No audio stream found in %r" % path)
    return {
        "file": os.path.basename(path),
        "path": os.path.abspath(path),
        "duration": round(_parse_duration(text), 4),
        "sample_rate": a["sample_rate"],
        "channels": a["channels"],
        "codec": a["codec"],
    }


# ----------------------------------------------------------------------
# Public probe facade
# ----------------------------------------------------------------------
class Probe:
    """Unified media metadata reader (ffprobe with ffmpeg fallback)."""

    def __init__(self, ffmpeg_bin, ffprobe_bin):
        self.ffmpeg = ffmpeg_bin
        self.ffprobe = ffprobe_bin

    def video(self, path):
        if self.ffprobe:
            try:
                return _video_from_ffprobe(path, self.ffprobe)
            except MediaProbeError:
                pass
        return _video_from_ffmpeg(path, self.ffmpeg)

    def audio(self, path):
        if self.ffprobe:
            try:
                return _audio_from_ffprobe(path, self.ffprobe)
            except MediaProbeError:
                pass
        return _audio_from_ffmpeg(path, self.ffmpeg)


def ffmpeg_version(ffmpeg_bin):
    proc = _run([ffmpeg_bin, "-version"])
    if proc.returncode != 0:
        raise EnvironmentError_("ffmpeg returned an error on startup")
    first = proc.stdout.splitlines()[0] if proc.stdout else "unknown"
    return first


def has_filter(ffmpeg_bin, name):
    """Return True if ffmpeg was built with the given filter."""
    try:
        proc = _run([ffmpeg_bin, "-hide_banner", "-filters"])
    except EnvironmentError_:
        return False
    if proc.returncode != 0:
        return False
    # Filter list lines look like: " TSC drawtext V->V  Draw text..."
    pattern = re.compile(r"^\s*\.\.\.\s*%s\s|\b%s\s+V->V|\b%s\s+A->A" %
                         (re.escape(name), re.escape(name), re.escape(name)))
    for line in proc.stdout.splitlines():
        tokens = line.split()
        if len(tokens) >= 2 and tokens[1] == name:
            return True
    return False
