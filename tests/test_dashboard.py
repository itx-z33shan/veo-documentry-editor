"""Pure-logic tests for the local finishing dashboard."""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import (DashboardError, DashboardState, build_dashboard_config,
                           recommend_workflow)


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
            "clipAudioVolume": 0.12,
            "musicVolume": 0.08,
            "ducking": True,
            "transition": "crossfade",
            "crossfadeSeconds": 0.3,
        })
        self.assertTrue(config["clip_audio_enabled"])
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
