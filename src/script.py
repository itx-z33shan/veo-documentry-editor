"""Parse the optional documentary script into logical scenes.

The tool supports two script formats:

1. Explicit markers::

       [SCENE 1]
       The Roman Empire seemed unstoppable...

       [SCENE 2]
       But centuries later...

2. Plain prose with no markers. In that case a deterministic, LLM-free
   segmentation splits the script into scenes on paragraph / sentence
   boundaries. This is a deterministic fallback — an LLM is never required.

The script is used to derive *visual requirements* for each scene, which the
matcher uses for semantic clip assignment (MODE B). The exact wording is
never forced onto the video; the user stays responsible for accuracy.
"""

import os
import re

from .errors import MediaNotFoundError

# Markers like [SCENE 1], [SCENE 01], [SCENE ONE], [SCENE-3]
_SCENE_MARKER = re.compile(
    r"^\s*\[?\s*SCENE\s*[-_.: ]?\s*([0-9]+|[ivxlcdm]+)\s*\]?\s*:?\s*$",
    re.IGNORECASE)

# Target an average of ~20 seconds of narration per scene.
SECONDS_PER_SCENE = 20.0


def find_script(input_dir):
    """Return path to input/script.txt if it exists, else None."""
    candidates = ["script.txt", "transcript.txt", "script.srt"]
    for name in candidates:
        path = os.path.join(input_dir, name)
        if os.path.isfile(path):
            return path
    return None


def _split_sentences(text):
    """Split text into sentences on punctuation, keeping delimiters."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def segment_scenes(script_text):
    """Return a list of scene dicts.

    Each scene: {"title": str, "text": str, "sentences": [str, ...]}
    """
    lines = [ln.strip() for ln in script_text.splitlines()]
    scenes = []
    current = {"title": None, "sentences": []}

    for line in lines:
        if not line:
            # Blank line ends a paragraph; flush a plain-prose scene.
            if current["sentences"] and current["title"] is None:
                scenes.append(current)
                current = {"title": None, "sentences": []}
            continue
        m = _SCENE_MARKER.match(line)
        if m:
            if current["sentences"] or current["title"] is not None:
                scenes.append(current)
            current = {"title": line, "sentences": []}
            continue
        # Accumulate text (split into sentences lazily below).
        current["sentences"].extend(_split_sentences(line))

    if current["sentences"] or current["title"] is not None:
        scenes.append(current)

    # If there were no explicit markers, the per-paragraph flush above may
    # already have produced scenes. If there are none at all (single blob),
    # split on a time budget to guarantee a deterministic result.
    if not scenes:
        text = script_text.strip()
        if not text:
            raise MediaNotFoundError("Script file is empty.")
        sentences = _split_sentences(text)
        scenes = _package_by_budget(sentences)

    # Normalize titles.
    for i, sc in enumerate(scenes, 1):
        if not sc["title"]:
            sc["title"] = "Scene %d" % i
        sc["index"] = i
        sc["text"] = " ".join(sc["sentences"]).strip()
    return scenes


def _package_by_budget(sentences):
    """Group sentences into scenes so each is ~SECONDS_PER_SCENE of narration.

    Approximate narration rate: ~2.6 words/second. This is only a rough
    estimator; subtitle timing uses the real narration duration later.
    """
    words_per_second = 2.6
    target_words = int(SECONDS_PER_SCENE * words_per_second)

    scenes = []
    bucket = []
    words = 0
    for s in sentences:
        bucket.append(s)
        words += len(s.split())
        if words >= target_words:
            scenes.append({"title": None, "sentences": bucket})
            bucket = []
            words = 0
    if bucket:
        scenes.append({"title": None, "sentences": bucket})
    return scenes


def derive_visual_requirements(scene):
    """Return a dict of keyword search terms for a scene.

    This is a lightweight keyword extractor (lowercasing, stopword removal).
    It does NOT invent meaning — it just provides tokens the matcher can use.
    """
    text = scene.get("text", "").lower()
    tokens = re.findall(r"[a-z][a-z']{2,}", text)
    stopwords = set(
        "the and but or for nor so yet a an of to in on at by with from as "
        "into during over under across through before after between this that "
        "these those it its it's they their them we our us you your i he she "
        "his her who whom which what when where why how was were is are been "
        "being be will would should could may might shall can have has had "
        "not no than then there here were was do does did about against "
        "because been can't cannot could've down had how i've is isn't it'll "
        "just more most other some such up very way".split())
    tokens = [t for t in tokens if t not in stopwords and len(t) > 2]
    # Preserve order of first appearance.
    seen = set()
    out = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:12]
