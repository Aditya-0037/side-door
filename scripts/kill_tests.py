"""
Side Door - Day-1 kill tests (K1-K7) for the Qloo hackathon API.

Usage:
    pip install requests python-dotenv
    # put QLOO_API_KEY=xxxx in a .env file in the repo root (never commit it)
    python scripts/kill_tests.py      # run from the repo root

Every raw response is saved in tests/fixtures/ and a summary in tests/fixtures/summary.json.
Share only the printed SUMMARY (never your key).
"""

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = "https://hackathon.api.qloo.com"
KEY = os.getenv("QLOO_API_KEY")
FIX = Path("tests/fixtures")
FIX.mkdir(parents=True, exist_ok=True)
MIN_GAP = 1 / 3  # max ~3 requests per second
_last_call = 0.0

if not KEY:
    sys.exit("QLOO_API_KEY not set. Put it in .env or export it first.")

# ---------------------------------------------------------------- helpers


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def norm(name):
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def get(path, params, save_as=None, retries=4):
    """Throttled GET with backoff on 429. Returns (status, json_or_text)."""
    global _last_call
    for attempt in range(retries):
        wait = MIN_GAP - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        r = requests.get(BASE + path, params=params,
                         headers={"X-Api-Key": KEY}, timeout=30)
        if r.status_code == 429:
            time.sleep(0.5 * 2 ** attempt)
            continue
        break
    try:
        body = r.json()
    except ValueError:
        body = r.text
    if save_as:
        (FIX / f"{save_as}.json").write_text(
            json.dumps({"status": r.status_code, "params": params, "body": body},
                       indent=2, ensure_ascii=False))
    return r.status_code, body


def search_results(body):
    if isinstance(body, dict):
        res = body.get("results", body.get("entities", []))
        if isinstance(res, dict):
            res = res.get("entities", [])
        return res if isinstance(res, list) else []
    return []


def ent_id(e):
    return e.get("entity_id") or e.get("id")


def ent_types(e):
    t = e.get("types") or e.get("type") or e.get("subtype") or []
    return t if isinstance(t, list) else [t]


def insight_entities(body):
    if isinstance(body, dict):
        res = body.get("results", {})
        if isinstance(res, dict):
            return res.get("entities", [])
        if isinstance(res, list):
            return res
    return []


def affinity(e):
    q = e.get("query") or {}
    return q.get("affinity", e.get("affinity"))


def search(name, types=None, save_as=None, take=5):
    params = {"query": name, "take": take}
    if types:
        params["types"] = types
    status, body = get("/search", params, save_as)
    results = search_results(body)
    if types and not results:  # fallback: no type filter, filter client-side
        params.pop("types")
        status, body = get("/search", params, (save_as or "") + "-notype" if save_as else None)
        results = [e for e in search_results(body)
                   if any(types in str(t) for t in ent_types(e))] or search_results(body)
    return status, results


def insights(params, save_as):
    base = {"take": 50}
    base.update(params)
    return get("/v2/insights", base, save_as)


# ---------------------------------------------------------------- test data

F1 = ["Max Verstappen", "Lewis Hamilton", "Charles Leclerc", "Lando Norris",
      "Oscar Piastri", "George Russell", "Kimi Antonelli", "Fernando Alonso",
      "Lance Stroll", "Pierre Gasly", "Alexander Albon", "Carlos Sainz Jr.",
      "Esteban Ocon", "Oliver Bearman", "Nico Hulkenberg", "Gabriel Bortoleto",
      "Liam Lawson", "Isack Hadjar", "Sergio Perez", "Yuki Tsunoda"]
PL = ["Mohamed Salah", "Erling Haaland", "Bukayo Saka", "Cole Palmer",
      "Bruno Fernandes", "Virgil van Dijk", "Martin Odegaard", "Declan Rice",
      "Phil Foden", "Son Heung-min"]
NBA = ["LeBron James", "Stephen Curry", "Giannis Antetokounmpo",
       "Nikola Jokic", "Victor Wembanyama"]
CRICKET = ["Virat Kohli", "Rohit Sharma", "Jasprit Bumrah", "MS Dhoni",
           "Smriti Mandhana"]
