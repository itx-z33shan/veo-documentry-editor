"""Audio handling: mixing graph builders, loudness, music ducking.

These helpers return FFmpeg filter graphs. The actual FFmpeg invocation
lives in renderer.py — this module only *plans* the audio.

Audio chain for the final mix:

    narration  -> aresample -> loudnorm (-14 LUFS, -1.5 dBTP)
    music      -> (loop/trim) -> volume -> fade in/out -> duck (sidechain)
    clip bed   -> (optional low-volume) -> volume
    mix        -> amix -> loudnorm -> limiter -> aresample -> atrim -> final track
"""

import os
import subprocess


def silence_cue_min_duration(cfg):
    return cfg.get("silence_min_duration", 0.3)


def normalize_narration(in_label, out_label, cfg):
    """aresample + loudnorm for the narration track."""
    lufs = cfg.get("loudness_target_lufs", -14.0)
    tp = cfg.get("loudness_target_tp", -1.5)
    lra = cfg.get("loudness_lra", 11.0)
    sr = cfg.get("sample_rate", 48000)
    return ("[%s]aresample=%d,loudnorm=I=%g:TP=%g:LRA=%g[%s]"
            % (in_label, sr, lufs, tp, lra, out_label))


def music_chain(in_label, out_label, narration_dur, cfg, narration_label):
    """Loop/trim music, set volume, fades, and sidechain-duck by narration.

    ``in_label`` must already point at the processed (input-scaled) music
    stream *before* ducking. Ducking uses ``narration_label`` as the
    sidechain source.
    """
    vol = cfg.get("music_volume", 0.08)
    d = float(narration_dur)
    fade_in = min(2.0, d * 0.5)
    fade_out = min(2.0, d * 0.5)
    fade_out_start = max(0.0, d - fade_out)

    chain = "[%s]aresample=%d,volume=%g" % (in_label, cfg.get("sample_rate", 48000), vol)
    if d > 0.5:
        chain += ",afade=t=in:st=0:d=%g,afade=t=out:st=%g:d=%g" % (
            fade_in, fade_out_start, fade_out)
    chain += "[%s_pre]" % out_label
    # Ducking: music_pre is main, narration is sidechain.
    duck = cfg.get("ducking_enabled", True)
    if duck:
        threshold = cfg.get("ducking_threshold", 0.03)
        ratio = cfg.get("ducking_ratio", 8.0)
        attack = cfg.get("ducking_attack_ms", 20)
        release = cfg.get("ducking_release_ms", 300)
        chain += (";[%s_pre][%s]sidechaincompress="
                  "threshold=%g:ratio=%g:attack=%d:release=%d[%s]"
                  % (out_label, narration_label, threshold, ratio, attack,
                     release, out_label))
    else:
        chain += ";[%s_pre]anull[%s]" % (out_label, out_label)
    return chain


def final_mix(labels, narration_dur, cfg):
    """Mix prepared tracks and master the *final* delivery audio.

    Narration is normalized earlier so it remains a stable sidechain key, but
    the addition of music or embedded clip audio can still move the integrated
    loudness. A final loudness pass therefore happens after ``amix``. This is
    the value that matters for a YouTube/Facebook delivery master.

    ``labels``: list of already-prepared stream labels to mix.
    Returns a filter string ending in ``[aout]``.
    """
    n = len(labels)
    sr = int(cfg.get("sample_rate", 48000))
    lufs = float(cfg.get("loudness_target_lufs", -14.0))
    tp = float(cfg.get("loudness_target_tp", -1.5))
    lra = float(cfg.get("loudness_lra", 11.0))
    srcs = "".join("[%s]" % lb for lb in labels)
    d = float(narration_dur)
    if n == 1:
        pre = srcs
    else:
        # normalize=0 keeps each input's deliberate pre-set level intact.
        pre = "%samix=inputs=%d:normalize=0:dropout_transition=3," % (srcs, n)
    return ("%sloudnorm=I=%g:TP=%g:LRA=%g,aresample=%d,"
            "alimiter=limit=0.98:level=false,atrim=start=0:end=%g[aout]"
            % (pre, lufs, tp, lra, sr, d))


def detect_silences(path, ffmpeg_bin, threshold=-45, min_duration=0.3):
    """Return a list of (start, duration) silence gaps in an audio file.

    Uses ffmpeg's silencedetect. Best effort; failures return [].
    """
    cmd = [ffmpeg_bin, "-hide_banner", "-i", path, "-af",
           "silencedetect=noise=%ddB:d=%g" % (threshold, min_duration),
           "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return []
    text = proc.stderr
    starts = []
    ends = []
    for line in text.splitlines():
        if "silence_start" in line:
            try:
                starts.append(float(line.split("silence_start:")[1].strip()))
            except (ValueError, IndexError):
                pass
        if "silence_end" in line:
            try:
                ends.append(float(line.split("silence_end:")[1].split()[0]))
            except (ValueError, IndexError):
                pass
    if not starts:
        return []
    if len(ends) < len(starts):
        ends.append(starts[-1] + min_duration)
    return [(round(s, 3), round(e - s, 3)) for s, e in zip(starts, ends)]


def format_duration(seconds):
    seconds = max(0, int(round(float(seconds))))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)
