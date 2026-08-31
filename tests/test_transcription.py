"""Tests for optional local-Whisper helpers (model import is mocked)."""

import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.transcription import (find_caption_srt, srt_to_plain_text,
                               parse_srt, apply_srt_scene_timing,
                               transcribe_to_srt)
from src.script import segment_scenes


class CaptionSrtTest(unittest.TestCase):
    def test_prefers_user_srt_and_extracts_text(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = os.path.join(directory, "script.srt")
            with open(srt, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\nHello <i>world</i>.\n\n")
            self.assertEqual(find_caption_srt(directory), srt)
            self.assertEqual(srt_to_plain_text(srt), "Hello world.")

    def test_parse_srt_cue_times_and_text(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = os.path.join(directory, "script.srt")
            with open(srt, "w", encoding="utf-8") as fh:
                fh.write(
                    "1\n"
                    "00:00:00,000 --> 00:00:02,500\n"
                    "The Roman Empire seemed unstoppable.\n\n"
                    "2\n"
                    "00:00:02,500 --> 00:00:05,000\n"
                    "Its legions marched at dawn.\n")
            cues = parse_srt(srt)
            self.assertEqual(len(cues), 2)
            self.assertEqual(cues[0]["start"], 0.0)
            self.assertEqual(cues[0]["end"], 2.5)
            self.assertEqual(cues[0]["text"],
                             "The Roman Empire seemed unstoppable.")
            self.assertEqual(cues[1]["start"], 2.5)
            self.assertEqual(cues[1]["end"], 5.0)
            self.assertEqual(cues[1]["text"], "Its legions marched at dawn.")

    def test_parse_srt_accepts_dot_decimal_and_drops_empty_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = os.path.join(directory, "script.srt")
            with open(srt, "w", encoding="utf-8") as fh:
                fh.write(
                    "1\n"
                    "00:00:00.000 --> 00:00:01.500\n"
                    "Only one cue with text.\n\n"
                    "2\n"
                    "00:00:01.500 --> 00:00:02.000\n\n")
            cues = parse_srt(srt)
            self.assertEqual(len(cues), 1)
            self.assertEqual(cues[0]["end"], 1.5)

    def test_apply_srt_scene_timing_assigns_real_boundaries(self):
        scenes = segment_scenes(
            "[SCENE 1]\nThe Roman Empire seemed unstoppable.\n\n"
            "[SCENE 2]\nIts legions marched at dawn.\n")
        with tempfile.TemporaryDirectory() as directory:
            srt = os.path.join(directory, "script.srt")
            with open(srt, "w", encoding="utf-8") as fh:
                fh.write(
                    "1\n00:00:00,000 --> 00:00:02,500\n"
                    "The Roman Empire seemed unstoppable.\n\n"
                    "2\n00:00:02,500 --> 00:00:05,000\n"
                    "Its legions marched at dawn.\n")
            cues = parse_srt(srt)
            self.assertTrue(apply_srt_scene_timing(scenes, cues))
        self.assertEqual(scenes[0]["start"], 0.0)
        self.assertEqual(scenes[0]["end"], 2.5)
        self.assertEqual(scenes[1]["start"], 2.5)
        self.assertEqual(scenes[1]["end"], 5.0)

    def test_apply_srt_scene_timing_interpolates_unmatched_scene(self):
        scenes = segment_scenes(
            "[SCENE 1]\nAlpha beta gamma.\n\n"
            "[SCENE 2]\nDelta epsilon zeta.\n\n"
            "[SCENE 3]\nEta theta iota.\n")
        with tempfile.TemporaryDirectory() as directory:
            srt = os.path.join(directory, "script.srt")
            with open(srt, "w", encoding="utf-8") as fh:
                fh.write(
                    "1\n00:00:00,000 --> 00:00:03,000\n"
                    "Alpha beta gamma.\n\n"
                    "2\n00:00:06,000 --> 00:00:09,000\n"
                    "Eta theta iota.\n")
            cues = parse_srt(srt)
            self.assertTrue(apply_srt_scene_timing(scenes, cues))
        self.assertEqual(scenes[0]["start"], 0.0)
        self.assertEqual(scenes[0]["end"], 3.0)
        # Scene 2 is interpolated inside the [3.0, 6.0] gap.
        self.assertGreaterEqual(scenes[1]["start"], 3.0)
        self.assertLessEqual(scenes[1]["end"], 6.0)
        self.assertEqual(scenes[2]["start"], 6.0)
        self.assertEqual(scenes[2]["end"], 9.0)

    def test_apply_srt_scene_timing_returns_false_without_cues(self):
        scenes = segment_scenes("[SCENE 1]\nHello world.\n")
        self.assertFalse(apply_srt_scene_timing(scenes, []))
        self.assertNotIn("start", scenes[0])


class LocalWhisperMockTest(unittest.TestCase):
    def test_mocked_model_writes_timed_srt_and_transcript(self):
        class FakeModel:
            def __init__(self, model, device, compute_type):
                self.model = model
                self.device = device
                self.compute_type = compute_type

            def transcribe(self, _path, **_kwargs):
                segment = SimpleNamespace(
                    start=0.0,
                    end=1.2,
                    text=" Hello world.",
                    words=[
                        SimpleNamespace(word=" Hello", start=0.0, end=0.4),
                        SimpleNamespace(word=" world.", start=0.45, end=1.2),
                    ],
                )
                return iter([segment]), SimpleNamespace(language="en")

        fake_module = types.ModuleType("faster_whisper")
        fake_module.WhisperModel = FakeModel
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "narration.aac")
            srt = os.path.join(directory, "dashboard_captions.srt")
            transcript = os.path.join(directory, "dashboard_transcript.txt")
            with open(source, "wb") as fh:
                fh.write(b"not real audio; mocked model")
            with mock.patch.dict(sys.modules, {"faster_whisper": fake_module}):
                result = transcribe_to_srt(source, srt, transcript, model_size="base")
            self.assertEqual(result["cue_count"], 1)
            with open(srt, encoding="utf-8") as fh:
                self.assertIn("Hello world.", fh.read())
            with open(transcript, encoding="utf-8") as fh:
                self.assertIn("Hello world.", fh.read())


if __name__ == "__main__":
    unittest.main()