TEAMS = ["Chelsea FC", "Manchester United", "Arsenal FC", "Liverpool FC",
         "McLaren F1 Team"]

MOVIE, ARTIST, GAME = "urn:entity:movie", "urn:entity:artist", "urn:entity:videogame"
PERSONAS = {
    "anime-indie":  [("Spirited Away", MOVIE), ("Arijit Singh", ARTIST), ("Valorant", GAME)],
    "space-pop":    [("Interstellar", MOVIE), ("Coldplay", ARTIST), ("Minecraft", GAME)],
    "dark-hiphop":  [("The Dark Knight", MOVIE), ("Eminem", ARTIST), ("Grand Theft Auto V", GAME)],
    "romance-pop":  [("Pride and Prejudice", MOVIE), ("Taylor Swift", ARTIST), ("The Sims 4", GAME)],
    "rock-action":  [("Rocky", MOVIE), ("AC/DC", ARTIST), ("Call of Duty", GAME)],
}

summary = {}
PERSON = "urn:entity:person"

# ---------------------------------------------------------------- K0 sanity
print("K0 sanity: search 'Inception' ...")
st, res = search("Inception", MOVIE, "k0-inception")
summary["K0_sanity"] = {"status": st, "results": len(res)}
print(f"   status {st}, {len(res)} results")
if st != 200:
    sys.exit("Sanity call failed. Check key / host. See tests/fixtures/k0-inception.json")

# ---------------------------------------------------------------- K1 athlete coverage
print("\nK1 athlete coverage (40 names) ...")
resolved, pool = {}, set()
k1_rows = []
for group, names in [("f1", F1), ("pl", PL), ("nba", NBA), ("cricket", CRICKET)]:
    for name in names:
        st, res = search(name, PERSON, f"k1-{slug(name)}")
        for e in res:
            if ent_id(e):
                pool.add(ent_id(e))
        top = res[0] if res else None
        exact = bool(top) and norm(top.get("name")) == norm(name)
        loose = bool(top) and norm(name).split()[-1] in norm(top.get("name"))
        if top and (exact or loose):
            resolved[name] = ent_id(top)
        k1_rows.append((group, name, top.get("name") if top else None, exact, loose))
        mark = "OK " if exact else ("~  " if loose else "XX ")
        print(f"   {mark}{name:24s} -> {top.get('name') if top else '-'}")
exact_n = sum(1 for r in k1_rows if r[3])
loose_n = sum(1 for r in k1_rows if r[3] or r[4])
summary["K1_coverage"] = {"exact": exact_n, "exact_or_loose": loose_n, "total": len(k1_rows),
                          "by_group": {g: sum(1 for r in k1_rows if r[0] == g and (r[3] or r[4]))
                                       for g in ["f1", "pl", "nba", "cricket"]},
                          "pass": loose_n >= 32}
print(f"   => {exact_n} exact, {loose_n} exact+loose of {len(k1_rows)} (pass gate: 32)")

f1_ids = [resolved[n] for n in F1 if n in resolved]

# ---------------------------------------------------------------- resolve personas
print("\nResolving persona seeds ...")
persona_ids = {}
for p, seeds in PERSONAS.items():
    ids = []
    for name, t in seeds:
        st, res = search(name, t, f"seed-{slug(name)}")
        if res and ent_id(res[0]):
            ids.append(ent_id(res[0]))
            print(f"   {p:12s} {name:22s} -> {res[0].get('name')}")
        else:
            print(f"   {p:12s} {name:22s} -> NOT FOUND")
    persona_ids[p] = ids

# ---------------------------------------------------------------- K2 ranking works
print("\nK2 ranking within fixed F1 set ...")
first = next(iter(persona_ids))
st, body = insights({"filter.type": PERSON,
                     "signal.interests.entities": ",".join(persona_ids[first]),
                     "filter.results.entities": ",".join(f1_ids),
                     "feature.explainability": "true",
                     "bias.trends": "off"}, "k2-insights")
