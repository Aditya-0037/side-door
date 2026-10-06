"""Taste Lift demo: resolve taste seeds, rank the verified roster with and without them, print the jump.

Usage:
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/demo_rank.py --seeds "Spirited Away|Arijit Singh|Valorant" --sport f1
    (optional) --exclude "Lewis Hamilton|Max Verstappen"   # repair path: "I already know them"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.rosters import load_rosters, name_tokens  # noqa: E402
from backend.qloo.client import QlooClient  # noqa: E402
from backend.qloo.seeds import resolve_seed  # noqa: E402
from backend.ranking.taste_lift import DEFAULT_ALPHA, DEFAULT_BETA, NO_DATA_TITLE, compute_taste_lift  # noqa: E402


def share_text(shares: dict[str, float], short: dict[str, str]) -> str:
    """'Valorant 45%  Spirited 30%  Arijit 25%' -- what the UI's contribution bar will show."""
    parts = sorted(shares.items(), key=lambda kv: (-kv[1], kv[0]))
    return "  ".join(f"{short.get(s, s[:8])} {v:.0%}" for s, v in parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", required=True, help='e.g. "Spirited Away|Arijit Singh|Valorant"')
    parser.add_argument("--sport", choices=["f1", "pl"], default="f1")
    parser.add_argument("--exclude", default="", help='athlete names to drop, e.g. "Lewis Hamilton"')
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    parser.add_argument("--beta", type=float, default=DEFAULT_BETA)
    args = parser.parse_args()

    client = QlooClient.dev()
    calls_before = client.budget.used

    print("Seeds:")
    seeds: dict[str, str] = {}
    for text in filter(None, (s.strip() for s in args.seeds.split("|"))):
        hit = resolve_seed(client, text)
        if hit is None:
            print(f"  {text:24s} -> NOT FOUND (skipped)")
            continue
        seeds[hit.entity_id] = hit.name
        print(f"  {text:24s} -> {hit.name} [{hit.types[0]}] {hit.entity_id}")
    if not seeds:
        sys.exit("No seed resolved; nothing to rank.")
    short = {sid: name.split()[0][:10] for sid, name in seeds.items()}

    roster = load_rosters(sport=args.sport)
    teams = {a.qloo_id: a.team for a in roster if a.qloo_id}
    names = {a.qloo_id: a.name for a in roster if a.qloo_id}
    by_name = {name_tokens(a.name): a.qloo_id for a in roster}
    exclude = {by_name[name_tokens(n)] for n in args.exclude.split("|") if n.strip() and name_tokens(n) in by_name}
    print(f"\nRoster: {len(teams)} verified {args.sport.upper()} athletes"
          + (f"; excluding {[names[i] for i in exclude]}" if exclude else ""))

    personal = client.insights_people(list(seeds), list(teams), explain=True, exclude_ids=exclude)
    baseline = client.baseline_people(list(teams))
    result = compute_taste_lift(personal, baseline, teams, alpha=args.alpha, beta=args.beta, exclude_ids=exclude)

    print(f"\nTaste Lift (alpha={args.alpha}, beta={args.beta}, N={result.n} scored)")
    print(f"  {'#':>2}  {'athlete':22s} {'team':18s} {'crowd -> you':>12s}  {'lift':>4s}  {'final':>6s}  "
          f"{'distinctive':12s} seed shares")
    for a in result.ranked:
        seed = short.get(a.distinctive_seed, "-") if a.distinctive_seed else "-"
        print(f"  {a.rank:>2}  {a.name:22s} {(a.team or '?'):18s} {a.crowd_rank:>5} -> {a.personal_rank:<4}"
              f"  {a.lift:+4d}  {a.final:6.3f}  {seed:12s} {share_text(a.seed_shares, short)}")
    print("\n  Why (top 3):")
    for item in result.to_payload(seeds)["ranked"][:3]:
        extra = f"  [distinctive: {item['distinctive_seed_name']}]" if "distinctive_seed_name" in item else ""
        print(f"    {item['why_line']}{extra}")
    if result.unknown:
        print(f"\n  {NO_DATA_TITLE}:")
        for u in result.unknown:
            print(f"    {names.get(u.entity_id, u.name)} ({u.team})")
    print(f"\nQloo calls this run: {client.budget.used - calls_before} (today: {client.budget.used}/{client.budget.limit})")


if __name__ == "__main__":
    main()
