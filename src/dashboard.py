"""Local-first web dashboard for the documentary finishing workflows.

The dashboard deliberately uses only Python's standard library.  It runs on
one trusted machine and invokes the existing ``editor.py`` command through
structured workflow choices; the browser never receives an FFmpeg command or
a Gemini API key.

It is not an authenticated multi-user service.  Keep the default loopback
binding for normal use.  If you deliberately bind it to a LAN interface, put
it behind appropriate network controls.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import load_config
from .errors import EditorError
from .inputs import AUDIO_EXTENSIONS, find_music, find_narration
from .media import Probe, SUPPORTED_VIDEO_EXTS, resolve_binaries
from .script import find_script
from .transcription import (find_caption_srt, local_whisper_status,
                            transcribe_to_srt)


MAX_UPLOAD_BYTES = 12 * 1024 * 1024 * 1024  # generous enough for long masters
JSON_LIMIT_BYTES = 1024 * 1024
CHUNK_SIZE = 1024 * 1024

TEXT_EXTENSIONS = {".txt", ".srt"}
VIDEO_EXTENSIONS = set(SUPPORTED_VIDEO_EXTS)

WORKFLOWS = {
    "master-preserve": {
        "title": "Finished CapCut master",
        "engine": "master",
        "audio_mode": "preserve",
        "profile": "master-preserve.json",
        "description": "Keep the baked CapCut mix and master it safely.",
    },
    "master-replace": {
        "title": "Master with narration only",
        "engine": "master",
        "audio_mode": "replace",
        "profile": "master-replace-narration.json",
        "description": "Discard the embedded mix and use clean narration only.",
    },
    "master-rebuild": {
        "title": "Master rebuilt from clean stems",
        "engine": "master",
        "audio_mode": "rebuild",
        "profile": "master-rebuild-with-music.json",
        "description": "Mix clean narration with a separate music stem.",
    },
    "clips-embedded": {
        "title": "Raw clips with embedded audio",
        "engine": "clips",
        "audio_mode": None,
        "profile": "veo-embedded-bed.json",
        "description": "Use source clips with their quiet embedded music/ambience.",
    },
    "clips-music": {
        "title": "Raw clips with external music",
        "engine": "clips",
        "audio_mode": None,
        "profile": "veo-embedded-bed.json",
        "description": "Use source clips, clean narration, and a separate music bed.",
    },
}


class DashboardError(Exception):
    """Safe, user-facing dashboard error."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_filename(name: str) -> str:
    """Strip paths and unsafe characters from a browser-supplied filename."""
    name = Path(str(name or "")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise DashboardError("The uploaded file has an invalid filename.")
    return name[:180]


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _number(settings, key, default, minimum, maximum):
    value = settings.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DashboardError("%s must be a number." % key)
    if not minimum <= value <= maximum:
        raise DashboardError("%s must be between %s and %s." %
                             (key, minimum, maximum))
    return value


def _public_probe(info):
    """Remove absolute local paths before returning metadata to the browser."""
    if not info:
        return None
    return {key: value for key, value in info.items() if key != "path"}


def _file_summary(path: Path):
    if not path or not path.is_file():
        return None
    stat = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        "modified_at": int(stat.st_mtime),
    }


def _human_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "%.1f %s" % (value, unit)
        value /= 1024


def _find_master(input_dir: Path):
    for ext in sorted(VIDEO_EXTENSIONS):
        candidate = input_dir / ("master" + ext)
        if candidate.is_file():
            return candidate
    return None


def _profile_config(repo_root: Path, workflow: str):
    definition = WORKFLOWS.get(workflow)
    if not definition:
        raise DashboardError("Unknown workflow: %s" % workflow)
    profile = repo_root / "profiles" / definition["profile"]
    try:
        with profile.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise DashboardError("Could not load dashboard profile %s: %s" %
                             (profile.name, exc))
    if not isinstance(data, dict):
        raise DashboardError("Dashboard profile %s must contain a JSON object."
                             % profile.name)
    return data


