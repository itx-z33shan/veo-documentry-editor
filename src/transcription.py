"""Optional local Whisper transcription and timed SRT helpers.

No speech model is imported at module load time.  The core editor remains
standard-library-only unless the user explicitly asks the local dashboard to
generate captions.  In that case install the optional ``faster-whisper``
dependency from ``requirements-transcription.txt``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import TranscriptionError
from .subtitles import write_srt


CAPTION_SRT_NAMES = ("script.srt", "captions.srt", "subtitles.srt",
                     "dashboard_captions.srt")


def find_caption_srt(input_dir):
    """Return a conventional time-coded caption SRT, preferring user files."""
    directory = Path(input_dir)
    for name in CAPTION_SRT_NAMES:
        path = directory / name
        if path.is_file():
            return str(path)
    return None


def local_whisper_status():
    """Return ``(available, message)`` without downloading a model."""
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False, (
            "Local transcription needs the optional faster-whisper package. "
            "Run: pip install -r requirements-transcription.txt")
    return True, "Local Whisper is available; the selected model downloads on first use."


def _clean_caption_text(text):
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def srt_to_plain_text(path):
    """Extract readable narration text from a time-coded SRT file."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise TranscriptionError("Cannot read SRT file %r: %s" % (path, exc))

    kept = []
    for raw in lines:
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        cleaned = _clean_caption_text(line)
        if cleaned:
            kept.append(cleaned)
    return " ".join(kept).strip()


