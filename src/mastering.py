"""Safe finishing of an already edited video master.

This workflow is intentionally distinct from the clip-assembly renderer:

* It preserves the existing visual edit instead of cutting it into new shots.
* It makes the audio topology explicit, so a clean ElevenLabs narration is
  never accidentally mixed on top of the same narration already baked into a
  CapCut export.
* It creates an upload-ready H.264/AAC master and an optional SRT sidecar.

It is appropriate for a CapCut/Premiere/DaVinci export.  It cannot recover or
change individual transitions that are already baked into that export.
"""

import json
import os
import re
import shutil
import subprocess
import time

from . import audio as audio_mod
from .errors import ConfigurationError, MediaNotFoundError, RenderError
from .script import segment_scenes
from .subtitles import write_srt
from .timeline import build_subtitle_cues


AUDIO_MODES = {"preserve", "replace", "rebuild"}


def _run_ffmpeg(cmd, what):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RenderError("ffmpeg binary not found while trying to %s." % what)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RenderError(
            "FFmpeg failed while %s.\n--- ffmpeg stderr ---\n%s" %
            (what, tail))
    return proc


def _fmt_duration(seconds):
    seconds = max(0.0, float(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return "%d:%02d:%05.2f" % (hours, minutes, seconds)
    return "%d:%05.2f" % (minutes, seconds)


def _srt_end_seconds(text):
    """Return the final end timestamp in an SRT, or ``None`` if unavailable."""
    matches = re.findall(
        r"-->\s*(\d{1,2}):(\d{2}):(\d{2}),(\d{3})", text)
    if not matches:
        return None
    h, m, s, ms = matches[-1]
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


class MasterFinisher:
    """Inspect and finish a single, already edited video file."""

    def __init__(self, cfg, ffmpeg_bin, prober):
        self.cfg = cfg
        self.ffmpeg = ffmpeg_bin
        self.prober = prober

    def prepare(self, master_path, audio_mode=None, narration_path=None,
                music_path=None):
        """Validate media topology and return a render plan.

        ``preserve`` keeps and masters the audio already inside the video.
        ``replace`` strips that audio and uses only the clean narration.
        ``rebuild`` strips it and mixes the clean narration with an external
        music stem.  The last two modes deliberately never retain the master
        audio because it may contain a duplicate baked narration.
        """
        if not master_path or not os.path.isfile(master_path):
            raise MediaNotFoundError(
                "Master video not found: %r" % master_path,
                hint="Pass an existing export to --master, for example "
                     "--master input/master.mp4.")
        master_path = os.path.abspath(master_path)
        mode = audio_mode or self.cfg.get("master_audio_mode", "preserve")
        if mode not in AUDIO_MODES:
            raise ConfigurationError(
                "master audio mode must be one of %s, got %r"
                % (sorted(AUDIO_MODES), mode))

        master = self.prober.video(master_path)
        duration = float(master.get("duration") or 0)
        if duration <= 0:
            raise ConfigurationError(
                "Master video has an invalid duration: %r" % master_path)

        master_audio = None
        if master.get("has_audio"):
            master_audio = self.prober.audio(master_path)
        if mode == "preserve" and not master_audio:
            raise ConfigurationError(
                "master_audio_mode='preserve' requires an audio stream in the "
                "master video.",
                hint="Use 'replace' with a narration file, or provide an export "
                     "that includes its audio.")

        narration = None
        if narration_path:
            if not os.path.isfile(narration_path):
                raise MediaNotFoundError("Narration file not found: %r" %
                                         narration_path)
            narration_path = os.path.abspath(narration_path)
            narration = self.prober.audio(narration_path)
        if mode in {"replace", "rebuild"} and narration is None:
            raise MediaNotFoundError(
                "A clean narration file is required for master audio mode %r."
                % mode,
                hint="Place narration.aac (or .mp3/.m4a/.wav) in input/ or set "
                     "narration_path in config.")

        music = None
        if music_path:
            if not os.path.isfile(music_path):
                raise MediaNotFoundError("Music file not found: %r" % music_path)
            music_path = os.path.abspath(music_path)
            music = self.prober.audio(music_path)
        if mode == "rebuild" and music is None:
            raise MediaNotFoundError(
                "master_audio_mode='rebuild' requires a separate music stem.",
                hint="Put background music in music/background.* or set "
                     "music_path in config. Do not use a CapCut master with a "
                     "baked narration as the music stem.")

        warnings = []
        tolerance = float(self.cfg.get("master_sync_tolerance_seconds", 0.35))
        if narration is not None:
            delta = float(narration["duration"]) - duration
            if abs(delta) > tolerance:
                warnings.append(
                    "Narration and master durations differ by %.3fs "
                    "(master %s; narration %s). No automatic timing shift was "
                    "applied; review sync before publishing."
                    % (delta, _fmt_duration(duration),
                       _fmt_duration(narration["duration"])))
        else:
            delta = None

        if mode == "preserve" and narration is not None:
            warnings.append(
                "The separate narration is used only as a sync/reference track. "
                "It is not mixed into the preserved CapCut audio, preventing "
                "duplicate/echoed narration.")
        if mode in {"replace", "rebuild"} and master_audio:
            warnings.append(
                "The master export's embedded audio will be discarded in %s "
                "mode. This avoids doubled narration but also removes any "
                "baked music/ambience from that export."
                % mode)
        if mode == "replace":
            warnings.append(
                "Replace mode outputs narration only. Use rebuild mode with a "
                "licensed external music stem if a background bed is required.")

        return {
            "workflow": "master",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "master_path": master_path,
            "master": master,
            "master_audio": master_audio,
            "duration": round(duration, 3),
            "audio_mode": mode,
            "narration_path": narration_path,
            "narration": narration,
            "narration_delta_seconds": round(delta, 3) if delta is not None else None,
            "music_path": music_path,
            "music": music,
            "warnings": warnings,
        }

    def _audio_filters(self, plan, fade=0.0):
        """Return filter sections which finish the selected audio topology."""
        mode = plan["audio_mode"]
        duration = plan["duration"]
        sr = int(self.cfg.get("sample_rate", 48000))
        lufs = float(self.cfg.get("loudness_target_lufs", -14.0))
        tp = float(self.cfg.get("loudness_target_tp", -1.5))
        lra = float(self.cfg.get("loudness_lra", 11.0))
        filters = []

        if mode == "preserve":
            filters.append("[0:a]aresample=%d[pre]" % sr)
        elif mode == "replace":
            filters.append("[1:a]aresample=%d[pre]" % sr)
        else:  # rebuild: input 1 is narration, input 2 is looping music.
            filters.append("[1:a]aresample=%d,asplit=2[nar][nar_key]" % sr)
            filters.append(audio_mod.music_chain(
                "2:a", "mus", duration, self.cfg, "nar_key"))
            filters.append("[nar][mus]amix=inputs=2:normalize=0:"
                           "dropout_transition=3[pre]")

        # Apply final loudness processing after all sources are mixed. This is
        # the target for the delivery master, not merely the narration stem.
        final_chain = ("[pre]loudnorm=I=%g:TP=%g:LRA=%g,aresample=%d,"
                       "alimiter=limit=0.98:level=false"
                       % (lufs, tp, lra, sr))
        if fade > 0:
            fade_out_start = max(0.0, duration - fade)
            final_chain += (",afade=t=in:st=0:d=%g,afade=t=out:st=%g:d=%g"
                            % (fade, fade_out_start, fade))
        final_chain += (",apad=whole_dur=%g,atrim=start=0:end=%g[aout]"
                        % (duration, duration))
        filters.append(final_chain)
        return filters

    def build_command(self, plan, output_path):
        """Build the FFmpeg command for a prepared plan (useful for dry-runs)."""
        mode = plan["audio_mode"]
        duration = plan["duration"]
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-i", plan["master_path"]]
        if mode in {"replace", "rebuild"}:
            cmd += ["-i", plan["narration_path"]]
        if mode == "rebuild":
            cmd += ["-stream_loop", "-1", "-i", plan["music_path"]]

        fade = min(float(self.cfg.get("master_fade_seconds", 0.0)),
                   duration / 2.0)
        filters = self._audio_filters(plan, fade=fade)
        if fade > 0:
            fade_out_start = max(0.0, duration - fade)
            filters.insert(0, "[0:v]fade=t=in:st=0:d=%g,fade=t=out:st=%g:d=%g"
                            "[vout]" % (fade, fade_out_start, fade))

        cmd += ["-filter_complex", ";".join(filters)]
        if fade > 0:
            cmd += ["-map", "[vout]", "-c:v", self.cfg.get("video_codec", "libx264"),
                    "-preset", self.cfg.get("preset", "medium"), "-crf",
                    str(self.cfg.get("crf", 18)), "-pix_fmt", "yuv420p"]
        else:
            # Re-encoding video solely for audio mastering is needless quality
            # loss. Stream-copy the already edited visual master instead.
            cmd += ["-map", "0:v:0", "-c:v", "copy"]

        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a",
                str(self.cfg.get("aac_bitrate", 192)) + "k", "-ar",
                str(self.cfg.get("sample_rate", 48000)), "-ac", "2"]
        if self.cfg.get("faststart", True):
            cmd += ["-movflags", "+faststart"]
        cmd += [output_path]
        return cmd

    def finish(self, plan, output_path):
        """Run FFmpeg and return final output metadata."""
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        cmd = self.build_command(plan, output_path)
        started = time.time()
        _run_ffmpeg(cmd, "finishing existing master video")
        elapsed = time.time() - started
        output = self.prober.video(output_path)
        output_audio = self.prober.audio(output_path) if output.get("has_audio") else None
        return {
            "path": output_path,
            "file": os.path.basename(output_path),
            "size_bytes": os.path.getsize(output_path),
            "video": output,
            "audio": output_audio,
            "render_time_seconds": round(elapsed, 3),
        }


def write_master_subtitles(script_path, output_path, cfg, duration):
    """Create/copy an SRT sidecar for a master video.

    A supplied ``.srt`` is treated as authoritative and copied unchanged.
    A plain final script/transcript is rendered with proportional timing and
    returns an explicit warning so it receives a human review before upload.
    """
    if not script_path or not os.path.isfile(script_path):
        return None, []
    output_path = os.path.abspath(output_path)
    warnings = []
    if script_path.lower().endswith(".srt"):
        shutil.copyfile(script_path, output_path)
        try:
            with open(script_path, "r", encoding="utf-8-sig") as fh:
                end = _srt_end_seconds(fh.read())
            if end is not None and end > float(duration) + 0.25:
                warnings.append(
                    "The supplied SRT ends at %.3fs but the master is %.3fs; "
                    "review the final caption cue." % (end, duration))
        except OSError:
            pass
        return {"path": output_path, "source": "provided_srt", "cue_count": None}, warnings

    with open(script_path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    scenes = segment_scenes(text)
    cues = build_subtitle_cues(cfg, scenes, duration)
    if not cues:
        return None, ["Transcript has no subtitle text; no SRT was written."]
    write_srt(cues, output_path)
    warnings.append(
        "SRT timings were proportionally estimated from the plain transcript. "
        "Review caption timing against the final narration before publishing.")
    return {"path": output_path, "source": "proportional_transcript",
            "cue_count": len(cues)}, warnings


def write_master_report(plan, output, subtitles, path):
    """Write a concise provenance and QC report for the finishing workflow."""
    report = dict(plan)
    report["output"] = output
    report["subtitles"] = subtitles
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    return path
