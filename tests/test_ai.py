"""Tests for the optional Gemini AI layer (mocked — no network/API calls).

Run: python -m unittest discover -s tests
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.vectorstore import VectorStore, cosine_similarity
from src.ai import _extract_json
from src.config import load_config
from src.matcher import assign_clips_with_ai


def _cfg():
    path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    return load_config(path)


class VectorStoreTest(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(
            cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(
            cosine_similarity([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine_similarity([], []), 0.0)

    def test_store_roundtrip_and_query(self, tmp="/tmp/vs_test.json"):
        store = VectorStore(tmp)
        store.put("a.mp4", [1, 0, 0], "k1")
        store.put("b.mp4", [0, 1, 0], "k2")
        store.save()
        store2 = VectorStore(tmp).load()
        self.assertTrue(store2.is_fresh("a.mp4", "k1"))
        self.assertFalse(store2.is_fresh("a.mp4", "changed"))
        top = store2.query([1.0, 0.0, 0.0], k=2)
        self.assertEqual(top[0][0], "a.mp4")
        if os.path.exists(tmp):
            os.remove(tmp)


class ExtractJsonTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        text = '```json\n{"clips": ["x.mp4"]}\n```'
        self.assertEqual(_extract_json(text), {"clips": ["x.mp4"]})

    def test_noise(self):
        text = 'Here you go:\n{"scene": 2, "clips": ["a.mp4","b.mp4"]}\nDone.'
        self.assertEqual(_extract_json(text)["scene"], 2)


class GeminiIntegrationTest(unittest.TestCase):
    def test_unavailable_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = _cfg()
            cfg["ai_provider"] = "gemini"
            clips = [{"file": "clip_001.mp4", "path": "/x/a.mp4",
                      "duration": 5.0, "width": 1920, "height": 1080,
                      "fps": 30.0, "codec": "h264", "has_audio": False,
                      "description": "", "tags": []}]
            scenes = [{"index": 1, "title": "A", "text": "desert"}]
            mapping, warnings = assign_clips_with_ai(scenes, clips, cfg, {})
            self.assertEqual(mapping[1], ["clip_001.mp4"])
            self.assertTrue(warnings)

    def test_mocked_pipeline_validates(self):
        from src.ai import GeminiAI

        clips = [
            {"file": "clip_001.mp4", "path": "/x/1.mp4", "duration": 5.0,
             "width": 1920, "height": 1080, "fps": 30.0, "codec": "h264",
             "has_audio": False, "description": "", "tags": []},
            {"file": "clip_002.mp4", "path": "/x/2.mp4", "duration": 5.0,
             "width": 1920, "height": 1080, "fps": 30.0, "codec": "h264",
             "has_audio": False, "description": "", "tags": []},
        ]
        scenes = [{"index": 1, "title": "S1", "text": "desert battle"}]

        store = VectorStore(None)
        store.put("clip_002.mp4", [1.0, 0.0], "k")
        store.put("clip_001.mp4", [0.0, 1.0], "k")

        cfg = _cfg()
        cfg["ai_provider"] = "gemini"
        ai = GeminiAI(cfg)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake"},
                             clear=True):
            with mock.patch.object(ai, "available", return_value=True):
                # describe_clips returns metadata for all clips
                ai.describe_clips = mock.Mock(return_value={
                    "clip_001.mp4": {"description": "desert", "tags": ["x"]},
                    "clip_002.mp4": {"description": "battle", "tags": ["y"]},
                })
                ai.build_index = mock.Mock(return_value=None)
                ai.retrieve = mock.Mock(return_value=[
                    ("clip_002.mp4", 0.9), ("clip_001.mp4", 0.6)])
                # finalize_scene deliberately returns an INVALID filename
                # plus a valid one to verify validation filtering.
                ai.finalize_scene = mock.Mock(return_value=[
                    "clip_002.mp4", "nonexistent.mp4"])
                store.save = mock.Mock()

                mapping, _ = ai.assign_scenes(scenes, clips, {}, store)
                chosen = mapping[1]
                self.assertIn("clip_002.mp4", chosen)
                # Invalid filename must have been filtered out.
                self.assertNotIn("nonexistent.mp4", chosen)


if __name__ == "__main__":
    unittest.main()
