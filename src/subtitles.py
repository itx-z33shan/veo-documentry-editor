"""Write subtitle files (.srt and optionally .ass) from timeline cues."""

import os


def _fmt_timestamp(seconds):
    """Convert seconds (float) to SRT timestamp HH:MM:SS,mmm."""
    seconds = max(0.0, float(seconds))
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if ms >= 1000:
        ms = 0
        s += 1
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _fmt_ass_time(seconds):
    seconds = max(0.0, float(seconds))
    cs = int(round(seconds * 100))
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return "%d:%02d:%02d.%02d" % (h, m, s, cs % 100)


def write_srt(cues, path):
    """Write an SRT file from a list of {start, end, text} dicts."""
    with open(path, "w", encoding="utf-8") as fh:
        for i, cue in enumerate(cues, 1):
            fh.write("%d\n" % i)
            fh.write("%s --> %s\n" % (_fmt_timestamp(cue["start"]),
                                      _fmt_timestamp(cue["end"])))
            fh.write("%s\n\n" % cue["text"].strip())


def write_ass(cues, path, cfg, width, height):
    """Write an ASS subtitle file with styling from config."""
    font = cfg.get("subtitle_font_size", 24)
    margin = max(20, int(height * 0.06))
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: %d\n"
        "PlayResY: %d\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,DejaVu Sans,%d,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,20,20,%d,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    ) % (width, height, font, margin)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header)
        for cue in cues:
            text = cue["text"].strip().replace("\n", r"\N")
            fh.write("Dialogue: 0,%s,%s,Default,,0,0,0,,%s\n" % (
                _fmt_ass_time(cue["start"]), _fmt_ass_time(cue["end"]), text))


def has_text(cues):
    return bool(cues) and any((c.get("text") or "").strip() for c in cues)
