"""Configuration loading, validation and defaults for Veo Documentary Editor.

Configuration is read from ``config.json`` (optionally overridden with
``--config``). Everything has a sane local-first default so the tool works
out of the box with zero configuration.
"""

import copy
import json
import os
import sys

from .errors import ConfigurationError

DEFAULTS = {
    # --- Output video ----------------------------------------------------
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "video_codec": "libx264",
    "crf": 18,
    "preset": "medium",

    # --- Pacing ----------------------------------------------------------
    # pacing: "slow" | "normal" | "fast"
    "pacing": "normal",
    "min_clip_seconds": None,        # None => derived from pacing
    "preferred_clip_seconds": None,  # None => derived from pacing
    "max_clip_seconds": None,        # None => derived from pacing
    "variation_seed": 7,             # deterministic pseudo-random variation

    # --- Transitions -----------------------------------------------------
    # "cut" | "crossfade"
    "transition": "cut",
    "crossfade_seconds": 0.3,

    # --- Footage handling ------------------------------------------------
    "fit": "pad",                    # "pad" | "crop"
    "clip_strategy": "semantic",     # "semantic" | "sequential"
    "fallback_strategy": "sequential",
    "loop_footage": True,            # loop clips when shorter than a shot
    "max_loop_count": 20,            # safety cap on how many loops per shot
    "clip_audio_enabled": False,     # keep (quiet) original clip audio

    # --- Audio -----------------------------------------------------------
    "loudness_target_lufs": -14.0,
    "loudness_target_tp": -1.5,
    "loudness_lra": 11.0,
    "sample_rate": 48000,
    # Optional explicit paths. When unset, conventional names such as
    # input/narration.aac and music/background.m4a are auto-discovered.
    "narration_path": None,
    "music_path": None,
    "music_enabled": True,
    "music_volume": 0.08,
    "ducking_enabled": True,
    "ducking_threshold": 0.03,
    "ducking_ratio": 8.0,
    "ducking_attack_ms": 20,
    "ducking_release_ms": 300,
    "clip_audio_volume": 0.15,       # relative volume of clip audio if enabled

    # --- Subtitles -------------------------------------------------------
    "subtitle_enabled": True,
    "subtitle_burn_in": False,
    "subtitle_max_chars_per_line": 42,
    "subtitle_max_lines": 2,
    "subtitle_min_display_seconds": 1.0,
    "subtitle_font": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "subtitle_font_size": 24,

    # --- Intro / outro (visual, does not change narration length) --------
    "intro_enabled": False,
    "intro_title": "VEO DOCUMENTARY",
    "intro_duration_seconds": 3.0,
    "outro_enabled": False,
    "outro_text": "Subscribe for more documentaries.",
    "outro_duration_seconds": 4.0,

    # --- Silences / pause analysis ---------------------------------------
    "silence_min_duration": 0.3,
    "silence_threshold_db": -45,

    # --- Encoding --------------------------------------------------------
    "faststart": True,
    "aac_bitrate": 192,
    "threads": 0,                    # 0 => let ffmpeg decide

    # --- Rendering -------------------------------------------------------
    "resume": True,          # reuse completed intermediate steps when inputs are identical
    "force": False,          # ignore cached intermediates and re-render everything
    "preview_seconds": 45,
    "preview_width": 640,
    "preview_height": 360,
    "preview_crf": 28,
    "preview_preset": "veryfast",

    # --- Existing-export / mastering workflow -----------------------------
    # Used by: python editor.py --master input/master.mp4
    # preserve = master has its own final mix; replace = narration only;
    # rebuild = clean narration + external music stem.
    "master_audio_mode": "preserve",
    "master_fade_seconds": 0.0,
    "master_sync_tolerance_seconds": 0.35,
    "master_output_name": "final_master.mp4",

    # --- AI layer (optional, Gemini free tier) ----------------------------
    "ai_provider": None,          # "gemini" | None (deterministic)
    "ai_api_key_env": "GEMINI_API_KEY",
    "ai_vision_model": None,      # None => "models/gemini-3.7-flash"
    "ai_embedding_model": None,   # None => "models/gemini-embedding-2"
    "ai_decision_model": None,    # None => "models/gemini-3.1-pro-preview"
    "ai_top_k": 5,
    "ai_max_video_bytes": 30 * 1024 * 1024,
    "ai_vector_db_path": "output/clip_vectors.json",

    # --- Paths / binaries ------------------------------------------------
    "ffmpeg_bin": "ffmpeg",
    "ffprobe_bin": "ffprobe",
    "temp_dir": "temp",
    "output_dir": "output",
    "clips_dir": "clips",
    "input_dir": "input",
    "music_dir": "music",

    # --- AI layer (optional, unused in v1 core) --------------------------
    "ai_provider": None,
    "ai_model": None,
    "ai_api_key_env": None,
}

