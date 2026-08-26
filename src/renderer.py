"""FFmpeg execution layer (EXECUTION).

This module turns a validated timeline into FFmpeg commands. It performs no
creative decisions of its own — it only *renders* the plan produced by the
timeline/matcher layers.

Render pipeline (multi-pass, resumable via done-files / output existence):

  A. normalize_shots   -> temp/step1/shot_NNNN.mp4 (standard format)
  B. assemble video    -> temp/step1/main_video_raw.mp4  (cut or crossfade)
  C. mix audio         -> temp/step1/audio_mix.m4a       (narration + music)
  D. burn subtitles    -> temp/step1/main_video_burned.mp4 (optional)
  E. intro/outro       -> temp/step1/final_video.mp4       (optional)
  F. final mux+encode  -> output/final_documentary.mp4
"""

import hashlib
import json
import os
import subprocess
import time

from . import audio as audio_mod
from .errors import RenderError, DiskSpaceError

INTERMEDIATE_CRF = 16
INTERMEDIATE_PRESET = "medium"


def _run_ffmpeg(cmd, what):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RenderError("ffmpeg binary not found while trying to %s." % what)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RenderError(
            "FFmpeg failed while %s.\n--- ffmpeg stderr ---\n%s" % (what, tail))
    return proc


def _content_key(*parts):
    return hashlib.md5(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _needs_skip(marker_path, key):
    if not os.path.isfile(marker_path):
        return False
    try:
        with open(marker_path, "r", encoding="utf-8") as fh:
            return fh.read().strip() == key
    except OSError:
        return False


def _write_marker(marker_path, key):
    with open(marker_path, "w", encoding="utf-8") as fh:
        fh.write(key)


def _check_disk(output_dir, min_bytes=60 * 1024 * 1024):
    try:
        st = os.statvfs(output_dir)
        free = st.f_bavail * st.f_frsize
    except OSError:
        return
    if free < min_bytes:
        raise DiskSpaceError(
            "Insufficient free disk space: only %.1f MB available in %r "
            "(need at least %.0f MB)." % (
                free / (1024 * 1024), output_dir, min_bytes / (1024 * 1024)))


class Renderer:
    def __init__(self, cfg, paths, ffmpeg_bin, prober, mode="full"):
        self.cfg = cfg
        self.paths = paths
        self.ffmpeg = ffmpeg_bin
        self.prober = prober
        self.mode = mode  # "full" | "preview"
        self.step1 = os.path.join(paths["temp_dir"], "step1")
        os.makedirs(self.step1, exist_ok=True)

        if mode == "preview":
            self.W = cfg.get("preview_width", 640)
            self.H = cfg.get("preview_height", 360)
            self.crf = cfg.get("preview_crf", 28)
            self.preset = cfg.get("preview_preset", "veryfast")
        else:
            self.W = cfg["width"]
            self.H = cfg["height"]
            self.crf = cfg["crf"]
            self.preset = cfg["preset"]
        self.FPS = cfg["fps"]

        self.crossfade = cfg.get("transition", "cut") == "crossfade"
        self.d = cfg.get("crossfade_seconds", 0.3) if self.crossfade else 0.0
        if self.crossfade:
            self.d = min(self.d, cfg.get("min_clip_seconds", 2) * 0.5)
        self.clip_audio = cfg.get("clip_audio_enabled", False)
        self._warnings = []
        self._log = []
        if self.crossfade and self.clip_audio:
            self._warnings.append(
                "clip_audio_enabled is not retained during crossfade assembly; "
                "use transition='cut' to keep embedded clip audio, or supply "
                "a separate music stem for crossfades.")
        from .media import has_filter
        self.has_drawtext = has_filter(ffmpeg_bin, "drawtext")
        self.has_subtitles = has_filter(ffmpeg_bin, "subtitles")

    def warnings(self):
        return list(self._warnings)

    # ------------------------------------------------------------------
    # A. Normalization
    # ------------------------------------------------------------------
    def _vf_string(self):
        w, h, fps = self.W, self.H, self.FPS
        if self.cfg.get("fit", "pad") == "crop":
            return ("scale=%d:%d:force_original_aspect_ratio=increase,"
                    "crop=%d:%d,fps=%d,format=yuv420p" % (w, h, w, h, fps))
        return ("scale=%d:%d:force_original_aspect_ratio=decrease,"
                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,fps=%d,"
                "format=yuv420p" % (w, h, w, h, fps))

    def _can_stream_copy(self, clip, shot, length):
        if shot.get("loop", 0):
            return False
        if shot.get("src_start", 0.0):
            return False
        if abs(clip["duration"] - length) > 0.05:
            return False
        if clip["width"] != self.W or clip["height"] != self.H:
            return False
        if abs(clip["fps"] - self.FPS) > 0.6:
            return False
        if clip["codec"] not in ("h264", "libx264", "avc1"):
            return False
        return True

    def normalize_shots(self, shots, clips_by_name):
        """Render each shot to a standard-format intermediate file.

        In crossfade mode every shot except the last is extended by ``d``
        seconds so the chained-xfade total still equals the narration length.
        """
        n = len(shots)
        vf = self._vf_string()
        out_paths = []
        for i, shot in enumerate(shots):
            clip = clips_by_name[shot["clip"]]
            length = shot["length"]
            if self.crossfade and i < n - 1:
                length += self.d
            length = round(length, 3)
            out = os.path.join(self.step1, "shot_%04d.mp4" % i)
            out_paths.append(out)

            key = _content_key(
                clip["path"], os.path.getmtime(clip["path"]),
                shot["src_start"], length, shot.get("loop"), vf, self.W,
                self.H, self.FPS, self.mode)
            marker = out + ".done"
            if (not self.cfg.get("force", False)
                    and _needs_skip(marker, key)):
                self._log.append("reuse shot %04d (%s)" % (i, shot["clip"]))
                print("  Render progress: shot %d/%d reused (%s)" %
                      (i + 1, n, shot["clip"]), flush=True)
                continue

            _check_disk(self.step1)
            self._render_shot(shot, clip, length, vf, out)
            _write_marker(marker, key)
            print("  Render progress: shot %d/%d complete (%s)" %
                  (i + 1, n, shot["clip"]), flush=True)
        return out_paths

    def _render_shot(self, shot, clip, length, vf, out):
        loop = shot.get("loop", 0)
        src_start = shot.get("src_start", 0.0)
        clip_path = clip["path"]
        clip_has_audio = clip["has_audio"]

        if (not self.cfg.get("force", False)
                and self._can_stream_copy(clip, shot, length)):
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-i", clip_path, "-c:v", "copy"]
            if self.clip_audio and clip_has_audio:
                cmd += ["-c:a", "aac", "-ar", "48000", "-ac", "2"]
            else:
                cmd += ["-an"]
            cmd += [out]
        else:
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
            if loop > 0:
                cmd += ["-stream_loop", str(loop)]
            cmd += ["-i", clip_path]
            if src_start:
                cmd += ["-ss", "%.3f" % src_start]
            cmd += ["-t", "%.3f" % length, "-vf", vf,
                    "-c:v", "libx264", "-preset", INTERMEDIATE_PRESET,
                    "-crf", str(INTERMEDIATE_CRF), "-pix_fmt", "yuv420p"]
            if self.clip_audio and clip_has_audio:
                cmd += ["-c:a", "aac", "-ar", "48000", "-ac", "2"]
            else:
                cmd += ["-an"]
            cmd += [out]
        self._log.append("render shot %s (%s)" % (os.path.basename(out),
                                                  shot["clip"]))
        _run_ffmpeg(cmd, "normalizing shot %s" % shot["clip"])

    # ------------------------------------------------------------------
    # B. Assembly
    # ------------------------------------------------------------------
    def assemble(self, shot_paths):
        """Combine normalized shots -> (main_video_raw, clip_bed_or_None)."""
        raw = os.path.join(self.step1, "main_video_raw.mp4")
        bed = os.path.join(self.step1, "clip_bed.m4a")
        if self.crossfade:
            return self._assemble_crossfade(shot_paths, raw), None
        return self._assemble_cut(shot_paths, raw, bed), bed

    def _assemble_cut(self, shot_paths, raw, bed):
        concat_list = os.path.join(self.step1, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as fh:
            for p in shot_paths:
                fh.write("file '%s'\n" % p.replace("'", r"'\''"))
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", concat_list,
               "-map", "0:v", "-c:v", "copy", raw]
        if self.clip_audio:
            cmd += ["-map", "0:a", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                    bed]
        _run_ffmpeg(cmd, "concatenating shots (cut assembly)")
        if self.clip_audio and not os.path.isfile(bed):
            self._silent_bed(bed)
        return raw

    def _silent_bed(self, bed):
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
               "-c:a", "aac", "-ar", "48000", "-ac", "2", bed]
        _run_ffmpeg(cmd, "creating silent clip bed")
        return bed

    def _assemble_crossfade(self, shot_paths, raw):
        n = len(shot_paths)
        d = self.d
        lengths = [self._probe_duration(p) for p in shot_paths]
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        for p in shot_paths:
            cmd += ["-i", p]
        fc = []
        prev_label = None
        for i in range(1, n):
            srcs = ("[%d:v]" % (i - 1)) if prev_label is None else "[%s]" % prev_label
            offset = sum(lengths[:i]) - i * d
            cur = "v%d" % i
            fc.append("%s[%d:v]xfade=transition=fade:duration=%g:offset=%g[%s]"
                      % (srcs, i, d, offset, cur))
            prev_label = cur
        fc.append("[%s]fps=%d,format=yuv420p[vout]" % (prev_label, self.FPS))
        cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]",
                "-c:v", "libx264", "-preset", self.preset, "-crf",
                str(self.crf), "-pix_fmt", "yuv420p", raw]
        _run_ffmpeg(cmd, "crossfade assembly")
        return raw

    def _probe_duration(self, path):
        return self.prober.video(path)["duration"]

    # ------------------------------------------------------------------
    # C. Audio mix
    # ------------------------------------------------------------------
    def mix_audio(self, narration_path, music_path, bed_path, narration_dur):
        out = os.path.join(self.step1, "audio_mix.m4a")
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-i", narration_path]
        # Inputs are optional and therefore do not have fixed FFmpeg indexes.
        # In particular, the embedded-bed profile deliberately has no external
        # music, making the clip bed input 1:a rather than 2:a.
        has_music = bool(self.cfg.get("music_enabled", True) and music_path
                         and os.path.isfile(music_path))
        input_index = 1
        music_input = None
        if has_music:
            music_input = "%d:a" % input_index
            cmd += ["-stream_loop", "-1", "-i", music_path]
            input_index += 1
        has_bed = bool(self.clip_audio and bed_path and os.path.isfile(bed_path))
        bed_input = None
        if has_bed:
            bed_input = "%d:a" % input_index
            cmd += ["-i", bed_path]
            input_index += 1

        sr = self.cfg.get("sample_rate", 48000)
        fc = [audio_mod.normalize_narration("0:a", "nar0", self.cfg)]
        music_duck = has_music and self.cfg.get("ducking_enabled", True)
        bed_duck = has_bed and self.cfg.get("clip_audio_ducking_enabled", True)
        duck_targets = int(bool(music_duck)) + int(bool(bed_duck))
        if duck_targets:
            # Each sidechain consumer needs its own narration copy. This lets
            # an external music stem and Veo clip-audio bed both duck cleanly.
            labels = ["nar"] + ["nk%d" % i for i in range(duck_targets)]
            fc.append("[nar0]asplit=%d%s" %
                      (len(labels), "".join("[%s]" % label for label in labels)))
        else:
            fc.append("[nar0]anull[nar]")
        mix_labels = ["nar"]
        key_index = 0
        if has_music:
            music_key = "nk%d" % key_index if music_duck else "nar"
            if music_duck:
                key_index += 1
            fc.append(audio_mod.music_chain(music_input, "mus", narration_dur,
                                            self.cfg, music_key))
            mix_labels.append("mus")
        if has_bed:
            bed_key = "nk%d" % key_index if bed_duck else "nar"
            fc.append(audio_mod.clip_bed_chain(bed_input, "bed", self.cfg,
                                               bed_key))
            mix_labels.append("bed")
        fc.append(audio_mod.final_mix(mix_labels, narration_dur, self.cfg))

        cmd += ["-filter_complex", ";".join(fc), "-map", "[aout]",
                "-c:a", "aac", "-b:a",
                str(self.cfg.get("aac_bitrate", 192)) + "k",
                "-ar", str(sr), "-ac", "2", out]
        _run_ffmpeg(cmd, "mixing narration and music")
        return out

    # ------------------------------------------------------------------
    # D. Subtitles burn-in
    # ------------------------------------------------------------------
    def burn_subtitles(self, main_video_raw, subtitle_path):
        if not (self.cfg.get("subtitle_enabled") and subtitle_path
                and os.path.isfile(subtitle_path)):
            return main_video_raw
        if not self.has_subtitles:
            self._warnings.append(
                "subtitle_burn_in is enabled but this FFmpeg build lacks the "
                "'subtitles' filter; burned-in subtitles were skipped "
                "(standalone subtitles.srt/.ass still written).")
            return main_video_raw
        out = os.path.join(self.step1, "main_video_burned.mp4")
        fs = self.cfg.get("subtitle_font_size", 24)
        margin = max(20, int(self.H * 0.06))
        style = ("Fontname=DejaVu Sans,Fontsize=%d,Outline=1,Shadow=1,"
                 "MarginV=%d" % (fs, margin))
        esc = subtitle_path.replace("\\", "\\\\").replace(":", "\\:")
        esc = esc.replace("'", r"\'")
        vf = "subtitles=%s:force_style='%s'" % (esc, style)
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-i", main_video_raw, "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", self.preset, "-crf",
               str(self.crf), "-pix_fmt", "yuv420p", out]
        _run_ffmpeg(cmd, "burning subtitles")
        return out

    # ------------------------------------------------------------------
    # E. Intro / outro
    # ------------------------------------------------------------------
    def _text_clip(self, text, path, duration):
        fs = max(30, self.W // 14)
        font = self.cfg.get("subtitle_font",
                            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        draw = ("drawtext=text='%s':fontfile=%s:fontsize=%d:fontcolor=white:"
                "x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.6:"
                "boxborderw=20" % (text.replace("'", r"\'"), font, fs))
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i",
               "color=c=black:s=%dx%d:r=%d:d=%g" % (self.W, self.H, self.FPS,
                                                    duration),
               "-vf", draw, "-an", "-c:v", "libx264", "-preset", self.preset,
               "-crf", str(self.crf), "-pix_fmt", "yuv420p", path]
        _run_ffmpeg(cmd, "generating %s" % os.path.basename(path))

    def apply_intro_outro(self, main_video_burned):
        intro = bool(self.cfg.get("intro_enabled")
                     and self.cfg.get("intro_duration_seconds", 3.0) > 0)
        outro = bool(self.cfg.get("outro_enabled")
                     and self.cfg.get("outro_duration_seconds", 3.0) > 0)
        if (intro or outro) and not self.has_drawtext:
            self._warnings.append(
                "intro/outro requested but this FFmpeg build lacks the "
                "'drawtext' filter; intro/outro were skipped.")
            return main_video_burned, 0.0, 0.0
        if not intro and not outro:
            return main_video_burned, 0.0, 0.0
        parts = []
        intro_dur = outro_dur = 0.0
        if intro:
            intro_dur = float(self.cfg.get("intro_duration_seconds", 3.0))
            ipath = os.path.join(self.step1, "intro.mp4")
            self._text_clip(self.cfg.get("intro_title", "VEO DOCUMENTARY"),
                            ipath, intro_dur)
            parts.append(ipath)
        parts.append(main_video_burned)
        if outro:
            outro_dur = float(self.cfg.get("outro_duration_seconds", 4.0))
            opath = os.path.join(self.step1, "outro.mp4")
            self._text_clip(self.cfg.get("outro_text", "Subscribe"), opath,
                            outro_dur)
            parts.append(opath)
        lst = os.path.join(self.step1, "intro_outro.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for p in parts:
                fh.write("file '%s'\n" % p.replace("'", r"'\''"))
        out = os.path.join(self.step1, "final_video.mp4")
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out]
        _run_ffmpeg(cmd, "assembling intro/outro")
        return out, intro_dur, outro_dur

    # ------------------------------------------------------------------
    # F. Final mux + encode
    # ------------------------------------------------------------------
    def finalize(self, final_video, audio_mix, output_path, intro_dur=0.0,
                 outro_dur=0.0):
        _check_disk(self.paths["output_dir"])
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-i", final_video, "-i", audio_mix]
        reencode_audio = False
        if intro_dur > 0 or outro_dur > 0:
            sr = self.cfg.get("sample_rate", 48000)
            ms = int(round(intro_dur * 1000))
            adelay = ",adelay=%d:all=1" % ms if ms else ""
            apad = ",apad=pad_dur=%g" % outro_dur if outro_dur else ""
            fc = "[1:a]aresample=%d%s%s[aout]" % (sr, adelay, apad)
            cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]"]
            reencode_audio = True
        else:
            cmd += ["-map", "0:v", "-map", "1:a"]
        cmd += ["-c:v", "copy"]
        cmd += ["-c:a", "aac", "-b:a", str(self.cfg.get("aac_bitrate", 192)) + "k"] \
            if reencode_audio else ["-c:a", "copy"]
        if self.cfg.get("faststart", True):
            cmd += ["-movflags", "+faststart"]
        cmd += [output_path]
        _run_ffmpeg(cmd, "writing final output")
        return output_path

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def render(self, timeline, clips_by_name, narration_path, music_path,
               subtitle_path, output_path):
        start = time.time()
        _check_disk(self.paths["output_dir"])

        shots = timeline["shots"]
        print("  Render stage: normalizing %d shot(s)…" % len(shots), flush=True)
        shot_paths = self.normalize_shots(shots, clips_by_name)
        print("  Render stage: assembling visual timeline…", flush=True)
        main_video_raw, bed = self.assemble(shot_paths)

        narration_dur = timeline["duration"]
        print("  Render stage: mixing and mastering audio…", flush=True)
        audio_mix = self.mix_audio(narration_path, music_path, bed,
                                   narration_dur)
        print("  Render stage: preparing subtitles…", flush=True)
        main_video_burned = self.burn_subtitles(main_video_raw, subtitle_path)
        print("  Render stage: applying final packaging…", flush=True)
        final_video, intro_dur, outro_dur = self.apply_intro_outro(
            main_video_burned)
        self.finalize(final_video, audio_mix, output_path, intro_dur,
                      outro_dur)

        return time.time() - start