ents = insight_entities(body)
affs = [affinity(e) for e in ents if affinity(e) is not None]
outside = [ent_id(e) for e in ents if ent_id(e) not in f1_ids]
summary["K2_ranking"] = {"status": st, "results": len(ents), "distinct_affinities": len(set(affs)),
                         "outside_set": len(outside),
                         "pass": st == 200 and len(ents) >= min(15, len(f1_ids)) and len(set(affs)) > 1
                         and not outside}
print(f"   status {st}, {len(ents)} results, {len(set(affs))} distinct affinities, "
      f"{len(outside)} outside the allowed set")
if st != 200:
    print("   (check tests/fixtures/k2-insights.json for the error message)")

# ---------------------------------------------------------------- K6 explainability
txt = json.dumps(body).lower()
has_expl = "explain" in txt or "contribution" in txt
summary["K6_explainability"] = {"payload_found": has_expl}
print(f"\nK6 explainability payload found: {has_expl}")

# ---------------------------------------------------------------- K3 lift exists
print("\nK3 different personas -> different picks? ...")
tops = {}
for p, ids in persona_ids.items():
    st, body = insights({"filter.type": PERSON,
                         "signal.interests.entities": ",".join(ids),
                         "filter.results.entities": ",".join(f1_ids),
                         "bias.trends": "off"}, f"k3-{p}")
    e = insight_entities(body)
    tops[p] = [x.get("name") for x in e[:3]]
    print(f"   {p:12s} top-3: {tops[p]}")
st, body = insights({"filter.type": PERSON,
                     "filter.results.entities": ",".join(f1_ids)}, "k3-baseline")
base = [x.get("name") for x in insight_entities(body)]
print(f"   baseline (no signal) status {st}, top-3: {base[:3]}")
top1 = [t[0] for t in tops.values() if t]
distinct = len(set(top1))
not_base1 = sum(1 for t in top1 if base and t != base[0])
summary["K3_lift"] = {"top1_per_persona": {p: (t[0] if t else None) for p, t in tops.items()},
                      "baseline_status": st, "baseline_top3": base[:3],
                      "distinct_top1": distinct, "top1_not_baseline1": not_base1,
                      "pass": distinct >= 3 and (not base or not_base1 >= 3)}
print(f"   => {distinct} distinct top-1 picks; {not_base1} differ from baseline #1")

# ---------------------------------------------------------------- K4 teams
print("\nK4 teams ...")
k4 = {}
for team in TEAMS:
    st, res = search(team, None, f"k4-{slug(team)}")
    top = res[0] if res else {}
    k4[team] = {"top": top.get("name"), "types": ent_types(top)}
    print(f"   {team:20s} -> {top.get('name')}  {ent_types(top)}")
summary["K4_teams"] = k4

# ---------------------------------------------------------------- K5 set size
print("\nK5 filter.results.entities size ...")
pool = list(pool)
k5 = {}
for n in [25, 50, 100]:
    if len(pool) < n:
        k5[n] = "not enough IDs in pool"
        print(f"   {n}: skipped (pool has {len(pool)})")
        continue
    st, body = insights({"filter.type": PERSON,
                         "signal.interests.entities": ",".join(persona_ids[first]),
                         "filter.results.entities": ",".join(pool[:n])}, f"k5-{n}")
    k5[n] = {"status": st, "results": len(insight_entities(body))}
    print(f"   {n}: status {st}, {len(insight_entities(body))} results")
summary["K5_set_size"] = k5

# ---------------------------------------------------------------- K7 throughput
N = int(os.getenv("K7_CALLS", "60"))
print(f"\nK7 throughput: {N} calls at ~3 req/s ...")
codes = []
for i in range(N):
    st, _ = get("/search", {"query": "Interstellar", "take": 1})
    codes.append(st)
n429 = codes.count(429)
summary["K7_throughput"] = {"calls": N, "429s": n429, "non_200": sum(c != 200 for c in codes),
                            "pass": n429 == 0}
print(f"   429s: {n429}, non-200: {sum(c != 200 for c in codes)}")

# ---------------------------------------------------------------- summary
(FIX / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
print("\n================ SUMMARY ================")
print(json.dumps(summary, indent=2, ensure_ascii=False))
print("\nRaw responses: tests/fixtures/   (share the SUMMARY, never your key)")
