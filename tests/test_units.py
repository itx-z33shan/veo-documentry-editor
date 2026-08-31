"""Unit tests for the pure-logic layers (no FFmpeg required).

Run from the repository root:
    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import load_config, PACING
from src import audio as audio_mod
from src.scanner import natural_key
from src.script import segment_scenes, derive_visual_requirements, _package_by_budget
from src.matcher import assign_clips_to_scenes
from src.subtitles import write_srt, write_ass, _fmt_timestamp
from src.timeline import build_timeline, _default_scene
from src.overrides import apply_overrides
from src.renderer import Renderer, _check_disk, _filtergraph_escape
from src.errors import DiskSpaceError

_USAGE = namedtuple("_Usage", ["total", "used", "free"])


def _cfg(tmp=None):
    path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    return load_config(path)


def _narration(dur=60.0):
    return {"file": "narration.mp3", "duration": dur, "sample_rate": 44100,
            "channels": 2, "codec": "mp3"}


def _clips(n=6):
    clips = []
    for i in range(1, n + 1):
        clips.append({
            "file": "clip_%03d.mp4" % i,
            "path": "/x/clip_%03d.mp4" % i,
            "duration": 5.0,
            "width": 1920, "height": 1080,
            "fps": 30.0, "codec": "h264", "has_audio": False,
            "description": "", "tags": [],
        })
    return clips


class NaturalSortTest(unittest.TestCase):
    def test_natural_order(self):
        names = ["10.mp4", "2.mp4", "1.mp4", "3.mp4"]
        self.assertEqual(sorted(names, key=natural_key),
                         ["1.mp4", "2.mp4", "3.mp4", "10.mp4"])


class ScriptTest(unittest.TestCase):
    def test_markers(self):
        text = ("[SCENE 1]\nRome was great.\n[SCENE 2]\nThen it fell.\n")
        scenes = segment_scenes(text)
        self.assertEqual(len(scenes), 2)
        self.assertEqual(scenes[0]["index"], 1)
        self.assertEqual(scenes[1]["index"], 2)

    def test_prose_fallback(self):
        text = ("One two three four five six seven eight nine ten. "
                "Eleven twelve thirteen fourteen fifteen sixteen.")
        scenes = segment_scenes(text)
        self.assertTrue(len(scenes) >= 1)
        # All sentences preserved.
        all_words = sum(len(sent.split()) for sc in scenes
                        for sent in sc["sentences"])
        self.assertGreater(all_words, 0)

    def test_visual_requirements(self):
        scene = {"text": "British forces advanced through the desert"}
        reqs = derive_visual_requirements(scene)
        self.assertIn("desert", reqs)
        self.assertIn("forces", reqs)


class MatcherTest(unittest.TestCase):
    def test_sequential(self):
        scenes = [{"index": 1}, {"index": 2}]
        clips = _clips(4)
        cfg = _cfg()
        cfg["clip_strategy"] = "sequential"
        mapping = assign_clips_to_scenes(scenes, clips, cfg)
        self.assertEqual(len(mapping[1]) + len(mapping[2]), 4)
        # natural order preserved within buckets
        seq = mapping[1] + mapping[2]
        self.assertEqual(seq, [c["file"] for c in clips])

    def test_semantic_fallback(self):
        scenes = [{"index": 1, "text": "desert battle"}]
        clips = _clips(3)  # no metadata
        cfg = _cfg()
        cfg["clip_strategy"] = "semantic"
        cfg["fallback_strategy"] = "sequential"
        mapping = assign_clips_to_scenes(scenes, clips, cfg)
        self.assertEqual(mapping[1], [c["file"] for c in clips])


class AudioTest(unittest.TestCase):
    def test_final_mix_masters_combined_audio(self):
        graph = audio_mod.final_mix(["nar", "bed"], 60.0, _cfg())
        self.assertIn("amix=inputs=2", graph)
        self.assertIn("loudnorm=I=-14", graph)
        self.assertIn("alimiter=limit=0.98", graph)
        self.assertIn("atrim=start=0:end=60", graph)

    def test_embedded_clip_bed_can_duck_under_narration(self):
        cfg = _cfg()
        cfg["clip_audio_ducking_enabled"] = True
        graph = audio_mod.clip_bed_chain("2:a", "bed", cfg, "nar_key")
        self.assertIn("volume=%g" % cfg["clip_audio_volume"], graph)
        self.assertIn("sidechaincompress", graph)
        self.assertIn("[bed_pre][nar_key]", graph)


class RendererAudioGraphTest(unittest.TestCase):
    def test_separate_narration_keys_duck_music_and_clip_bed(self):
        cfg = _cfg()
        cfg.update({
            "clip_audio_enabled": True,
            "clip_audio_ducking_enabled": True,
            "ducking_enabled": True,
        })
        with tempfile.TemporaryDirectory() as directory:
            narration = os.path.join(directory, "narration.aac")
            music = os.path.join(directory, "background.m4a")
            bed = os.path.join(directory, "clip_bed.m4a")
            for path in (narration, music, bed):
                with open(path, "wb") as fh:
                    fh.write(b"x")
            renderer = object.__new__(Renderer)
            renderer.step1 = directory
            renderer.ffmpeg = "ffmpeg"
            renderer.cfg = cfg
            renderer.clip_audio = True
            with mock.patch("src.renderer._run_ffmpeg") as run_ffmpeg:
                renderer.mix_audio(narration, music, bed, 60.0)
            cmd = run_ffmpeg.call_args[0][0]
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("asplit=3[nar][nk0][nk1]", graph)
            self.assertIn("[mus_pre][nk0]sidechaincompress", graph)
            self.assertIn("[bed_pre][nk1]sidechaincompress", graph)

    def test_embedded_bed_uses_first_optional_input_without_music(self):
        cfg = _cfg()
        cfg.update({
            "clip_audio_enabled": True,
            "clip_audio_ducking_enabled": True,
            "music_enabled": False,
        })
        with tempfile.TemporaryDirectory() as directory:
            narration = os.path.join(directory, "narration.aac")
            unused_music = os.path.join(directory, "background.m4a")
            bed = os.path.join(directory, "clip_bed.m4a")
            for path in (narration, unused_music, bed):
                with open(path, "wb") as fh:
                    fh.write(b"x")
            renderer = object.__new__(Renderer)
            renderer.step1 = directory
            renderer.ffmpeg = "ffmpeg"
            renderer.cfg = cfg
            renderer.clip_audio = True
            with mock.patch("src.renderer._run_ffmpeg") as run_ffmpeg:
                renderer.mix_audio(narration, unused_music, bed, 60.0)
            cmd = run_ffmpeg.call_args[0][0]
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertEqual(cmd.count("-i"), 2)
            self.assertNotIn(unused_music, cmd)
            self.assertIn(bed, cmd)
            self.assertIn("asplit=2[nar][nk0]", graph)
            self.assertIn("[1:a]aresample", graph)
            self.assertIn("[bed_pre][nk0]sidechaincompress", graph)
            self.assertNotIn("[2:a]", graph)


class SubtitleTest(unittest.TestCase):
    def test_srt_format(self):
        self.assertEqual(_fmt_timestamp(0), "00:00:00,000")
        self.assertEqual(_fmt_timestamp(2.806), "00:00:02,806")
        self.assertEqual(_fmt_timestamp(3661.5), "01:01:01,500")

    def test_write_srt(self):
        cues = [{"start": 0.0, "end": 2.0, "text": "Hello world"}]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.srt")
            write_srt(cues, p)
            with open(p, encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("00:00:00,000 --> 00:00:02,000", content)
            self.assertIn("Hello world", content)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _cfg()
        self.assertEqual(cfg["width"], 1920)
        self.assertEqual(cfg["crf"], 18)
        self.assertEqual(cfg["subtitle_max_chars_per_line"], 42)
        self.assertEqual(cfg["clip_audio_volume"], 0.12)
        self.assertTrue(cfg["clip_audio_ducking_enabled"])

    def test_pacing_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as fh:
                json.dump({"pacing": "fast"}, fh)
            cfg = load_config(path)
        mn, pf, mx = PACING["fast"]
        self.assertEqual(cfg["min_clip_seconds"], mn)
        self.assertEqual(cfg["preferred_clip_seconds"], pf)
        self.assertEqual(cfg["max_clip_seconds"], mx)

    def test_invalid_crf(self):
        from src.errors import ConfigurationError
        cfg = _cfg()
        cfg["crf"] = 99
        with self.assertRaises(ConfigurationError):
            from src.config import _validate
            _validate(cfg)

    def test_invalid_clip_audio_volume(self):
        from src.errors import ConfigurationError
        from src.config import _validate
        cfg = _cfg()
        cfg["clip_audio_volume"] = 0
        with self.assertRaises(ConfigurationError):
            _validate(cfg)


class OverrideTest(unittest.TestCase):
    def test_apply(self):
        clips = {c["file"]: c for c in _clips()}
        timeline = {
            "shots": [{"scene": 3, "clip": "clip_001.mp4", "start": 5.0,
                       "end": 9.0, "length": 4.0, "reused": False}],
        }
        ov = {"scene_3": {"clip": "clip_005.mp4", "duration": 6.5}}
        apply_overrides(timeline, ov, clips)
        self.assertEqual(timeline["shots"][0]["clip"], "clip_005.mp4")
        self.assertEqual(timeline["shots"][0]["length"], 6.5)


class TimelineTest(unittest.TestCase):
    def test_build_matches_narration(self):
        cfg = _cfg()
        narration = _narration(dur=60.0)
        scenes = [{"index": 1, "title": "A", "sentences": ["Hello there one "
                   "two three four five six."],
                   "text": "Hello there"}, {"index": 2, "title": "B",
                   "sentences": ["Goodbye now the end is near for everyone."],
                   "text": "Goodbye"}]
        clips = _clips(4)
        mapping = {1: ["clip_001.mp4", "clip_002.mp4"],
                   2: ["clip_003.mp4", "clip_004.mp4"]}
        timeline = build_timeline(cfg, narration, scenes, mapping, clips)
        self.assertAlmostEqual(timeline["duration"], 60.0, places=1)
        self.assertTrue(timeline["shots"])
        total = sum(s["length"] for s in timeline["shots"])
        self.assertAlmostEqual(total, 60.0, delta=1.0)
        self.assertTrue(timeline["subtitles"])

    def test_default_single_scene(self):
        cfg = _cfg()
        narration = _narration(dur=30.0)
        scenes = _default_scene(30.0)
        clips = _clips(2)
        mapping = {1: ["clip_001.mp4", "clip_002.mp4"]}
        timeline = build_timeline(cfg, narration, scenes, mapping, clips)
        self.assertEqual(len(timeline["scenes"]), 1)

    def test_build_timeline_keeps_srt_scene_boundaries(self):
        cfg = _cfg()
        narration = _narration(dur=6.0)
        scenes = segment_scenes(
            "[SCENE 1]\nAlpha beta gamma delta.\n\n"
            "[SCENE 2]\nEpsilon zeta eta theta.\n")
        # Real boundaries coming from a time-coded SRT.
        scenes[0]["start"], scenes[0]["end"] = 0.0, 3.0
        scenes[1]["start"], scenes[1]["end"] = 3.0, 6.0
        clips = _clips(4)
        mapping = {1: ["clip_001.mp4", "clip_002.mp4"],
                   2: ["clip_003.mp4", "clip_004.mp4"]}
        timeline = build_timeline(cfg, narration, scenes, mapping, clips)
        entries = {s["index"]: s for s in timeline["scenes"]}
        self.assertEqual(entries[1]["start"], 0.0)
        self.assertEqual(entries[1]["end"], 3.0)
        self.assertEqual(entries[2]["start"], 3.0)
        self.assertEqual(entries[2]["end"], 6.0)
        # Shots must not cross the real scene boundaries.
        for shot in timeline["shots"]:
            if shot["scene"] == 1:
                self.assertLessEqual(shot["end"], 3.0 + 1e-6)
            else:
                self.assertGreaterEqual(shot["start"], 3.0 - 1e-6)
        # Subtitles are still derived and stay inside the narration.
        self.assertTrue(timeline["subtitles"])
        self.assertAlmostEqual(timeline["duration"], 6.0, places=1)


# ----------------------------------------------------------------------
# FFmpeg filtergraph parser (test double)
#
# A faithful, minimal reimplementation of the FFmpeg filtergraph quoting
# and escaping semantics (ffmpeg-utils, "Quoting and escaping"):
#
#   * Outside quotes a backslash escapes the next character.
#   * Inside single quotes everything is literal except that \' stands
#     for a single quote.
#   * An unescaped ':' separates filter options; the first unescaped '='
#     in an option separates its key from its value.
#
# The renderer's escaping is validated against this parser, and the
# production crash (Windows SRT path landing in the "original_size"
# option) is reproduced exactly.
# ----------------------------------------------------------------------
def _parse_vf(vf):
    """Parse a single-filter -vf string the way FFmpeg does.

    Returns (filter_name, options); options is a list of (key, value)
    tuples where key is None for positional values.
    """
    segments = []  # each: [key_chars, value_chars, has_key]
    seg = None
    in_quotes = False
    i, n = 0, len(vf)

    def ensure_seg():
        nonlocal seg
        if seg is None:
            seg = [[], [], False]
            segments.append(seg)

    def append(ch):
        (seg[1] if seg[2] else seg[0]).append(ch)

    while i < n:
        ch = vf[i]
        if in_quotes:
            if ch == "\\" and i + 1 < n and vf[i + 1] == "'":
                append("'")
                i += 2
            elif ch == "'":
                in_quotes = False
                i += 1
            else:
                append(ch)
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            ensure_seg()
            append(vf[i + 1])
            i += 2
            continue
        if ch == "'":
            ensure_seg()
            in_quotes = True
            i += 1
            continue
        if ch == ":":
            seg = None
            i += 1
            continue
        ensure_seg()
        if ch == "=" and not seg[2]:
            seg[2] = True
            i += 1
            continue
        append(ch)
        i += 1

    name = "".join(segments[0][0])
    options = []
    if segments[0][2]:
        options.append((None, "".join(segments[0][1])))
    for key_chars, value_chars, has_key in segments[1:]:
        if has_key:
            options.append(("".join(key_chars), "".join(value_chars)))
        else:
            # Positional values accumulate in key_chars (there is no '=').
            options.append((None, "".join(key_chars)))
    return name, options


class FiltergraphEscapeTest(unittest.TestCase):
    """Regression test for the Windows subtitle-burn crash.

    The old build emitted ``subtitles=C:\\Users\\...:original_size=...``
    with an UNESCAPED drive-letter path. The filtergraph parser split it
    at ':', consumed the backslashes as escapes, and applied the path
    tail to ``original_size`` (which expects a WxH image size), so the
    render died before the first frame.
    """

    WINDOWS_SRT = (r"C:\Users\PMLS\Desktop\Veo-Documentry-Editor\output"
                   r"\subtitles.srt")

    def test_unescaped_path_reproduces_reported_failure(self):
        buggy = "subtitles=%s:original_size=%s" % (self.WINDOWS_SRT,
                                                    self.WINDOWS_SRT)
        name, options = _parse_vf(buggy)
        self.assertEqual(name, "subtitles")
        # The drive letter became the filename and the de-escaped tail of
        # the path became the second positional option (original_size).
        self.assertEqual(options[0], (None, "C"))
        self.assertEqual(
            options[1],
            (None, "UsersPMLSDesktopVeo-Documentry-Editoroutputsubtitles.srt"))

    def test_burn_subtitles_windows_path_round_trips(self):
        cfg = _cfg()
        cfg["subtitle_enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            renderer = object.__new__(Renderer)
            renderer.cfg = cfg
            renderer.ffmpeg = "ffmpeg"
            renderer.step1 = directory
            renderer.H = 1080
            renderer.crf = 18
            renderer.preset = "medium"
            renderer.has_subtitles = True
            video = os.path.join(directory, "main_video_raw.mp4")
            with open(video, "wb") as fh:
                fh.write(b"x")
            with mock.patch("os.path.isfile", return_value=True), \
                    mock.patch("src.renderer._run_ffmpeg") as run_ffmpeg:
                out = renderer.burn_subtitles(video, self.WINDOWS_SRT)
            self.assertEqual(out,
                             os.path.join(directory, "main_video_burned.mp4"))
            cmd = run_ffmpeg.call_args[0][0]
            vf = cmd[cmd.index("-vf") + 1]
            name, options = _parse_vf(vf)
            self.assertEqual(name, "subtitles")
            # The whole path must arrive as the single filename value.
            self.assertEqual(options[0], (None, self.WINDOWS_SRT))
            keys = [key for key, _ in options]
            self.assertNotIn("original_size", keys)
            self.assertIn("force_style", keys)
            self.assertTrue(options[-1][1].startswith("Fontname="))

    def test_tricky_paths_round_trip(self):
        for path in (self.WINDOWS_SRT,
                     r"C:\Users\me's clips;folder\sub,titles.srt",
                     r"C:\a=b\c.srt",
                     r"C:\dir\[1]\x.srt",
                     "/home/user/output/subtitles.srt"):
            vf = ("subtitles=%s:force_style='Fontname=DejaVu Sans'"
                  % _filtergraph_escape(path))
            name, options = _parse_vf(vf)
            self.assertEqual(name, "subtitles", path)
            self.assertEqual(options[0], (None, path), path)
            self.assertEqual(options[1],
                             ("force_style", "Fontname=DejaVu Sans"), path)


class CheckDiskTest(unittest.TestCase):
    def test_uses_cross_platform_disk_usage(self):
        # os.statvfs is POSIX-only; on Windows it raises AttributeError,
        # which the OSError guard cannot catch and which used to crash
        # the render before the first stage.
        with mock.patch("shutil.disk_usage",
                        return_value=_USAGE(0, 0, 1024 ** 3)) as usage, \
                mock.patch("os.statvfs",
                           side_effect=AttributeError(
                               "module 'os' has no attribute 'statvfs'")):
            _check_disk("/tmp")  # must not raise
            usage.assert_called_once()

    def test_low_free_space_raises(self):
        with mock.patch("shutil.disk_usage",
                        return_value=_USAGE(0, 0, 1)):
            with self.assertRaises(DiskSpaceError):
                _check_disk("/tmp")


class DrawtextFontTest(unittest.TestCase):
    @staticmethod
    def _renderer(cfg):
        renderer = object.__new__(Renderer)
        renderer.cfg = cfg
        return renderer

    def test_configured_font_wins_when_present(self):
        cfg = _cfg()
        with tempfile.TemporaryDirectory() as directory:
            font = os.path.join(directory, "Font.ttf")
            with open(font, "wb") as fh:
                fh.write(b"x")
            cfg["subtitle_font"] = font
            self.assertEqual(self._renderer(cfg)._drawtext_font(), font)

    def test_returns_none_when_no_font_exists(self):
        cfg = _cfg()
        cfg["subtitle_font"] = "/does/not/exist/Font.ttf"
        with mock.patch("os.path.isfile", return_value=False):
            self.assertIsNone(self._renderer(cfg)._drawtext_font())


if __name__ == "__main__":
    unittest.main()
