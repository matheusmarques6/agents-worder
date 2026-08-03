"""A deterministic embedder — the stand-in the knowledge suite retrieves with.

Real embeddings cost money, need a key and change when the provider retrains;
none of that belongs in a suite that has to answer one question: does
similarity search return the right chunk first? So the double maps words to
slots and normalises, which makes cosine similarity a plain measure of word
overlap — identical texts score 1.0, texts with nothing in common score 0.0,
and the ordering a test asserts is arithmetic instead of vibes.

It lives under `tests/` because a test double never ships inside the runtime
image (decisão 8). The real embedder is `LlmPort.embed` (S5), injected at the
call site, and the only thing the two must agree on is the DIMENSION — 1536,
frozen into the schema by D2 and confirmed against the provider itself by the
`contract` suite.
"""

import hashlib
import math
from collections.abc import Sequence

#: D2. Same number as `vector(1536)` in the migration of S2 — a double with a
#: different dimension would fail at INSERT and prove nothing about retrieval.
DIMENSIONS = 1536

_SPLIT = str.maketrans({character: " " for character in ".,;:!?()[]\"'"})


def _words(text: str) -> list[str]:
    return [word for word in text.lower().translate(_SPLIT).split() if word]


def _slot(word: str) -> int:
    # sha256, not hash(): Python's hash is salted per process, so the same word
    # would land in a different slot on every run and the suite would be a coin
    # toss. This is determinism, not cryptography.
    return int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % DIMENSIONS


def embed_text(text: str) -> tuple[float, ...]:
    """One unit vector per text. Overlapping words → higher cosine similarity."""
    weights: dict[int, float] = {}
    for word in _words(text):
        slot = _slot(word)
        weights[slot] = weights.get(slot, 0.0) + 1.0

    norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0

    vector = [0.0] * DIMENSIONS
    for slot, weight in weights.items():
        vector[slot] = weight / norm
    return tuple(vector)


def embed_all(texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
    return tuple(embed_text(text) for text in texts)