PACING = {
    # min, preferred, max shot durations in seconds
    "slow":   (5, 7, 10),
    "normal": (3, 5, 8),
    "fast":   (2, 4, 5),
}

VALID_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"}
VALID_CRF = (18, 20, 22, 24)
VALID_TRANSITIONS = {"cut", "crossfade"}
VALID_FIT = {"pad", "crop"}
VALID_STRATEGIES = {"semantic", "sequential"}
VALID_PACING = set(PACING.keys())
VALID_MASTER_AUDIO_MODES = {"preserve", "replace", "rebuild"}


def _validate(v):
    if v["crf"] not in VALID_CRF:
        raise ConfigurationError(
            "crf must be one of %s, got %r"
            % (sorted(VALID_CRF), v["crf"]))
    if v["preset"] not in VALID_PRESETS:
        raise ConfigurationError(
            "preset must be one of x264 presets, got %r" % v["preset"])
    if v["transition"] not in VALID_TRANSITIONS:
        raise ConfigurationError(
            "transition must be one of %s, got %r"
            % (sorted(VALID_TRANSITIONS), v["transition"]))
    if v["fit"] not in VALID_FIT:
        raise ConfigurationError(
            "fit must be one of %s, got %r" % (sorted(VALID_FIT), v["fit"]))
    if v["clip_strategy"] not in VALID_STRATEGIES:
        raise ConfigurationError(
            "clip_strategy must be one of %s, got %r"
            % (sorted(VALID_STRATEGIES), v["clip_strategy"]))
    if v["fallback_strategy"] not in VALID_STRATEGIES:
        raise ConfigurationError(
            "fallback_strategy must be one of %s, got %r"
            % (sorted(VALID_STRATEGIES), v["fallback_strategy"]))
    if v["pacing"] not in VALID_PACING:
        raise ConfigurationError(
            "pacing must be one of %s, got %r"
            % (sorted(VALID_PACING), v["pacing"]))
    if v["width"] <= 0 or v["height"] <= 0 or v["fps"] <= 0:
        raise ConfigurationError("width/height/fps must be positive")
    if not (0.0 < v["music_volume"] <= 1.0):
        raise ConfigurationError("music_volume must be in (0, 1]")
    if v["crossfade_seconds"] < 0:
        raise ConfigurationError("crossfade_seconds must be >= 0")
    if v["master_audio_mode"] not in VALID_MASTER_AUDIO_MODES:
        raise ConfigurationError(
            "master_audio_mode must be one of %s, got %r"
            % (sorted(VALID_MASTER_AUDIO_MODES), v["master_audio_mode"]))
    if v["master_fade_seconds"] < 0:
        raise ConfigurationError("master_fade_seconds must be >= 0")
    if v["master_sync_tolerance_seconds"] < 0:
        raise ConfigurationError("master_sync_tolerance_seconds must be >= 0")


def load_config(path=None, overrides=None):
    """Load config from ``config.json`` (or ``path``) merged over defaults.

    ``overrides`` is an optional dict of extra key/value overrides.
    Returns a flat dict.
    """
    cfg = copy.deepcopy(DEFAULTS)

    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "config.json")
        path = os.path.abspath(path)

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                "config.json is not valid JSON: %s" % exc)
        except OSError as exc:
            raise ConfigurationError("cannot read config: %s" % exc)
        if not isinstance(data, dict):
            raise ConfigurationError("config.json must contain a JSON object")
        for key, value in data.items():
            cfg[key] = value

    if overrides:
        for key, value in overrides.items():
            cfg[key] = value

    _validate(cfg)
    # Resolve pacing-derived shot durations if not explicitly set.
    mn, pf, mx = PACING.get(cfg["pacing"], PACING["normal"])
    if cfg["min_clip_seconds"] is None:
        cfg["min_clip_seconds"] = mn
    if cfg["preferred_clip_seconds"] is None:
        cfg["preferred_clip_seconds"] = pf
    if cfg["max_clip_seconds"] is None:
        cfg["max_clip_seconds"] = mx

    if not (cfg["min_clip_seconds"] <= cfg["preferred_clip_seconds"]
            <= cfg["max_clip_seconds"]):
        raise ConfigurationError(
            "clip pacing must satisfy min <= preferred <= max")

    return cfg


def resolved_paths(cfg, base_dir=None):
    """Return absolute paths for the well-known directories.

    If a configured path is relative it is resolved against ``base_dir``
    (defaults to the current working directory, i.e. where editor.py runs).
    """
    base_dir = base_dir or os.getcwd()
    out = {}
    for key in ("clips_dir", "input_dir", "music_dir",
                "temp_dir", "output_dir"):
        p = cfg.get(key)
        if not os.path.isabs(p):
            p = os.path.join(base_dir, p)
        out[key] = os.path.normpath(p)
    return out
