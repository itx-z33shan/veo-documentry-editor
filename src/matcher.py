"""Assign clips to scenes (INTELLIGENCE layer, deterministic core).

This module decides *what should appear where*. It is fully deterministic and
never depends on an LLM. Two strategies are supported:

* MODE A -- Sequential: use clips in natural filename order.
* MODE B -- Semantic:  match scene visual requirements against optional
  clips/metadata.json descriptions/tags. Falls back to sequential when no
  metadata exists.

Manual timeline overrides always win over anything decided here (handled in
the timeline builder).
"""

import re
import unicodedata

_STOPWORDS = set(
    "the and but or for nor so yet a an of to in on at by with from as into "
    "during over under across through before after between this that these "
    "those it its they their them we our us you your i he she his her who "
    "whom which what when where why how was were is are been being be will "
    "would should could may might shall can have has had not no than then "
    "there here were do does did about against because just most more other "
    "some such up very way".split())


def _tokens(text):
    text = unicodedata.normalize("NFKD", text).lower()
    text = "".join(c for c in text if c.isalnum() or c.isspace())
    words = re.findall(r"[a-z0-9']+", text)
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _clip_terms(clip):
    """Token set for a clip from its description and tags."""
    terms = set()
    if clip.get("description"):
        terms |= _tokens(clip["description"])
    for tag in clip.get("tags") or []:
        terms |= _tokens(tag)
    return terms


def _score(query_terms, clip_terms):
    """How many scene query terms a clip covers (exact overlap)."""
    if not query_terms:
        return 0.0
    return len(query_terms & clip_terms)


def _order_sequential(clips):
    """Return clip names in natural (already sorted) order."""
    return [c["file"] for c in clips]


def _assign_sequential(scenes, clips):
    """MODE A: distribute clips across scenes in natural order."""
    names = _order_sequential(clips)
    c = len(names)
    s = len(scenes)
    mapping = {}

    if s == 0:
        return mapping

    if c >= s:
        base = c // s
        extra = c % s
        idx = 0
        for i, scene in enumerate(scenes):
            n = base + (1 if i < extra else 0)
            mapping[scene["index"]] = names[idx:idx + n]
            idx += n
    else:
        # Fewer clips than scenes: cycle clips so every scene is covered.
        for i, scene in enumerate(scenes):
            mapping[scene["index"]] = [names[i % c]]
    return mapping


def _assign_semantic(scenes, clips):
    """MODE B: score each clip against each scene's visual requirements.

    For every scene, clips are ranked by semantic score (ties broken by
    natural order). Clips may be reused across scenes. Scenes with no
    semantic signal fall back to sequential ordering.
    """
    names = _order_sequential(clips)
    name_set = set(names)
    # Precompute token sets.
    clip_terms = {}
    for clip in clips:
        clip_terms[clip["file"]] = _clip_terms(clip)

    has_semantics = any(clip_terms[c] for c in name_set)
    if not has_semantics:
        return _assign_sequential(scenes, clips)

    mapping = {}
    for scene in scenes:
        query = scene.get("_query_terms")
        if query is None:
            from .script import derive_visual_requirements
            query = set(derive_visual_requirements(scene))
            scene["_query_terms"] = query
        if not query:
            mapping[scene["index"]] = names
            continue
        scored = []
        for c in names:
            scored.append((_score(query, clip_terms[c]), c))
        # Sort by score desc, then natural order asc for stable ties.
        scored.sort(key=lambda t: (-t[0], natural_index(t[1])))
        ordered = [c for _, c in scored]
        mapping[scene["index"]] = ordered
    return mapping


def natural_index(name):
    import re as _re
    nums = _re.findall(r"\d+", name)
    return int(nums[0]) if nums else 0


def assign_clips_to_scenes(scenes, clips, cfg):
    """Return {scene_index: [clip_filename, ...]}.

    Chooses semantic vs sequential based on config.clip_strategy; if a
    semantic match finds nothing usable it falls back to the configured
    fallback strategy automatically.
    """
    strategy = cfg.get("clip_strategy", "sequential")
    fallback = cfg.get("fallback_strategy", "sequential")

    if strategy == "semantic":
        mapping = _assign_semantic(scenes, clips)
        # If no clip got a semantic (non-zero) ranking signal at all, fall back.
        if not _any_semantic_signal(mapping, clips):
            if fallback == "sequential":
                mapping = _assign_sequential(scenes, clips)
    else:
        mapping = _assign_sequential(scenes, clips)
    return mapping


def _any_semantic_signal(mapping, clips):
    for clip in clips:
        if clip.get("description") or clip.get("tags"):
            return True
    return False


def assign_clips_with_ai(scenes, clips, cfg, existing_metadata=None,
                         vector_store=None):
    """INTELLIGENCE via the optional Gemini layer.

    Wraps ``src.ai.GeminiAI``. If AI is unavailable or fails, returns the
    deterministic mapping plus warnings. ``vector_store`` is a
    ``src.vectorstore.VectorStore`` (created if None).
    """
    from .ai import GeminiAI
    from .vectorstore import VectorStore

    ai = GeminiAI(cfg)
    if not ai.available():
        return assign_clips_to_scenes(scenes, clips, cfg), \
            ["AI not available (no key/SDK); using deterministic matching."]

    db_path = cfg.get("ai_vector_db_path") or "output/clip_vectors.json"
    store = vector_store or VectorStore(db_path).load()

    try:
        mapping, _descriptions = ai.assign_scenes(
            scenes, clips, existing_metadata or {}, store)
    except Exception as exc:  # noqa: BLE001
        mapping = assign_clips_to_scenes(scenes, clips, cfg)
        return mapping, ["AI pipeline failed (%s); using deterministic "
                         "matching." % exc]

    return mapping, ai.warnings


def summarize(mapping):
    """Human-readable summary for reports: scene -> assigned clip count."""
    return {k: len(v) for k, v in mapping.items()}