def build_dashboard_config(repo_root, workflow, settings):
    """Build a restricted render config from a workflow and UI settings.

    Only a small safe set of documented controls can be changed by the web UI.
    It cannot pass arbitrary FFmpeg flags or arbitrary executable paths.
    """
    repo_root = Path(repo_root).resolve()
    definition = WORKFLOWS.get(workflow)
    if not definition:
        raise DashboardError("Choose a valid finishing workflow.")
    if not isinstance(settings, dict):
        raise DashboardError("Dashboard settings must be an object.")

    config = _profile_config(repo_root, workflow)
    warnings = []
    config["loudness_target_lufs"] = _number(
        settings, "loudnessTarget", config.get("loudness_target_lufs", -14),
        -24.0, -10.0)
    config["loudness_target_tp"] = _number(
        settings, "truePeak", config.get("loudness_target_tp", -1.5),
        -3.0, -0.1)
    config["aac_bitrate"] = int(round(_number(
        settings, "aacBitrate", config.get("aac_bitrate", 256), 96, 512)))
    config["subtitle_enabled"] = _as_bool(
        settings.get("subtitles"), config.get("subtitle_enabled", True))
    # The dashboard's delivery choice is SRT sidecar, not captions burned into
    # an already approved visual master.
    config["subtitle_burn_in"] = False
    config["faststart"] = True

    if definition["engine"] == "master":
        config["master_audio_mode"] = definition["audio_mode"]
        config["master_fade_seconds"] = _number(
            settings, "masterFade", config.get("master_fade_seconds", 0.35),
            0.0, 5.0)
        config["master_output_name"] = "final_master.mp4"
        return config, warnings

    embedded_audio = workflow == "clips-embedded"
    config["clip_audio_enabled"] = _as_bool(
        settings.get("keepClipAudio"), embedded_audio)
    config["clip_audio_volume"] = _number(
        settings, "clipAudioVolume", config.get("clip_audio_volume", 0.12),
        0.01, 0.50)
    config["clip_audio_ducking_enabled"] = _as_bool(
        settings.get("clipAudioDucking"),
        config.get("clip_audio_ducking_enabled", True))
    config["music_enabled"] = workflow == "clips-music"
    config["music_volume"] = _number(
        settings, "musicVolume", config.get("music_volume", 0.08), 0.01, 0.50)
    config["ducking_enabled"] = _as_bool(
        settings.get("ducking"), config.get("ducking_enabled", True))

    requested_transition = str(settings.get("transition", "cut")).lower()
    if requested_transition not in {"cut", "crossfade"}:
        raise DashboardError("Transition must be either cut or crossfade.")
    if config["clip_audio_enabled"] and requested_transition == "crossfade":
        # Renderer correctly warns about this, but the dashboard makes the
        # conservative editorial decision rather than silently losing the bed.
        requested_transition = "cut"
        warnings.append(
            "Hard cuts were kept because crossfades with embedded clip audio "
            "can drop or disrupt the clip music/ambience.")
    config["transition"] = requested_transition
    config["crossfade_seconds"] = _number(
        settings, "crossfadeSeconds", config.get("crossfade_seconds", 0.3),
        0.1, 1.0)
    return config, warnings


def recommend_workflow(media):
    """Return a conservative workflow recommendation based on media topology."""
    master = media.get("master") or {}
    narration = media.get("narration") or {}
    music = media.get("music") or {}
    clips = media.get("clips") or {}

    if master.get("exists"):
        has_audio = master.get("media", {}).get("has_audio")
        if has_audio is not False:
            return {
                "workflow": "master-preserve",
                "title": "Preserve the finished CapCut mix",
                "reason": (
                    "The master video has an audio stream. Preserve mode avoids "
                    "mixing the clean narration over a voice that is already "
                    "baked into the export."),
                "warnings": ([
                    "A separate narration file will be used only as a sync "
                    "reference in preserve mode."] if narration.get("exists") else []),
            }
        if narration.get("exists") and music.get("exists"):
            return {
                "workflow": "master-rebuild",
                "title": "Rebuild audio from clean stems",
                "reason": "The master has no embedded audio and clean narration plus music are available.",
                "warnings": [],
            }
        if narration.get("exists"):
            return {
                "workflow": "master-replace",
                "title": "Use the clean narration",
                "reason": "The master has no embedded audio and a narration file is available.",
                "warnings": [],
            }
    if clips.get("count", 0) and narration.get("exists"):
        if clips.get("audio_clip_count", 0):
            return {
                "workflow": "clips-embedded",
                "title": "Use source clips with their quiet embedded audio",
                "reason": "Source clips and a clean narration are available; clip audio can remain underneath the voice.",
                "warnings": [],
            }
        if music.get("exists"):
            return {
                "workflow": "clips-music",
                "title": "Use raw clips with a separate music stem",
                "reason": "Source clips, narration, and music are available as separate inputs.",
                "warnings": [],
            }
    return {
        "workflow": "master-preserve",
        "title": "Start with a finished master",
        "reason": "Add a CapCut export, narration reference, and transcript to receive a media-aware recommendation.",
        "warnings": [],
    }


