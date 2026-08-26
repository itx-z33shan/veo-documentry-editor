"""Tiny local vector store (no external DB dependency).

Holds clip embedding vectors on disk as JSON keyed by clip filename, keyed to
the source content hash so stale entries are invalidated. Retrieval is
brute-force cosine similarity, which is instant for a few hundred clips and
keeps the "local vector DB" zero-dependency.
"""

import json
import math
import os


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class VectorStore:
    def __init__(self, path):
        self.path = path
        self._vectors = {}   # file -> {"vec": [...], "key": str}

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return self
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._vectors = data.get("vectors", {})
        except (OSError, json.JSONDecodeError):
            self._vectors = {}
        return self

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"vectors": self._vectors}, fh)
        os.replace(tmp, self.path)

    def get(self, file):
        entry = self._vectors.get(file)
        return entry["vec"] if entry else None

    def is_fresh(self, file, key):
        entry = self._vectors.get(file)
        return bool(entry and entry.get("key") == key)

    def put(self, file, vec, key):
        self._vectors[file] = {"vec": vec, "key": key}

    def query(self, query_vec, k=5):
        """Return top-k [(file, score)] by cosine similarity, descending."""
        scored = []
        for file, entry in self._vectors.items():
            score = cosine_similarity(query_vec, entry["vec"])
            scored.append((file, score))
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored[:k]

    def __len__(self):
        return len(self._vectors)
