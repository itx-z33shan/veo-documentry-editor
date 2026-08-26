"""Unit tests for the pure-logic layers (no FFmpeg required).

Run from the repository root:
    python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
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
from src.renderer import Renderer


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


if __name__ == "__main__":
    unittest.main()
