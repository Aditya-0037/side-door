"""Refresh data/athlete_media.json: each verified athlete's YouTube channel (Wikidata P2397) and
official website (P856), plus the leagues' official channels. Every channel's uploads playlist
is validated through YouTube oEmbed (no API key, no quota).

Wikimedia requires contact info in the User-Agent, so set WIKIMEDIA_CONTACT first:
    $env:WIKIMEDIA_CONTACT="https://github.com/<you>/side-door"; $env:PYTHONUTF8="1"
    .venv\\Scripts\\python.exe scripts/refresh_media.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.clips import (  # noqa: E402
    MEDIA_PATH, Wikidata, league_channels, match_athlete, verify_uploads_playlist, wikimedia_user_agent,
)
from backend.inventory.rosters import load_rosters  # noqa: E402


def main() -> None:
    try:
        wd = Wikidata(wikimedia_user_agent())
    except RuntimeError as exc:
        sys.exit(str(exc))

    athletes, oembed_checks = {}, 0
    for a in load_rosters():
        rec = match_athlete(wd, a)
        if rec["youtube_channel_id"]:
            rec["uploads_playlist_title"] = verify_uploads_playlist(rec["youtube_channel_id"])
            oembed_checks += 1
            if not rec["uploads_playlist_title"]:  # channel id on Wikidata but no live uploads playlist
                rec["youtube_channel_id"] = None
        athletes[a.qloo_id] = rec
        print(f"  {a.sport} {a.name:24s} {rec['match']:32s} {rec['wikidata_id'] or '-':10s} "
              f"yt={rec.get('uploads_playlist_title') or '-'}")
    leagues = league_channels(wd)
    for league in leagues.values():
        if league["youtube_channel_id"]:
            league["uploads_playlist_title"] = verify_uploads_playlist(league["youtube_channel_id"])
            oembed_checks += 1

    MEDIA_PATH.write_text(json.dumps({
        "generated_on": date.today().isoformat(),
        "source": "Wikidata (CC0): P2397 YouTube channel ID, P856 official website; matched by name + exact DOB. "
                  "Uploads playlists validated via YouTube oEmbed.",
        "leagues": leagues, "athletes": athletes,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for sport in ("f1", "pl"):
        recs = [r for r in athletes.values() if r["sport"] == sport]
        print(f"{sport}: {sum(bool(r['wikidata_id']) for r in recs)}/{len(recs)} matched on Wikidata, "
              f"{sum(bool(r['youtube_channel_id']) for r in recs)} own YouTube channel, "
              f"{sum(bool(r['official_website']) for r in recs)} official website; "
              f"league channel: {leagues[sport].get('uploads_playlist_title') or 'none'}")
    print(f"Wikidata requests: {wd.calls}; oEmbed checks: {oembed_checks}; wrote {MEDIA_PATH.name}")


if __name__ == "__main__":
    main()