class DashboardState:
    """Owns workspace media, structured render jobs, and API-facing state."""

    def __init__(self, repo_root):
        self.repo_root = Path(repo_root).resolve()
        self.input_dir = self.repo_root / "input"
        self.clips_dir = self.repo_root / "clips"
        self.music_dir = self.repo_root / "music"
        self.output_dir = self.repo_root / "output"
        self.temp_dir = self.repo_root / "temp" / "dashboard"
        self.static_dir = self.repo_root / "web" / "static"
        # Distinct names let the dashboard invalidate only its own generated
        # captions when a source video/audio file changes, never a user's
        # manually supplied transcript or SRT.
        self.dashboard_transcript = self.input_dir / "dashboard_transcript.txt"
        self.dashboard_captions = self.input_dir / "dashboard_captions.srt"
        for directory in (self.input_dir, self.clips_dir, self.music_dir,
                          self.output_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._job = {
            "id": None,
            "status": "idle",
            "action": None,
            "workflow": None,
            "started_at": None,
            "finished_at": None,
            "stage": "Idle",
            "logs": [],
            "returncode": None,
            "error": None,
            "warnings": [],
            "output_files": [],
            "process": None,
            "cancel_requested": False,
        }

    # ------------------------------------------------------------------
    # Media inventory / inspection
    # ------------------------------------------------------------------
    def _base_config(self):
        return load_config(str(self.repo_root / "config.json"))

    def _prober(self):
        try:
            config = self._base_config()
            ffmpeg, ffprobe = resolve_binaries(config)
            return Probe(ffmpeg, ffprobe), None
        except EditorError as exc:
            return None, exc.message

    def health(self):
        prober, error = self._prober()
        whisper_available, whisper_message = local_whisper_status()
        return {
            "ffmpeg_available": bool(prober),
            "message": ("FFmpeg and media inspection are ready." if prober
                        else (error or "FFmpeg is not available.")),
            "local_whisper_available": whisper_available,
            "local_whisper_message": whisper_message,
            "local_only_notice": "This dashboard is intended for a trusted local machine.",
        }

    def _inspect(self, path, kind, prober):
        result = _file_summary(path)
        if result is None:
            return {"exists": False}
        result["exists"] = True
        if prober:
            try:
                info = prober.video(str(path)) if kind == "video" else prober.audio(str(path))
                result["media"] = _public_probe(info)
            except EditorError as exc:
                result["probe_error"] = exc.message
        return result

    def media_summary(self):
        prober, probe_error = self._prober()
        whisper_available, whisper_message = local_whisper_status()
        master_path = _find_master(self.input_dir)
        narration_path = find_narration(str(self.input_dir), required=False)
        music_path = find_music(str(self.music_dir), required=False)
        script_path = find_script(str(self.input_dir))
        caption_srt = find_caption_srt(str(self.input_dir))
        transcript_source = script_path or caption_srt

        master = self._inspect(master_path, "video", prober) if master_path else {"exists": False}
        narration = (self._inspect(Path(narration_path), "audio", prober)
                     if narration_path else {"exists": False})
        music = (self._inspect(Path(music_path), "audio", prober)
                 if music_path else {"exists": False})
        transcript = _file_summary(Path(transcript_source)) if transcript_source else None

        clip_paths = sorted(
            [path for path in self.clips_dir.iterdir()
             if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS],
            key=lambda path: path.name.lower())
        clip_files = []
        audio_clip_count = 0
        for path in clip_paths:
            item = self._inspect(path, "video", prober)
            if item.get("media", {}).get("has_audio"):
                audio_clip_count += 1
            clip_files.append(item)
        clips = {
            "count": len(clip_files),
            "audio_clip_count": audio_clip_count,
            "total_bytes": sum(item.get("size_bytes", 0) for item in clip_files),
            "files": clip_files,
        }

        media = {
            "health": {
                "ffmpeg_available": bool(prober),
                "message": ("Media inspection is ready." if prober else probe_error),
                "local_whisper_available": whisper_available,
                "local_whisper_message": whisper_message,
            },
            "master": master,
            "narration": narration,
            "music": music,
            "transcript": ({"exists": True, **transcript} if transcript
                           else {"exists": False}),
            "clips": clips,
        }
        media["recommendation"] = recommend_workflow(media)
        return media

    # ------------------------------------------------------------------
    # Upload handling
    # ------------------------------------------------------------------
    def _remove_named_media(self, directory: Path, stem: str, extensions):
        for extension in extensions:
            candidate = directory / (stem + extension)
            if candidate.is_file():
                candidate.unlink()

    def _clear_clip_media(self):
        for path in self.clips_dir.iterdir():
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                path.unlink()

    def _clear_dashboard_transcription(self):
        """Remove only dashboard-generated captions, never user files."""
        for path in (self.dashboard_transcript, self.dashboard_captions):
            path.unlink(missing_ok=True)

    def _has_user_caption_source(self):
        """True when a user supplied TXT/SRT should take precedence."""
        names = ("script.txt", "transcript.txt", "script.srt", "captions.srt",
                 "subtitles.srt")
        return any((self.input_dir / name).is_file() for name in names)

    def _destination_for_upload(self, field_name, original_name):
        name = _safe_filename(original_name)
        ext = Path(name).suffix.lower()
        if field_name == "master":
            if ext not in VIDEO_EXTENSIONS:
                raise DashboardError("Master video must be MP4, MOV, MKV, WebM, or M4V.")
            return self.input_dir / ("master" + ext)
        if field_name == "narration":
            if ext not in AUDIO_EXTENSIONS:
                raise DashboardError("Narration must be a supported audio file such as AAC, M4A, MP3, or WAV.")
            return self.input_dir / ("narration" + ext)
        if field_name == "music":
            if ext not in AUDIO_EXTENSIONS:
                raise DashboardError("Music must be a supported audio file such as AAC, M4A, MP3, or WAV.")
            return self.music_dir / ("background" + ext)
        if field_name == "transcript":
            if ext not in TEXT_EXTENSIONS:
                raise DashboardError("Transcript must be a .txt or time-coded .srt file.")
            return self.input_dir / ("script.srt" if ext == ".srt" else "transcript.txt")
        if field_name == "clips":
            if ext not in VIDEO_EXTENSIONS:
                raise DashboardError("Each source clip must be MP4, MOV, MKV, WebM, or M4V.")
            return self.clips_dir / name
        raise DashboardError("Unexpected upload field: %s" % field_name)

    def save_upload(self, field_name, original_name, source, content_length,
                    replace_clips=False):
        """Stream one raw browser upload directly to its safe workspace role.

        One-file requests avoid loading a long MP4 into memory and avoid the
        deprecated ``cgi`` multipart module (which is absent in newer Python
        versions). The browser uploads multiple selected clips sequentially.
        """
        if field_name not in {"master", "narration", "music", "transcript", "clips"}:
            raise DashboardError("Unexpected upload field: %s" % field_name)
        try:
            content_length = int(content_length)
        except (TypeError, ValueError):
            raise DashboardError("Upload has an invalid content length.")
        if content_length <= 0:
            raise DashboardError("The selected file is empty.")
        if content_length > MAX_UPLOAD_BYTES:
            raise DashboardError("Upload exceeds the %s dashboard limit."
                                 % _human_bytes(MAX_UPLOAD_BYTES))

        # Validate before replacing existing canonical media. Existing input is
        # removed only after the complete new file has safely reached a temp
        # file, so a dropped long upload cannot destroy the prior master.
        destination = self._destination_for_upload(field_name, original_name)
        with self._lock:
            if field_name == "clips" and destination.exists() and not replace_clips:
                stem, suffix = destination.stem, destination.suffix
                number = 2
                while destination.exists():
                    destination = self.clips_dir / ("%s_%d%s" %
                                                    (stem, number, suffix))
                    number += 1
            temporary = destination.with_name(destination.name + ".uploading")
            remaining = content_length
            written = 0
            try:
                with temporary.open("wb") as target:
                    while remaining:
                        chunk = source.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise DashboardError("Upload ended before the complete file was received.")
                        target.write(chunk)
                        written += len(chunk)
                        remaining -= len(chunk)

                if field_name == "master":
                    self._remove_named_media(self.input_dir, "master", VIDEO_EXTENSIONS)
                elif field_name == "narration":
                    self._remove_named_media(self.input_dir, "narration", AUDIO_EXTENSIONS)
                elif field_name == "music":
                    self._remove_named_media(self.music_dir, "background", AUDIO_EXTENSIONS)
                elif field_name == "transcript":
                    for candidate in (self.input_dir / "transcript.txt",
                                      self.input_dir / "script.srt"):
                        if candidate.is_file():
                            candidate.unlink()
                elif field_name == "clips" and replace_clips:
                    self._clear_clip_media()
                if field_name in {"master", "narration", "transcript"} or (
                        field_name == "clips" and replace_clips):
                    self._clear_dashboard_transcription()
                os.replace(temporary, destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return {
            "field": field_name,
            "name": destination.name,
            "size_bytes": written,
        }

    # ------------------------------------------------------------------
    # Render jobs
    # ------------------------------------------------------------------
    def _input_paths_for_workflow(self, workflow):
        definition = WORKFLOWS.get(workflow)
        if not definition:
            raise DashboardError("Choose a valid finishing workflow.")
        master = _find_master(self.input_dir)
        narration = find_narration(str(self.input_dir), required=False)
        music = find_music(str(self.music_dir), required=False)
        clips = [path for path in self.clips_dir.iterdir()
                 if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]

        if definition["engine"] == "master":
            if not master:
                raise DashboardError("Upload a finished master video first.")
            if definition["audio_mode"] in {"replace", "rebuild"} and not narration:
                raise DashboardError("This workflow requires a clean narration file.")
            if definition["audio_mode"] == "rebuild" and not music:
                raise DashboardError("Rebuild mode requires a separate music stem.")
        else:
            if not clips:
                raise DashboardError("Upload one or more source clips first.")
            if not narration:
                raise DashboardError("Source-clip workflows require a narration file.")
            if workflow == "clips-music" and not music:
                raise DashboardError("This workflow requires a separate music stem.")
        return master, narration, music, clips

    def _write_session_config(self, workflow, settings):
        config, warnings = build_dashboard_config(self.repo_root, workflow, settings)
        filename = "dashboard-%s.json" % uuid.uuid4().hex
        path = self.temp_dir / filename
        with path.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
        return path, warnings

    def _append_log(self, job_id, line):
        with self._lock:
            if self._job.get("id") != job_id:
                return
            self._job["logs"].append({"at": time.time(), "line": line.rstrip("\n")})
            # Browser logs should stay responsive on a long render.
            if len(self._job["logs"]) > 4000:
                self._job["logs"] = self._job["logs"][-4000:]

    def _set_stage(self, job_id, stage):
        with self._lock:
            if self._job.get("id") == job_id:
                self._job["stage"] = stage

    def _cancel_requested(self, job_id):
        with self._lock:
            return bool(self._job.get("id") == job_id and
                        self._job.get("cancel_requested"))

    def _transcription_request(self, workflow, settings, master, narration):
        """Build an optional local-Whisper preflight request for one job."""
        if not _as_bool(settings.get("autoTranscript"), False):
            return None
        if self._has_user_caption_source():
            return {"reuse": True, "reason": "Using your uploaded transcript or SRT."}
        if self.dashboard_transcript.is_file() and self.dashboard_captions.is_file():
            return {"reuse": True, "reason": "Reusing the existing local Whisper draft."}

        source = Path(narration) if narration else (Path(master) if master else None)
        if source is None or not source.is_file():
            raise DashboardError(
                "Automatic captions need a narration file or a master video with audio.")
        model = str(settings.get("transcriptionModel", "base") or "base").lower()
        if model not in {"tiny", "base", "small", "medium", "large-v3"}:
            raise DashboardError("Choose a supported local Whisper model.")
        available, message = local_whisper_status()
        if not available:
            raise DashboardError(message)
        return {
            "reuse": False,
            "source": str(source),
            "srt_path": str(self.dashboard_captions),
            "transcript_path": str(self.dashboard_transcript),
            "model": model,
        }

    def _run_transcription(self, job_id, request):
        if request.get("reuse"):
            self._append_log(job_id, "[transcription] " + request["reason"])
            return True
        self._set_stage(job_id, "Generating local Whisper captions")
        self._append_log(job_id, "[transcription] Starting local Whisper from %s."
                         % os.path.basename(request["source"]))

        def progress(message):
            self._append_log(job_id, "[transcription] " + message)

        result = transcribe_to_srt(
            request["source"], request["srt_path"], request["transcript_path"],
            model_size=request["model"], max_chars_per_line=42, max_lines=2,
            progress_callback=progress)
        self._append_log(job_id, "[transcription] Draft captions are ready for review: %d cue(s)."
                         % result["cue_count"])
        return True

    def _finish_cancelled_before_editor(self, job_id):
        with self._lock:
            if self._job.get("id") == job_id:
                self._job["status"] = "cancelled"
                self._job["finished_at"] = time.time()
                self._job["stage"] = "Cancelled"

    def _run_job(self, job_id, command, transcription_request=None):
        try:
            if transcription_request:
                self._run_transcription(job_id, transcription_request)
            if self._cancel_requested(job_id):
                self._finish_cancelled_before_editor(job_id)
                return
        except EditorError as exc:
            with self._lock:
                if self._job.get("id") == job_id:
                    self._job["status"] = "failed"
                    self._job["error"] = exc.message
                    self._job["finished_at"] = time.time()
                    self._job["stage"] = "Caption generation failed"
            self._append_log(job_id, "[transcription] ERROR: " + exc.message)
            return
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            with self._lock:
                if self._job.get("id") == job_id:
                    self._job["status"] = "failed"
                    self._job["error"] = "Caption generation failed: %s" % exc
                    self._job["finished_at"] = time.time()
                    self._job["stage"] = "Caption generation failed"
            self._append_log(job_id, "[transcription] ERROR: %s" % exc)
            return

        self._set_stage(job_id, "Launching editor")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        popen_kwargs = {
            "cwd": str(self.repo_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
            "env": env,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            with self._lock:
                action = self._job.get("action") if self._job.get("id") == job_id else None
            self._set_stage(job_id, "Running dry check" if action == "dry-run" else "Rendering final video")
            process = subprocess.Popen(command, **popen_kwargs)
            with self._lock:
                if self._job.get("id") == job_id:
                    self._job["process"] = process
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                self._append_log(job_id, line)
            returncode = process.wait()
            with self._lock:
                if self._job.get("id") != job_id:
                    return
                self._job["returncode"] = returncode
                self._job["finished_at"] = time.time()
                self._job["process"] = None
                if self._job.get("cancel_requested"):
                    self._job["status"] = "cancelled"
                    self._job["stage"] = "Cancelled"
                elif returncode == 0:
                    self._job["status"] = "succeeded"
                    self._job["stage"] = "Finished"
                    self._job["output_files"] = self.output_files()
                else:
                    self._job["status"] = "failed"
                    self._job["stage"] = "Editor failed"
                    self._job["error"] = "The editor exited with code %s." % returncode
        except Exception as exc:  # pragma: no cover - process failures are OS-specific
            with self._lock:
                if self._job.get("id") == job_id:
                    self._job["status"] = "failed"
                    self._job["error"] = "Could not start the editor: %s" % exc
                    self._job["finished_at"] = time.time()
                    self._job["process"] = None
                    self._job["stage"] = "Editor failed to start"

    def start_job(self, action, workflow, settings):
        if action not in {"dry-run", "render"}:
            raise DashboardError("Choose either a dry run or final render.")
        health = self.health()
        if not health["ffmpeg_available"]:
            raise DashboardError(health["message"] or "FFmpeg is required to render.")
        master, narration, _music, _clips = self._input_paths_for_workflow(workflow)
        transcription_request = self._transcription_request(
            workflow, settings, master, narration)
        config_path, warnings = self._write_session_config(workflow, settings)
        definition = WORKFLOWS[workflow]
        command = [sys.executable, str(self.repo_root / "editor.py"),
                   "--config", str(config_path)]
        if definition["engine"] == "master":
            command += ["--master", str(master), "--master-audio-mode",
                        definition["audio_mode"]]
        if action == "dry-run":
            command.append("--dry-run")

        with self._lock:
            if self._job.get("status") == "running":
                raise DashboardError("A render is already running. Cancel or wait for it first.")
            job_id = uuid.uuid4().hex
            self._job = {
                "id": job_id,
                "status": "running",
                "action": action,
                "workflow": workflow,
                "started_at": time.time(),
                "finished_at": None,
                "stage": ("Preparing local caption draft" if transcription_request
                          else "Preparing editor"),
                "logs": [],
                "returncode": None,
                "error": None,
                "warnings": warnings,
                "output_files": [],
                "process": None,
                "cancel_requested": False,
            }
            for warning in warnings:
                self._append_log(job_id, "[dashboard] WARNING: " + warning)
            self._append_log(job_id, "[dashboard] %s started." %
                             ("Dry run" if action == "dry-run" else "Final render"))
            worker = threading.Thread(target=self._run_job,
                                      args=(job_id, command, transcription_request),
                                      daemon=True)
            worker.start()
        return self.job_snapshot(0)

    def cancel_job(self):
        with self._lock:
            if self._job.get("status") != "running":
                raise DashboardError("There is no active render to cancel.")
            process = self._job.get("process")
            self._job["cancel_requested"] = True
            self._append_log(self._job["id"], "[dashboard] Cancellation requested.")
        if process and process.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except OSError:
                pass
        return self.job_snapshot(0)

    def job_snapshot(self, cursor=0):
        try:
            cursor = max(0, int(cursor))
        except (TypeError, ValueError):
            cursor = 0
        with self._lock:
            job = self._job
            logs = job.get("logs", [])
            visible = logs[cursor:]
            return {
                "id": job.get("id"),
                "status": job.get("status"),
                "action": job.get("action"),
                "workflow": job.get("workflow"),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "stage": job.get("stage"),
                "returncode": job.get("returncode"),
                "error": job.get("error"),
                "warnings": list(job.get("warnings") or []),
                "logs": visible,
                "next_cursor": len(logs),
                "output_files": list(job.get("output_files") or []),
            }

    # ------------------------------------------------------------------
    # Output delivery
    # ------------------------------------------------------------------
    def output_files(self):
        files = []
        for path in sorted(self.output_dir.rglob("*"), key=lambda item: str(item).lower()):
            if (not path.is_file() or path.name.startswith(".") or
                    not _is_within(path, self.output_dir)):
                continue
            relative = str(path.relative_to(self.output_dir)).replace(os.sep, "/")
            stat = path.stat()
            extension = path.suffix.lower()
            files.append({
                "name": relative,
                "size_bytes": stat.st_size,
                "modified_at": int(stat.st_mtime),
                "kind": ("video" if extension in VIDEO_EXTENSIONS else
                         "subtitle" if extension == ".srt" else
                         "report" if extension == ".json" else "file"),
            })
        return files

    def output_path(self, relative_name):
        relative_name = unquote(str(relative_name or ""))
        candidate = (self.output_dir / relative_name).resolve()
        if not _is_within(candidate, self.output_dir) or not candidate.is_file():
            raise DashboardError("Requested output file was not found.")
        return candidate


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP API plus static-file handler. ``state`` is injected by factory."""

    protocol_version = "HTTP/1.1"
    state = None

    def log_message(self, fmt, *args):  # concise local server diagnostics
        sys.stdout.write("[dashboard] %s\n" % (fmt % args))

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=HTTPStatus.BAD_REQUEST):
        self._send_json({"ok": False, "error": str(message)}, status)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise DashboardError("Invalid request length.")
        if length <= 0 or length > JSON_LIMIT_BYTES:
            raise DashboardError("Invalid JSON request size.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DashboardError("Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise DashboardError("Request body must be a JSON object.")
        return data

    def _upload_request(self, parsed):
        """Validate one streaming raw-file upload described by query fields."""
        query = parse_qs(parsed.query)
        field_name = query.get("field", [""])[0]
        original_name = unquote(query.get("name", [""])[0])
        replace_clips = _as_bool(query.get("replaceClips", ["false"])[0])
        final = _as_bool(query.get("final", ["false"])[0])
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise DashboardError("Invalid upload length.")
        if length <= 0:
            raise DashboardError("Choose a non-empty file to upload.")
        if length > MAX_UPLOAD_BYTES:
            raise DashboardError("Upload exceeds the %s dashboard limit."
                                 % _human_bytes(MAX_UPLOAD_BYTES))
        return field_name, original_name, length, replace_clips, final

    def _serve_static(self, request_path):
        if request_path == "/":
            path = self.state.static_dir / "index.html"
        elif request_path.startswith("/static/"):
            path = (self.state.static_dir / request_path[len("/static/"):]).resolve()
            if not _is_within(path, self.state.static_dir):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8"
                         if content_type.startswith("text/") or content_type == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self';")
        self.end_headers()
        self.wfile.write(body)

    def _serve_output(self, relative_name):
        try:
            path = self.state.output_path(relative_name)
        except DashboardError as exc:
            self._error(exc, HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        start, end = 0, max(0, size - 1)
        range_header = self.headers.get("Range")
        status = HTTPStatus.OK
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if match:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = int(last) if last else end
                elif last:
                    suffix = int(last)
                    start = max(0, size - suffix)
                if start >= size or end < start:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        safe_name = path.name.replace('"', "")
        self.send_header("Content-Disposition", 'inline; filename="%s"' % safe_name)
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._send_json({"ok": True, "health": self.state.health()})
                return
            if parsed.path == "/api/media":
                self._send_json({"ok": True, "media": self.state.media_summary()})
                return
            if parsed.path == "/api/job":
                cursor = parse_qs(parsed.query).get("cursor", [0])[0]
                self._send_json({"ok": True, "job": self.state.job_snapshot(cursor)})
                return
            if parsed.path == "/api/results":
                self._send_json({"ok": True, "files": self.state.output_files()})
                return
            if parsed.path.startswith("/api/files/"):
                self._serve_output(parsed.path[len("/api/files/"):])
                return
            self._serve_static(parsed.path)
        except DashboardError as exc:
            self._error(exc)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._error("Dashboard error: %s" % exc, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/upload":
                field_name, original_name, length, replace, final = self._upload_request(parsed)
                saved = self.state.save_upload(
                    field_name, original_name, self.rfile, length,
                    replace_clips=replace)
                payload = {"ok": True, "saved": saved}
                # Scanning a 70-clip batch after every single browser upload is
                # wasteful; the final upload returns the complete inspection.
                if final:
                    payload["media"] = self.state.media_summary()
                self._send_json(payload)
                return
            if parsed.path == "/api/job":
                data = self._read_json()
                job = self.state.start_job(data.get("action"), data.get("workflow"),
                                           data.get("settings") or {})
                self._send_json({"ok": True, "job": job}, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/job/cancel":
                job = self.state.cancel_job()
                self._send_json({"ok": True, "job": job})
                return
            self._error("Unknown API endpoint.", HTTPStatus.NOT_FOUND)
        except DashboardError as exc:
            self._error(exc)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._error("Dashboard error: %s" % exc, HTTPStatus.INTERNAL_SERVER_ERROR)


def handler_for(state):
    """Return a handler class bound to one dashboard workspace state."""

    class BoundDashboardHandler(DashboardHandler):
        pass

    BoundDashboardHandler.state = state
    return BoundDashboardHandler


def create_server(repo_root, host="127.0.0.1", port=8765):
    state = DashboardState(repo_root)
    if not state.static_dir.is_dir():
        raise DashboardError("Dashboard static files are missing: %s" % state.static_dir)
    server = ThreadingHTTPServer((host, int(port)), handler_for(state))
    server.daemon_threads = True
    return server, state


def serve(repo_root, host="127.0.0.1", port=8765):
    """Run the local dashboard until interrupted."""
    server, _state = create_server(repo_root, host=host, port=port)
    actual_host, actual_port = server.server_address[:2]
    print("Veo Documentary Dashboard")
    print("Open http://%s:%s" % (actual_host, actual_port))
    if actual_host not in {"127.0.0.1", "::1", "localhost"}:
        print("WARNING: This dashboard has no login. Keep it on a trusted network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
