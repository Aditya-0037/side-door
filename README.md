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
| `GOOGLE_API_KEY` | for narration / Without-Qloo | Gemini (AI Studio) key used by google-genai and Google ADK. Without it the main flow still works; narration and the LLM baseline report "unavailable". |
| `GOOGLE_GENAI_USE_VERTEXAI` | no | `FALSE` for an AI Studio key. |
| `GEMINI_MODEL` | no | Flash-class model for the agent and baseline (default `gemini-2.5-flash`). |
| `FOOTBALL_DATA_API_KEY` | for PL fixtures | football-data.org key; without it PL cards have no next match (never a fake time). |
| `WIKIMEDIA_CONTACT` | for `refresh_media.py` | Repo URL or email for the Wikidata User-Agent (Wikimedia rejects requests without contact info). |
| `SIDE_DOOR_PROD_ORIGIN` | no | Extra CORS origin for the deployed frontend (`http://localhost:5173` is always allowed). |
| `RATE_LIMIT_PER_MIN` | no | Per-IP request limit for `/api/*` (default 30). |

## Run locally

```powershell
$env:PYTHONUTF8="1"
.venv\Scripts\python.exe scripts/refresh_media.py        # optional: athlete YouTube channels (needs WIKIMEDIA_CONTACT)
.venv\Scripts\python.exe scripts/precompute_presets.py   # 6 presets x 2 sports -> data/precomputed/
.venv\Scripts\python.exe -m uvicorn backend.main:app --reload
```

Example calls (Git Bash / curl):

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/presets
curl -X POST http://127.0.0.1:8000/api/resolve -H "Content-Type: application/json" \
  -d '{"items":[{"text":"Inception","type":"movie"},{"text":"A.R. Rahman","type":"artist"},{"text":"Rocket League","type":"videogame"}]}'
curl -X POST http://127.0.0.1:8000/api/side-door -H "Content-Type: application/json" \
  -d '{"seed_ids":["<id>","<id>","<id>"],"sport":"f1","window":"evenings"}'
curl -X POST http://127.0.0.1:8000/api/without-qloo -H "Content-Type: application/json" \
  -d '{"seed_names":["Spirited Away","Arijit Singh","Valorant"],"sport":"f1"}'
curl -X POST http://127.0.0.1:8000/api/repair -H "Content-Type: application/json" \
  -d '{"seed_ids":["<id>","<id>","<id>"],"sport":"f1","window":"evenings","rejected_ids":["<athlete id>"],"reason":"not_feeling_it"}'
curl http://127.0.0.1:8000/api/insights
```

Windows: `evenings` (weekdays 19:00–23:59 IST, weekends 12:00–23:59), `late_night_ok` (17:00–02:59),
`weekends_only`. Repair reasons: `not_feeling_it` (re-rank without them), `bad_time` (best match whose
next event fits the window; add `"allow_sport_switch": true` to allow the other sport).

The main flow (`/api/side-door`) never calls the LLM. `/api/repair` runs a Google ADK agent over the
same tool functions with a 12 s timeout; if Gemini fails, the deterministic swap is returned and
`narration_available` is `false`.

## Layout

```
backend/main.py      FastAPI app; api_models.py (schemas); pipeline.py (deterministic fast path); presets.py
backend/agent/       tools.py (shared tool functions), agent.py (Google ADK agent, trace, fallback)
backend/qloo/        client.py (throttle, retry, cache, budget), models.py, cache.py, seeds.py
backend/ranking/     taste_lift.py (pure scoring), sweep.py, personalization.py, bias.py (analyses)
backend/inventory/   rosters.py (evidence tiers), events.py (F1 + PL schedules, IST windows), clips.py (official channels)
data/                f1_grid.json (Jolpica snapshot), pl_seed.json (manually curated), roster_overrides.json,
                     rosters.json (verified Qloo IDs + evidence tier), personas_eval.json,
                     sweep_inputs.json / sweep_results.json (parameter sweep), personalization_strength.json,
                     precomputed/ (preset payloads served with 0 live calls)
scripts/             refresh_f1_grid.py, verify_rosters.py, demo_rank.py, param_sweep.py, personalization_check.py,
                     bias_check.py, smoke_compare.py, kill_tests.py, refresh_media.py, precompute_presets.py
tests/               pytest suite (no live API) + fixtures/ (raw Qloo responses, no keys)
```

## Scripts

```powershell
.venv\Scripts\python.exe scripts/refresh_f1_grid.py           # latest race lineup -> data/f1_grid.json (nightly later)
.venv\Scripts\python.exe scripts/verify_rosters.py            # resolve rosters -> data/rosters.json
.venv\Scripts\python.exe scripts/demo_rank.py --seeds "Spirited Away|Arijit Singh|Valorant" --sport f1
.venv\Scripts\python.exe scripts/param_sweep.py --offline     # recompute the alpha/beta sweep, no API calls
.venv\Scripts\python.exe scripts/personalization_check.py     # how different are personas' rankings? (offline)
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
