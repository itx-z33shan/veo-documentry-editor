"""Optional Gemini AI intelligence layer (fully optional).

This module implements the "INTELLIGENCE" side of the editor using Google's
Gemini free-tier models, exactly mirroring the intended pipeline:

    Veo clips ──Gemini 3.7 Flash──> clip descriptions
    descriptions ──Gemini Embedding 2──> embeddings -> local vector DB
    narration -> scene requirements ──retrieval──> top-k candidates
    candidates ──Gemini 3.1 Pro──> final ordered decisions
    decisions ──[validated]──> timeline -> FFmpeg

Everything produced by the models is validated before it can reach FFmpeg
(see :func:`assign_scenes`). If the SDK, API key, or any call is unavailable
or fails, callers fall back to the deterministic matcher — the editor always
works without AI.

Dependencies: `google-genai` (optional). Install with::
    pip install google-genai
"""

import hashlib
import json
import os
import re

from .vectorstore import VectorStore, cosine_similarity  # noqa: F401
from .errors import EditorError

# Default model names (all on the Gemini free tier).
DEFAULT_VISION_MODEL = "models/gemini-3.7-flash"
DEFAULT_EMBEDDING_MODEL = "models/gemini-embedding-2"
DEFAULT_DECISION_MODEL = "models/gemini-3.1-pro-preview"

_DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["description", "tags"],
}

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "integer"},
        "clips": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["scene", "clips"],
}


