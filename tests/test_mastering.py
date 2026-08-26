"""Unit tests for existing-master finishing logic (no FFmpeg binary needed)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import load_config
from src.inputs import find_narration
from src.mastering import MasterFinisher, write_master_subtitles


class FakeProbe:
    """Small deterministic media probe for command-planning tests."""

    def video(self, path):
        return {
            "file": os.path.basename(path), "path": os.path.abspath(path),
            "duration": 660.0, "width": 1920, "height": 1080,
            "fps": 30.0, "codec": "h264", "has_audio": True,
        }

    def audio(self, path):
        name = os.path.basename(path)
        duration = 660.0 if "narration" in name else 120.0
        return {
            "file": name, "path": os.path.abspath(path),
            "duration": duration, "sample_rate": 48000, "channels": 2,
            "codec": "aac",
        }


def _cfg():
    root = os.path.join(os.path.dirname(__file__), "..")
    return load_config(os.path.join(root, "config.json"))


def _touch(path, text=""):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class InputDiscoveryTest(unittest.TestCase):
    def test_accepts_aac_narration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "narration.aac")
            _touch(path)
            self.assertEqual(find_narration(directory), os.path.abspath(path))


class MasterFinisherTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.finisher = MasterFinisher(self.cfg, "ffmpeg", FakeProbe())

    def test_preserve_uses_only_embedded_mix(self):
        with tempfile.TemporaryDirectory() as directory:
            master = os.path.join(directory, "master.mp4")
            narration = os.path.join(directory, "narration.aac")
            _touch(master)
            _touch(narration)
            plan = self.finisher.prepare(master, "preserve", narration)
            cmd = self.finisher.build_command(plan, os.path.join(directory, "out.mp4"))
            command = " ".join(cmd)
            self.assertIn("[0:a]aresample", command)
            self.assertNotIn("-i %s" % narration, command)
            self.assertTrue(any("duplicate/echoed" in w for w in plan["warnings"]))

    def test_replace_discards_embedded_mix_and_uses_clean_narration(self):
        with tempfile.TemporaryDirectory() as directory:
            master = os.path.join(directory, "master.mp4")
            narration = os.path.join(directory, "narration.aac")
            _touch(master)
            _touch(narration)
            plan = self.finisher.prepare(master, "replace", narration)
            cmd = self.finisher.build_command(plan, os.path.join(directory, "out.mp4"))
            command = " ".join(cmd)
            self.assertIn("-i %s" % narration, command)
            self.assertIn("[1:a]aresample", command)
            self.assertNotIn("[0:a]aresample", command)
            self.assertTrue(any("discarded" in w for w in plan["warnings"]))

    def test_rebuild_uses_external_music_and_ducking(self):
        with tempfile.TemporaryDirectory() as directory:
            master = os.path.join(directory, "master.mp4")
            narration = os.path.join(directory, "narration.aac")
            music = os.path.join(directory, "background.m4a")
            _touch(master)
            _touch(narration)
            _touch(music)
            plan = self.finisher.prepare(master, "rebuild", narration, music)
            cmd = self.finisher.build_command(plan, os.path.join(directory, "out.mp4"))
            command = " ".join(cmd)
            self.assertIn("-stream_loop -1 -i %s" % music, command)
            self.assertIn("sidechaincompress", command)
            self.assertIn("loudnorm=I=-14", command)

    def test_final_fade_is_applied_to_video_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            master = os.path.join(directory, "master.mp4")
            _touch(master)
            self.cfg["master_fade_seconds"] = 0.35
            plan = self.finisher.prepare(master, "preserve")
            cmd = self.finisher.build_command(plan, os.path.join(directory, "out.mp4"))
            command = " ".join(cmd)
            self.assertIn("[0:v]fade=t=in", command)
            self.assertIn("afade=t=in", command)
            self.assertIn("-c:v libx264", command)


class MasterSubtitleTest(unittest.TestCase):
    def test_plain_transcript_writes_reviewable_srt(self):
        with tempfile.TemporaryDirectory() as directory:
            script = os.path.join(directory, "transcript.txt")
            output = os.path.join(directory, "subtitles.srt")
            _touch(script, "First sentence. Second sentence.")
            info, warnings = write_master_subtitles(script, output, _cfg(), 10.0)
            self.assertEqual(info["source"], "proportional_transcript")
            self.assertTrue(os.path.isfile(output))
            self.assertTrue(any("proportionally" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
