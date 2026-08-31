"""Pure-logic tests for the local finishing dashboard."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import (DashboardError, DashboardState, build_dashboard_config,
                           recommend_workflow)
from src.media import SUPPORTED_VIDEO_EXTS


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class DashboardConfigTest(unittest.TestCase):
    def test_master_preserve_config_is_restricted_and_safe(self):
        config, warnings = build_dashboard_config(ROOT, "master-preserve", {
            "loudnessTarget": -14,
            "truePeak": -1.5,
            "aacBitrate": 256,
            "subtitles": True,
            "masterFade": 0.35,
        })
        self.assertEqual(config["master_audio_mode"], "preserve")
        self.assertFalse(config["subtitle_burn_in"])
        self.assertEqual(config["aac_bitrate"], 256)
        self.assertAlmostEqual(config["master_fade_seconds"], 0.35)
        self.assertEqual(warnings, [])

    def test_embedded_clip_audio_forces_safe_cut_over_crossfade(self):
        config, warnings = build_dashboard_config(ROOT, "clips-embedded", {
            "loudnessTarget": -14,
            "truePeak": -1.5,
            "aacBitrate": 256,
            "subtitles": True,
            "keepClipAudio": True,
            "useGeminiMatching": True,
            "clipAudioDucking": True,
            "clipAudioVolume": 0.12,
            "musicVolume": 0.08,
            "ducking": True,
            "transition": "crossfade",
            "crossfadeSeconds": 0.3,
        })
        self.assertTrue(config["clip_audio_enabled"])
        self.assertEqual(config["ai_provider"], "gemini")
        self.assertTrue(config["clip_audio_ducking_enabled"])
        self.assertEqual(config["transition"], "cut")
        self.assertTrue(warnings)

    def test_rejects_out_of_range_audio_setting(self):
        with self.assertRaises(DashboardError):
            build_dashboard_config(ROOT, "master-preserve", {
                "loudnessTarget": 1,
                "truePeak": -1.5,
                "aacBitrate": 256,
                "masterFade": 0.35,
            })


class RecommendationTest(unittest.TestCase):
    def test_baked_master_prefers_preserve(self):
        result = recommend_workflow({
            "master": {"exists": True, "media": {"has_audio": True}},
            "narration": {"exists": True},
            "music": {"exists": False},
            "clips": {"count": 0},
        })
        self.assertEqual(result["workflow"], "master-preserve")
        self.assertTrue(result["warnings"])

    def test_raw_audio_clips_prefers_embedded_clip_workflow(self):
        result = recommend_workflow({
            "master": {"exists": False},
            "narration": {"exists": True},
            "music": {"exists": False},
            "clips": {"count": 70, "audio_clip_count": 70},
        })
        self.assertEqual(result["workflow"], "clips-embedded")


class DashboardUploadTest(unittest.TestCase):
    def test_streaming_upload_writes_canonical_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            payload = b"Final spoken words."
            saved = state.save_upload("transcript", "final transcript.txt",
                                      io.BytesIO(payload), len(payload))
            self.assertEqual(saved["name"], "transcript.txt")
            with open(os.path.join(directory, "input", "transcript.txt"), "rb") as fh:
                self.assertEqual(fh.read(), payload)

    def _clip(self, state, name, payload=b"x", replace=False):
        return state.save_upload("clips", name, io.BytesIO(payload),
                                 len(payload), replace_clips=replace)

    def test_clip_folder_files_are_stripped_to_safe_names(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            saved = self._clip(state, "batch 2/clip_001.mp4")
            self.assertEqual(saved["name"], "clip_001.mp4")
            self.assertTrue(os.path.isfile(os.path.join(directory, "clips",
                                                        "clip_001.mp4")))

    def test_appended_duplicate_clip_names_are_kept_apart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            self.assertEqual(self._clip(state, "a/clip_001.mp4")["name"],
                             "clip_001.mp4")
            self.assertEqual(self._clip(state, "b/clip_001.mp4")["name"],
                             "clip_001_2.mp4")
            names = sorted(os.listdir(os.path.join(directory, "clips")))
            self.assertEqual(names, ["clip_001.mp4", "clip_001_2.mp4"])

    def test_replacing_clip_folder_swaps_videos_but_keeps_the_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            self._clip(state, "clip_001.mp4", b"old")
            (state.clips_dir / "metadata.json").write_text(
                json.dumps({"clip_001.mp4": {"description": "keep me"}}),
                encoding="utf-8")
            self._clip(state, "clip_002.mp4", b"new", replace=True)
            names = sorted(path.name for path in state.clips_dir.iterdir())
            self.assertEqual(names, ["clip_002.mp4", "metadata.json"])

    def test_non_video_file_in_a_clip_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            with self.assertRaises(DashboardError):
                self._clip(state, "poster.jpg")

    def test_clip_metadata_sidecar_is_written_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            payload = json.dumps({"clip_001.mp4": {"description": "sunset"}}).encode()
            saved = state.save_upload("clips", "veo/metadata.json",
                                      io.BytesIO(payload), len(payload))
            self.assertEqual(saved["name"], "metadata.json")
            summary = state.media_summary()
            self.assertTrue(summary["clips"]["metadata"]["exists"])

            with self.assertRaises(DashboardError):
                state.save_upload("clips", "metadata.json",
                                  io.BytesIO(b"{not json"), 9)
            # The rejected upload must not destroy the valid sidecar.
            with open(state.clips_dir / "metadata.json", "rb") as fh:
                self.assertEqual(fh.read(), payload)

            with self.assertRaises(DashboardError):
                state.save_upload("clips", "metadata.json",
                                  io.BytesIO(b"[1, 2]"), 6)


class ClipFolderScriptTest(unittest.TestCase):
    """The browser queue rules are checked by the same logic it ships."""

    script = os.path.join(ROOT, "web", "static", "clips-folder.js")
    scripts = ()

    scripts = ("clips_folder_test.js", "dashboard_ui_test.js")

    def test_node_checks_for_folder_queueing_pass(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed; folder queue checks skipped.")
        for script in self.scripts:
            with self.subTest(script=script):
                result = subprocess.run(
                    [node, os.path.join(ROOT, "tests", script)],
                    capture_output=True, text=True, cwd=ROOT, timeout=120)
                self.assertEqual(result.returncode, 0,
                                 (result.stdout + result.stderr).strip())

    def test_queue_rules_match_the_dashboard_acceptance_lists(self):
        import re

        with open(self.script, "r", encoding="utf-8") as fh:
            source = fh.read()
        video = re.search(r"var VIDEO_EXTENSIONS = \[(.*?)\];", source, re.S)
        self.assertIsNotNone(video)
        listed = set(re.findall(r'"(\.[A-Za-z0-9]+)"', video.group(1)))
        self.assertEqual(listed, set(SUPPORTED_VIDEO_EXTS))
        self.assertIn('var METADATA_NAME = "metadata.json"', source)
        self.assertIn("var MAX_NAME_LENGTH = 180", source)


class DashboardTranscriptionTest(unittest.TestCase):
    def test_reuses_dashboard_generated_caption_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            state.dashboard_transcript.write_text("Generated words.", encoding="utf-8")
            state.dashboard_captions.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nGenerated words.\n",
                encoding="utf-8")
            request = state._transcription_request(
                "master-preserve", {"autoTranscript": True}, None, None)
            self.assertTrue(request["reuse"])


class DashboardOutputTest(unittest.TestCase):
    def test_output_listing_ignores_gitkeep_and_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            state = DashboardState(directory)
            with open(os.path.join(directory, "output", ".gitkeep"), "w") as fh:
                fh.write("")
            with open(os.path.join(directory, "output", "final_master.mp4"), "w") as fh:
                fh.write("video")
            self.assertEqual([item["name"] for item in state.output_files()],
                             ["final_master.mp4"])
            with self.assertRaises(DashboardError):
                state.output_path("../secret.txt")


if __name__ == "__main__":
    unittest.main()
