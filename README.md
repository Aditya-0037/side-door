# Side Door

**Get into a sport through the side door — your taste in films, music and games.**

Side Door is an agent for the Qloo Agentic Hackathon. A newcomer tells it what they already love
(say *Spirited Away*, *Arijit Singh* and *Valorant*), and it uses Qloo's taste graph to find the
Formula 1 drivers or Premier League players they're most likely to click with.

The headline feature is **Taste Lift**. We rank the same fixed set of current athletes twice — once
with no signal (the "everyone already knows them" baseline) and once with the user's taste signal —
and show who jumps up the most, together with which of the user's seeds pulled them up.

> Status: foundation only (verified rosters, Qloo client, Taste Lift, tests). The agent and UI come next.

## Setup (Windows / PowerShell)

```powershell
py -3 -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
notepad .env          # one line: QLOO_API_KEY=<your key>   (never commit this file)
$env:PYTHONUTF8="1"   # athlete names contain characters like ø / ć
.venv\Scripts\python.exe -m pytest -q
```

### Environment variables

| Name | Required | Meaning |
|---|---|---|
| `QLOO_API_KEY` | yes | Qloo hackathon API key, read from `.env` via python-dotenv. Never logged. |
| `QLOO_DAILY_BUDGET` | no | Max live Qloo calls per day across all scripts (default 150). |

## Layout

```
backend/qloo/        client.py (throttle, retry, cache, budget), models.py, cache.py, seeds.py
backend/ranking/     taste_lift.py (pure scoring), sweep.py (param sweep), bias.py (popularity-bias check)
backend/inventory/   rosters.py (Jolpica grid, evidence tiers for Qloo matches)
data/                f1_grid.json (Jolpica snapshot), pl_seed.json (manually curated), roster_overrides.json,
                     rosters.json (verified Qloo IDs + evidence tier), personas_eval.json,
                     sweep_inputs.json / sweep_results.json (parameter sweep)
scripts/             refresh_f1_grid.py, verify_rosters.py, demo_rank.py, param_sweep.py, bias_check.py,
                     smoke_compare.py, kill_tests.py
tests/               pytest suite (no live API) + fixtures/ (raw Qloo responses, no keys)
```

## Scripts

```powershell
.venv\Scripts\python.exe scripts/refresh_f1_grid.py           # latest race lineup -> data/f1_grid.json (nightly later)
.venv\Scripts\python.exe scripts/verify_rosters.py            # resolve rosters -> data/rosters.json
.venv\Scripts\python.exe scripts/demo_rank.py --seeds "Spirited Away|Arijit Singh|Valorant" --sport f1
.venv\Scripts\python.exe scripts/param_sweep.py --offline     # recompute the alpha/beta sweep, no API calls
.venv\Scripts\python.exe scripts/bias_check.py                # Day-1 popularity-bias analysis (fixtures only)
```

Roster matches are accepted at **tier 1** (current club in the candidate's team list) or **tier 2**
(sport + nationality + plausible date of birth); anything else needs review. Popularity is never
used to choose between namesakes.

Live responses are cached in `.cache/`, and every script shares one daily call budget.

## Data & attributions

- **Qloo** Taste AI API — taste graph, entity search and insights (hackathon use).
- **Jolpica-F1** (https://github.com/jolpica/jolpica-f1) — current F1 grid and teams. Data licensed
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
- **football-data.org** — Premier League data source (planned). Squads aren't on the free tier, so
  `data/pl_seed.json` is currently a manually curated seed list.
- **Wikidata** — entity facts and categories surfaced through Qloo tags, [CC0](https://creativecommons.org/publicdomain/zero/1.0/).

## License

MIT, see [LICENSE](LICENSE).
