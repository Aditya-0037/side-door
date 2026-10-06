"""Resolving a user's taste seeds (films, artists, games) to Qloo entities."""

from __future__ import annotations

from backend.qloo.client import QlooClient
from backend.qloo.models import SearchResult

SEED_TYPES = {"movie": "urn:entity:movie", "artist": "urn:entity:artist", "videogame": "urn:entity:videogame"}


def resolve_seed(client: QlooClient, text: str, kind: str | None = None) -> SearchResult | None:
    """Qloo's top result for the seed. Typed (take=5, same params as the Day-1 fixtures) when the
    kind is known, else one untyped search keeping the first movie/artist/videogame.

    We trust Qloo's relevance order rather than exact-name matching: "Pride and Prejudice" must
    resolve to the 2005 film ("Pride & Prejudice"), not the exact-titled 1940 one.
    """
    if kind:
        results = client.search(text, types=SEED_TYPES[kind], take=5)
        return results[0] if results else None
    allowed = set(SEED_TYPES.values())
    return next((r for r in client.search(text, take=10) if allowed & set(r.types)), None)