class GeminiAI:
    def __init__(self, cfg):
        self.cfg = cfg
        self.vision_model = cfg.get("ai_vision_model") or DEFAULT_VISION_MODEL
        self.embed_model = cfg.get("ai_embedding_model") or DEFAULT_EMBEDDING_MODEL
        self.decision_model = cfg.get("ai_decision_model") or DEFAULT_DECISION_MODEL
        self.top_k = int(cfg.get("ai_top_k", 5))
        self.max_video_bytes = int(cfg.get("ai_max_video_bytes", 30 * 1024 * 1024))
        self.api_key_env = cfg.get("ai_api_key_env") or "GEMINI_API_KEY"
        self._client = None
        self._client_error = None
        self.warnings = []

    # ------------------------------------------------------------------
    # availability
    # ------------------------------------------------------------------
    @property
    def api_key(self):
        return os.environ.get(self.api_key_env) or ""

    def available(self):
        """True if an API key is present and the SDK can be imported."""
        return bool(self.api_key) and self._import_ok()

    def _import_ok(self):
        try:
            from google import genai  # noqa: F401
            return True
        except Exception:
            return False

    def _client(self):
        if self._client is not None:
            return self._client
        if not self._import_ok():
            raise EditorError(
                "Gemini AI configured but the 'google-genai' SDK is not "
                "installed. Install it with: pip install google-genai",
                hint="Or set ai_provider to null to use the deterministic matcher.")
        if not self.api_key:
            raise EditorError(
                "Gemini AI configured but no API key found in environment "
                "variable %r." % self.api_key_env,
                hint="Set it with: export GEMINI_API_KEY=...  (free tier: "
                     "https://aistudio.google.com/apikey)")
        from google import genai
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    # ------------------------------------------------------------------
    # low-level calls
    # ------------------------------------------------------------------
    def _generate_json(self, model, contents, schema):
        """Ask a model for a JSON object; returns parsed dict or None."""
        from google import genai as _g
        client = self._client()
        prompt = contents if isinstance(contents, str) else contents
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=_g.types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.1,
                ))
        except Exception as exc:  # noqa: BLE001
            # Fall back to a plain-text JSON request (some models/regions).
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=_g.types.GenerateContentConfig(
                        temperature=0.1))
                return _extract_json(resp.text)
            except Exception as exc2:  # noqa: BLE001
                raise EditorError(
                    "Gemini model %r call failed: %s" % (model, exc2))
        text = getattr(resp, "text", None) or ""
        parsed = _extract_json(text)
        return parsed

    def embed_texts(self, texts):
        """Return a list of embedding vectors (one per input text)."""
        client = self._client()
        if not texts:
            return []
        resp = client.models.embed_content(
            model=self.embed_model, contents=list(texts))
        return [e.values for e in resp.embeddings]

    # ------------------------------------------------------------------
    # clip descriptions (Gemini vision)
    # ------------------------------------------------------------------
    def describe_clips(self, clips, existing_metadata):
        """Return {file: {"description", "tags"}} for clips.

        Reuses existing metadata.json entries when present (no API cost);
        otherwise sends the clip to the vision model.
        """
        from google.genai import types as _types
        client = self._client()
        out = {}
        for clip in clips:
            name = clip["file"]
            meta = (existing_metadata or {}).get(name) or {}
            if meta.get("description") or meta.get("tags"):
                out[name] = meta
                continue
            path = clip.get("path")
            if not path or not os.path.isfile(path):
                self.warnings.append("AI: clip %r file missing." % name)
                continue
            size = os.path.getsize(path)
            if size > self.max_video_bytes:
                self.warnings.append(
                    "AI: clip %r is %.1f MB (limit %.0f MB); skipped "
                    "vision description." % (name, size / 1048576,
                                             self.max_video_bytes / 1048576))
                continue
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError as exc:
                self.warnings.append("AI: cannot read clip %r: %s" % (name, exc))
                continue
            prompt = (
                "You are a documentary footage librarian. Describe this "
                "video clip in 1-2 factual sentences and return a JSON object "
                "with fields description (string) and tags (array of 3-6 "
                "short keywords). Do not invent history or context that is "
                "not visible."
            )
            try:
                parsed = self._generate_json(
                    self.vision_model,
                    [_types.Part.from_bytes(data=blob, mime_type="video/mp4"),
                     _types.Part(text=prompt)],
                    _DESCRIBE_SCHEMA)
            except EditorError as exc:
                self.warnings.append("AI: description of %r failed: %s"
                                     % (name, exc.message))
                continue
            desc = (parsed or {}).get("description") or ""
            tags = (parsed or {}).get("tags") or []
            if not desc and not tags:
                self.warnings.append("AI: empty description for %r." % name)
                continue
            out[name] = {"description": desc, "tags": list(tags)}
        return out

    # ------------------------------------------------------------------
    # vector index
    # ------------------------------------------------------------------
    def _text_key(self, text, clip_path=None):
        h = hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
        if clip_path and os.path.isfile(clip_path):
            h += "-%d" % int(os.path.getmtime(clip_path))
        return h

    def build_index(self, clip_descriptions, clips, store):
        """Embed each clip's description/tags and cache in the vector store."""
        missing = []
        for clip in clips:
            name = clip["file"]
            meta = clip_descriptions.get(name) or {}
            text = (meta.get("description", "") + " " +
                    " ".join(meta.get("tags", []))).strip().lower()
            if not text:
                self.warnings.append("AI: no description text for %r." % name)
                continue
            key = self._text_key(text, clip.get("path"))
            if store.is_fresh(name, key):
                continue
            missing.append(name)
        if missing:
            vecs = self.embed_texts([(clip_descriptions.get(n) or {}).get(
                "description", "") + " " + " ".join(
                    (clip_descriptions.get(n) or {}).get("tags", []))
                for n in missing])
            for name, vec in zip(missing, vecs):
                text = ((clip_descriptions.get(name) or {}).get("description",
                                                                "") + " " +
                        " ".join((clip_descriptions.get(name) or {}).get(
                            "tags", []))).strip().lower()
                store.put(name, vec, self._text_key(text,
                                                    _path_for(clips, name)))

    # ------------------------------------------------------------------
    # retrieval
    # ------------------------------------------------------------------
    def scene_query_text(self, scene):
        """Build a retrieval query from a scene's text + visual requirements."""
        try:
            from .script import derive_visual_requirements
            kw = " ".join(derive_visual_requirements(scene))
        except Exception:
            kw = ""
        title = scene.get("title") or ""
        text = scene.get("text") or ""
        return ("%s %s %s" % (title, text, kw)).strip()

    def retrieve(self, store, query_text, k=None):
        vecs = self.embed_texts([query_text])
        if not vecs:
            return []
        return store.query(vecs[0], k=k or self.top_k)

    # ------------------------------------------------------------------
    # final decisions (Gemini Pro)
    # ------------------------------------------------------------------
    def finalize_scene(self, scene, candidates, clip_descriptions, all_clips):
        """Ask the decision model for an ordered clip list for one scene.

        Returns an ordered list of clip filenames (may be empty on failure),
        validated to only contain clips that exist in the manifest.
        """
        names = {c["file"] for c in all_clips}
        cand_lines = []
        for name, score in candidates:
            meta = clip_descriptions.get(name) or {}
            desc = meta.get("description", "") or name
            cand_lines.append("- %s (score %.3f): %s" % (name, score, desc))
        prompt = (
            "You are a documentary editor. Choose and ORDER the most fitting "
            "footage clips for the following scene. Return a JSON object: "
            "{\"scene\": <int>, \"clips\": [<filenames in priority order>], "
            "\"rationale\": \"...\"}.\n"
            "You may reuse a clip if needed. Only pick from the candidates. "
            "Do NOT invent filenames.\n\n"
            "SCENE: %s\n"
            "SCENE TEXT: %s\n"
            "CANDIDATES:\n%s"
            % (scene.get("title", ""), scene.get("text", ""),
               "\n".join(cand_lines)))
        try:
            parsed = self._generate_json(self.decision_model, prompt,
                                         _DECISION_SCHEMA)
        except EditorError as exc:
            self.warnings.append("AI: decision for %r failed: %s"
                                 % (scene.get("title"), exc.message))
            return [n for n, _ in candidates]
        chosen = (parsed or {}).get("clips") or []
        # Validate: only existing clips, dedupe, keep order.
        ordered = []
        seen = set()
        for c in chosen:
            if isinstance(c, str) and c in names and c not in seen:
                seen.add(c)
                ordered.append(c)
        if not ordered:
            ordered = [n for n, _ in candidates]
        return ordered

    # ------------------------------------------------------------------
    # orchestration
    # ------------------------------------------------------------------
    def assign_scenes(self, scenes, clips, existing_metadata, store):
        """Run the full AI pipeline -> {scene_index: [clip_file, ...]}.

        Fully validated: any failure short-circuits to a sequential order so
        the caller always has a usable mapping.
        """
        descriptions = self.describe_clips(clips, existing_metadata)
        self.build_index(descriptions, clips, store)
        store.save()

        mapping = {}
        names = {c["file"] for c in clips}
        for scene in scenes:
            query = self.scene_query_text(scene)
            cands = []
            try:
                cands = self.retrieve(store, query)
            except EditorError as exc:
                self.warnings.append("AI: retrieval failed: %s" % exc.message)
            if not cands:
                # fallback for this scene
                mapping[scene["index"]] = [c["file"] for c in clips]
                continue
            ordered = self.finalize_scene(scene, cands, descriptions, clips)
            mapping[scene["index"]] = [n for n in ordered if n in names] or \
                [c["file"] for c in clips]
        return mapping, descriptions


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _path_for(clips, name):
    for c in clips:
        if c["file"] == name:
            return c.get("path")
    return None


def _extract_json(text):
    """Robustly pull the first JSON object out of a model response."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        # Try progressively shrinking to the last valid object.
        for cut in range(end, start, -1):
            try:
                return json.loads(text[start:cut + 1])
            except json.JSONDecodeError:
                continue
    return None
