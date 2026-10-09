"""Official clips with zero YouTube API quota.

Each athlete's own YouTube channel (Wikidata P2397) becomes an uploads-playlist embed
(channel "UC..." -> playlist "UU..."); otherwise we fall back to the league's official channel.
Official websites (P856) are added when present. Video IDs are never invented.
Wikidata items are matched by name search + exact date of birth, so namesakes can't slip in.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from backend.inventory.rosters import ROOT, RosterAthlete

MEDIA_PATH = ROOT / "data" / "athlete_media.json"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
YOUTUBE_OEMBED = "https://www.youtube.com/oembed"
LEAGUE_ITEMS = {"f1": ("Q1968", "Formula 1"), "pl": ("Q9448", "Premier League")}


def wikimedia_user_agent() -> str:
    """Wikimedia's robot policy rejects clients without contact info (403), so the operator must
    provide one (a repo URL or an email they're happy to share) in WIKIMEDIA_CONTACT."""
    contact = os.environ.get("WIKIMEDIA_CONTACT", "").strip()
    if not contact:
        raise RuntimeError("Set WIKIMEDIA_CONTACT (your repo URL or a contact email) to query Wikidata.")
    return f"SideDoor/0.1 ({contact}) python-httpx/{httpx.__version__}"


def verify_uploads_playlist(channel_id: str, http_get: Callable[..., httpx.Response] = httpx.get) -> str | None:
    """Playlist title via YouTube oEmbed (no API key, no quota): 200 for a real uploads playlist,
    404 for a bogus one. The embed page itself returns 200 either way, so it can't be used."""
    url = uploads_embed_url(channel_id)
    if not url:
        return None
    resp = http_get(YOUTUBE_OEMBED, params={"url": f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}",
                                            "format": "json"}, timeout=30)
    return resp.json().get("title") if resp.status_code == 200 else None
_CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")


@dataclass(frozen=True)
class Clip:
    kind: str  # "athlete_channel" | "league_channel" | "website"
    label: str
    url: str
    embed_url: str | None = None


def uploads_embed_url(channel_id: str) -> str | None:
    """'UCxxxx' -> embed of the channel's uploads playlist 'UUxxxx'; None for anything else."""
    if not _CHANNEL_ID.fullmatch(channel_id or ""):
        return None
    return f"https://www.youtube.com/embed/videoseries?list=UU{channel_id[2:]}"


def channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def load_media(path: Path = MEDIA_PATH) -> dict[str, Any]:
    if not Path(path).exists():
        return {"athletes": {}, "leagues": {}}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def clips_for(athlete_id: str, sport: str, media: dict[str, Any], name: str | None = None) -> list[Clip]:
    """Athlete channel if known, else the league channel; plus the official website if known."""
    info = media.get("athletes", {}).get(athlete_id, {})
    clips: list[Clip] = []
    cid = info.get("youtube_channel_id")
    if cid and uploads_embed_url(cid):
        clips.append(Clip("athlete_channel", f"{name or info.get('name') or 'Athlete'} on YouTube",
                          channel_url(cid), uploads_embed_url(cid)))
    else:
        league = media.get("leagues", {}).get(sport, {})
        lcid = league.get("youtube_channel_id")
        if lcid and uploads_embed_url(lcid):
            clips.append(Clip("league_channel", f"{league.get('label', sport.upper())} on YouTube",
                              channel_url(lcid), uploads_embed_url(lcid)))
    if info.get("official_website"):
        clips.append(Clip("website", "Official website", info["official_website"]))
    return clips


# ---------------------------------------------------------------- Wikidata (refresh step)


def _claim_values(entity: dict[str, Any], prop: str) -> list[Any]:
    values = []
    for claim in entity.get("claims", {}).get(prop, []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if value is not None:
            values.append(value)
    return values


def _dob(entity: dict[str, Any]) -> str | None:
    for v in _claim_values(entity, "P569"):
        if isinstance(v, dict) and str(v.get("time", "")).startswith("+"):
            return v["time"][1:11]  # "+1997-09-30T00:00:00Z" -> "1997-09-30"
    return None


class Wikidata:
    def __init__(self, user_agent: str, http_get: Callable[..., httpx.Response] = httpx.get,
                 min_gap_s: float = 0.25) -> None:
        self._ua = user_agent
        self._get = http_get
        self._gap = min_gap_s
        self._last = 0.0
        self.calls = 0

    def _api(self, **params: str) -> dict[str, Any]:
        wait = self._gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        resp = self._get(WIKIDATA_API, params={"format": "json", **params},
                         headers={"User-Agent": self._ua}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search(self, name: str, limit: int = 7) -> list[str]:
        data = self._api(action="wbsearchentities", search=name, language="en", type="item", limit=str(limit))
        return [hit["id"] for hit in data.get("search", [])]

    def entities(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 50):
            data = self._api(action="wbgetentities", ids="|".join(ids[i:i + 50]), props="claims|labels")
            out.update(data.get("entities", {}))
        return out


def match_athlete(wd: Wikidata, athlete: RosterAthlete) -> dict[str, Any]:
    """Find the Wikidata item with the athlete's name AND exact date of birth."""
    record: dict[str, Any] = {"name": athlete.name, "sport": athlete.sport, "wikidata_id": None,
                              "youtube_channel_id": None, "official_website": None, "match": "no_dob"}
    if not athlete.dob:
        return record
    names = list(dict.fromkeys(n for n in (athlete.qloo_name, athlete.name) if n))
    for name in names:
        ids = wd.search(name)
        hits = [(qid, e) for qid, e in wd.entities(ids).items() if _dob(e) == athlete.dob] if ids else []
        if len(hits) == 1:
            qid, entity = hits[0]
            channels = [v for v in _claim_values(entity, "P2397") if isinstance(v, str) and _CHANNEL_ID.fullmatch(v)]
            sites = [v for v in _claim_values(entity, "P856") if isinstance(v, str)]
            record.update(wikidata_id=qid, youtube_channel_id=channels[0] if channels else None,
                          official_website=sites[0] if sites else None, match=f"name+dob ({name})")
            return record
        if len(hits) > 1:
            record["match"] = "ambiguous"
            return record
    record["match"] = "not_found"
    return record


def league_channels(wd: Wikidata) -> dict[str, dict[str, Any]]:
    entities = wd.entities([qid for qid, _ in LEAGUE_ITEMS.values()])
    out = {}
    for sport, (qid, label) in LEAGUE_ITEMS.items():
        channels = [v for v in _claim_values(entities.get(qid, {}), "P2397")
                    if isinstance(v, str) and _CHANNEL_ID.fullmatch(v)]
        out[sport] = {"wikidata_id": qid, "label": label, "youtube_channel_id": channels[0] if channels else None}
    return out


def clip_dicts(clips: Sequence[Clip]) -> list[dict[str, Any]]:
    return [asdict(c) for c in clips]
