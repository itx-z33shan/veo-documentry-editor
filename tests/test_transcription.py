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
                               transcribe_to_srt)


class CaptionSrtTest(unittest.TestCase):
    def test_prefers_user_srt_and_extracts_text(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = os.path.join(directory, "script.srt")
            with open(srt, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\nHello <i>world</i>.\n\n")
            self.assertEqual(find_caption_srt(directory), srt)
            self.assertEqual(srt_to_plain_text(srt), "Hello world.")


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