_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_srt(path):
    """Parse a time-coded SRT into a list of ``{start, end, text}`` cues.

    Times are returned in seconds. Cue text is cleaned the same way as
    :func:`srt_to_plain_text` (HTML-style tags stripped, whitespace
    collapsed). Cues without any text are dropped.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise TranscriptionError("Cannot read SRT file %r: %s" % (path, exc))

    def _to_seconds(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    cues = []
    text_lines = []
    current = None
    for raw in lines:
        line = raw.strip()
        m = _SRT_TIME_RE.search(line)
        if m:
            if current is not None:
                current["end"] = round(current["end"], 3)
                cues.append(current)
            current = {
                "start": round(_to_seconds(*m.groups()[:4]), 3),
                "end": round(_to_seconds(*m.groups()[4:]), 3),
                "text": "",
            }
            text_lines = []
            continue
        if current is None:
            continue  # cue index line or leading junk
        if not line:
            # Blank line terminates the cue.
            text = " ".join(t for t in (_clean_caption_text(t)
                                        for t in text_lines) if t)
            if text:
                current["text"] = text
                cues.append(current)
            current = None
            continue
        text_lines.append(line)

    if current is not None:
        text = " ".join(t for t in (_clean_caption_text(t)
                                    for t in text_lines) if t)
        if text:
            current["text"] = text
            cues.append(current)
    return cues


def _word_tokens(text):
    """Lowercased word tokens used for ordered cue-to-scene matching."""
    return set(re.findall(r"[a-z0-9']+", str(text).lower()))


def _cue_scene_score(cue_words, scene_words):
    if not cue_words:
        return 0.0
    return len(cue_words & scene_words) / len(cue_words)


def apply_srt_scene_timing(scenes, cues):
    """Overlay real SRT cue times onto scenes as ``[start, end]`` windows.

    The scenes' text is normally derived from the same SRT (via
    :func:`srt_to_plain_text`), so each cue is matched to the scene whose
    words it overlaps most. Matching only moves forward through the scene
    list (ordered assignment), which keeps alignment stable even when the
    narration repeats words. Scenes that match no cue are interpolated
    inside the gap between their timed neighbours.

    Modifies ``scenes`` in place by setting ``start``/``end``. Returns True
    when at least one scene received a real timed window, else False.
    """
    if not scenes or not cues:
        return False
    n = len(scenes)
    scene_words = [_word_tokens(s.get("text", "")) for s in scenes]
    assigned = [[] for _ in range(n)]
    ptr = 0
    for cue in cues:
        cue_words = _word_tokens(cue.get("text", ""))
        best, best_score = ptr, 0.0
        for i in range(ptr, n):
            score = _cue_scene_score(cue_words, scene_words[i])
            if score > best_score:
                best, best_score = i, score
        if best_score <= 0.0:
            best = ptr
        assigned[best].append(cue)
        ptr = best

    if not any(group for group in assigned):
        return False

    for i, group in enumerate(assigned):
        if group:
            scenes[i]["start"] = min(c["start"] for c in group)
            scenes[i]["end"] = max(c["end"] for c in group)
        else:
            scenes[i]["start"] = None
            scenes[i]["end"] = None

    # Interpolate unmatched scenes between their timed neighbours.
    last_known = None
    for i in range(n):
        if assigned[i]:
            last_known = i
            continue
        prev_end = (scenes[last_known]["end"]
                    if last_known is not None else 0.0)
        nxt = next((j for j in range(i + 1, n) if assigned[j]), None)
        if nxt is None:
            start = end = prev_end
        else:
            gap = scenes[nxt]["start"] - prev_end
            wi = len(scenes[i].get("text", "").split())
            wn = len(scenes[nxt].get("text", "").split())
            share = (wi / (wi + wn)) if (wi + wn) else 0.5
            start = prev_end
            end = prev_end + max(0.0, gap) * share
        scenes[i]["start"] = round(start, 3)
        scenes[i]["end"] = round(end, 3)
        last_known = i

    for i in range(n):
        if scenes[i]["start"] is None or scenes[i]["end"] is None:
            scenes[i]["start"] = 0.0
            scenes[i]["end"] = 0.0
        scenes[i]["start"] = round(scenes[i]["start"], 3)
        scenes[i]["end"] = round(scenes[i]["end"], 3)
    return True


def _wrap_caption(text, max_chars_per_line, max_lines):
    """Wrap caption text into the configured one/two-line visual layout."""
    words = _clean_caption_text(text).split()
    if not words:
        return ""
    lines = []
    current = []
    current_len = 0
    for word in words:
        extra = len(word) + (1 if current else 0)
        if current and current_len + extra > max_chars_per_line and len(lines) < max_lines - 1:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += extra
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines[:max_lines])


def _cues_from_words(words, max_chars, max_lines, max_seconds=5.0):
    """Turn word timestamps into readable short SRT cues."""
    max_total = max_chars * max_lines
    cues = []
    bucket = []
    bucket_start = None
    last_end = 0.0

    def flush():
        nonlocal bucket, bucket_start, last_end
        if not bucket:
            return
        text = " ".join(item["text"] for item in bucket).strip()
        start = float(bucket_start if bucket_start is not None else last_end)
        end = float(bucket[-1]["end"])
        if end <= start:
            end = start + 0.4
        if cues and start < cues[-1]["end"]:
            start = cues[-1]["end"]
        wrapped = _wrap_caption(text, max_chars, max_lines)
        if wrapped:
            cues.append({"start": round(start, 3), "end": round(end, 3),
                         "text": wrapped})
        last_end = end
        bucket = []
        bucket_start = None

    for word in words:
        text = _clean_caption_text(word.get("text"))
        if not text:
            continue
        start = float(word.get("start") if word.get("start") is not None else last_end)
        end = float(word.get("end") if word.get("end") is not None else start + 0.35)
        current_text = " ".join(item["text"] for item in bucket)
        proposed = (current_text + " " + text).strip()
        too_long = bucket and len(proposed) > max_total
        too_long_in_time = bucket and start - float(bucket_start) >= max_seconds
        if too_long or too_long_in_time:
            flush()
        if bucket_start is None:
            bucket_start = start
        bucket.append({"text": text, "end": max(end, start + 0.05)})
    flush()
    return cues


def _fallback_cue(segment, max_chars, max_lines, previous_end):
    text = _wrap_caption(getattr(segment, "text", ""), max_chars, max_lines)
    if not text:
        return None
    start = float(getattr(segment, "start", previous_end) or previous_end)
    end = float(getattr(segment, "end", start + 0.5) or start + 0.5)
    if start < previous_end:
        start = previous_end
    if end <= start:
        end = start + 0.5
    return {"start": round(start, 3), "end": round(end, 3), "text": text}


def transcribe_to_srt(audio_path, srt_path, transcript_path, model_size="base",
                      language=None, device="cpu", compute_type="int8",
                      max_chars_per_line=42, max_lines=2,
                      progress_callback=None):
    """Transcribe local audio/video with faster-whisper and write SRT + TXT.

    ``audio_path`` may be a clean narration file or an MP4 with embedded audio.
    Word-level timestamps are used when available; segment timing is retained
    as a fallback.  The returned SRT is a review draft, never a claim of
    perfect punctuation or factual spelling.
    """
    source = Path(audio_path)
    if not source.is_file():
        raise TranscriptionError("Transcription source was not found: %r" % audio_path)
    if model_size not in {"tiny", "base", "small", "medium", "large-v3"}:
        raise TranscriptionError("Unsupported local Whisper model: %r" % model_size)
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise TranscriptionError(
            "Local transcription needs faster-whisper. Install it with: "
            "pip install -r requirements-transcription.txt",
            hint="Then restart the dashboard and run the dry check again.") from exc

    callback = progress_callback or (lambda _message: None)
    callback("Loading local Whisper model '%s'…" % model_size)
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments, info = model.transcribe(
            str(source),
            language=language or None,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=False,
        )
    except Exception as exc:
        raise TranscriptionError("Local Whisper could not start transcription: %s" % exc) from exc

    cues = []
    transcript_segments = []
    previous_end = 0.0
    segment_count = 0
    try:
        for segment in segments:
            segment_count += 1
            segment_text = _clean_caption_text(getattr(segment, "text", ""))
            if segment_text:
                transcript_segments.append(segment_text)
            words = []
            for word in getattr(segment, "words", None) or []:
                word_text = _clean_caption_text(getattr(word, "word", ""))
                if word_text:
                    words.append({
                        "text": word_text,
                        "start": getattr(word, "start", None),
                        "end": getattr(word, "end", None),
                    })
            if words:
                segment_cues = _cues_from_words(words, int(max_chars_per_line),
                                                 int(max_lines))
                if segment_cues:
                    if cues and segment_cues[0]["start"] < cues[-1]["end"]:
                        segment_cues[0]["start"] = cues[-1]["end"]
                    cues.extend(segment_cues)
                    previous_end = max(previous_end, segment_cues[-1]["end"])
            else:
                cue = _fallback_cue(segment, int(max_chars_per_line),
                                    int(max_lines), previous_end)
                if cue:
                    cues.append(cue)
                    previous_end = cue["end"]
            if segment_count == 1 or segment_count % 5 == 0:
                callback("Transcribing: %s processed" %
                         _format_time(getattr(segment, "end", previous_end)))
    except Exception as exc:
        raise TranscriptionError("Local Whisper stopped during transcription: %s" % exc) from exc

    if not cues:
        detected = getattr(info, "language", None)
        language_note = " (%s detected)" % detected if detected else ""
        raise TranscriptionError("No spoken caption cues were detected%s." % language_note)

    srt_path = Path(srt_path)
    transcript_path = Path(transcript_path)
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    write_srt(cues, str(srt_path))
    with transcript_path.open("w", encoding="utf-8") as fh:
        fh.write("\n\n".join(transcript_segments).strip() + "\n")

    detected_language = getattr(info, "language", None)
    callback("Local Whisper finished: %d SRT cue(s)%s." % (
        len(cues), "; detected %s" % detected_language if detected_language else ""))
    return {
        "source": str(source),
        "srt_path": str(srt_path),
        "transcript_path": str(transcript_path),
        "cue_count": len(cues),
        "segment_count": segment_count,
        "language": detected_language,
        "model": model_size,
    }


def _format_time(seconds):
    seconds = max(0.0, float(seconds or 0))
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%d:%02d" % (minutes, seconds)
