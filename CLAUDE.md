# CLAUDE.md: hockey assistant for "Talladega Nights" (Fantrax NHL)

Personal, **read-only** fantasy hockey assistant. Python 3.11, uv, Typer + Rich CLI, SQLite cache.
League facts (10 teams, H2H points, weekly Monday lock, 2C/2LW/2RW/4D/2G, keeper rules) live in
`data/league.yaml`. The approved architecture/phase plan is summarized at the bottom.

## Hard rules
- **Never act on Fantrax.** No roster moves, claims, trades, or lineup changes. Output only.
  Enforced in `hockey/http.py`: GET-only client (no post/put/delete exists), host allowlist, and on
  fantrax.com only `/fxea/` paths. `tests/test_http_guard.py` pins this. Don't loosen it.
- **No cookies / no Fantrax web-app backend.** Fantrax's ToS prohibits crawling/scraping. We use only
  the published keyless `fxea` API plus CSVs the user exports by hand. `fantraxapi` (PyPI) and
  `/fxpa/req` were evaluated and **rejected** for this reason; don't add them back.
- **Secrets** go in `.env` (gitignored). Currently only non-secret config lives there (league id,
  team name). `var/` (SQLite db, probe output) is gitignored too.
- **Tests never hit the network** (`pytest-socket`, `--disable-socket` in pyproject). Use fixtures.
- Don't commit anything from the league rules that identifies people (e.g. commissioner email).

## Commands
```
uv sync                         # install
uv run pytest                   # offline test suite
uv run ruff check . && uv run ruff format --check .
uv run hockey probe             # live check of every source: scoring table + mapping result, a sample
                                #   player entry, roster/pool status counts; samples -> var/probe/
uv run hockey sync [--refresh]  # Fantrax + NHL + ID mapping into var/hockey.db
uv run hockey roster [--team X] # roster with projected FP under league scoring
uv run hockey ids --unmatched | --fuzzy
uv run hockey import-csv FILE [--team NAME]   # fallback when fxea refuses league data
uv run hockey validate-scoring FILE           # our engine vs Fantrax FPts, same CSV
```

## Data sources
| Source | Endpoints | Auth | Cache TTL |
|---|---|---|---|
| Fantrax fxea (published) | `/fxea/general/getPlayerIds?sport=NHL`, `getLeagueInfo?leagueId=`, `getTeamRosters?leagueId=`, `getStandings?leagueId=` | none | ids 7d, league 24h, rosters 1h |
| Fantrax CSV (manual) | Players page / roster → Download CSV → `hockey import-csv` | user's browser | n/a |
| NHL stats REST | `api.nhle.com/stats/rest/en/{skater,goalie}/{summary,realtime,faceoffwins}` (paginated 100/page, `cayenneExp=gameTypeId=2 and seasonId=YYYYYYYY`) | none | past seasons forever, current 6h |
| NHL web API | `api-web.nhle.com/v1/roster/{TEAM}/{season}` (32 calls; `/current` is a 307 to this) | none | 24h |
| MoneyPuck (Phase 2) | `moneypuck.com/moneypuck/playerData/seasonSummary/{yr}/regular/{skaters,goalies}.csv`; `playerId` = NHL id | none | 24h |

`getPlayerIds` (verified 2026-09-29, 9,045 NHL players) carries **no NHL id**. Its extra fields are
`rotowireId, sportRadarId, statsIncId, shortName, teamName, teamShortName`. The team is read from
`team`, then `teamShortName`, then `teamName` (full names are mapped in `sources/teams.py`).

Rate limits (`http.py`): Fantrax ≥1s between requests, NHL ≥0.5s, MoneyPuck ≥2s; retries with
exponential backoff on 429/5xx/transport errors. Redirects are followed (max 3 hops) **only** when
the target passes `check_allowed()` on the same host; anything else raises `ReadOnlyViolation`.
MoneyPuck terms (read 2026-09-29): "free to use for non-commercial purposes… Please clearly credit
MoneyPuck.com in all cases where you are showing anything using our data as an input." The user
approved it for Phase 2: **every output that uses MoneyPuck data must show a MoneyPuck.com credit.**

## Known fragile points
1. **fxea league access.** Verified working for Talladega Nights on 2026-09-29: getLeagueInfo
   (10 teams, pool 8,747, 8 skater + 4 goalie scoring codes), getTeamRosters (248 rows), and
   getStandings. If Fantrax ever refuses, sync reports it and falls back: rosters via
   `hockey import-csv`, scoring via `data/league.yaml`.
2. **Fantrax errors arrive as HTTP 200** with an error object. `fantrax_fxea.body_error()` checks
   every body; error bodies are never cached.
3. **Response shapes are best-known guesses** until `hockey probe` runs against the real league.
   Parsers raise `FantraxShapeError` rather than guessing. Test fixtures in `tests/fixtures/` are
   hand-built. After a successful probe, diff `var/probe/*.json` against them and update both the
   parsers and fixtures deliberately. **The user asked that fixtures be anonymized further:**
   placeholder team names (except "The Blue Blazers"), no owner or manager fields, and only the
   players the tests need. Never copy raw probe samples into the repo.
4. **Scoring code mapping** (`scoring/rules.py`): unknown Fantrax stat codes raise; rate stats (GAA,
   SV%) are refused, not guessed. Escape hatches: `scoring.code_aliases` / `ignore_codes` in
   `data/league.yaml`. Verify with `hockey validate-scoring` on a Fantrax stats CSV export.
5. **NHL API is unofficial/undocumented.** The web API uses 307 redirects for "current"/"now" URLs.
   We request explicit-season URLs, and follow only allowlisted same-host redirects. One team's
   roster failing is a sync note, not an abort. Field names used: see `sources/nhl.py`
   (`merge_skater_rows`, `goalie_lines`, `roster_players`). If NHL renames a field, stats silently
   become 0: check `hockey probe` field lists first when numbers look off.
6. **ID mapping** (most likely to break). See next section.
7. **CSV column drift:** `sources/fantrax_csv.py` looks columns up through alias lists and errors
   with the header it saw.

## Fantrax → NHL id mapping (`idmap/`)
Cascade, each result stored in `player_map` with `method` + `confidence`:
override (`data/id_overrides.csv`) → external_id (if Fantrax ever exposes an NHL id) → sticky
(previous match kept while names agree) → exact (name+pos group+team) → exact_anypos (unique name,
same team) → alias (nicknames/suffixes, unique in pos group) → alias_team → fuzzy (rapidfuzz ≥92,
unique; flagged until confirmed) → unmatched (`id_unmatched`, with reason + plausible candidates).
Only rostered/pool players are mapped when rosters are known. Two Fantrax ids claiming one NHL id
demotes the weaker match to unmatched. Traps covered by tests: two Sebastian Ahos (CAR F / NYI D),
two Elias Petterssons (both VAN, F vs D), accents, J.T./JT, Mitch/Mitchell, Egor/Yegor, position
changes, prospects with no NHL games (expected unmatched).
**Fixing a miss:** `hockey ids --unmatched`, add `fantrax_id,nhl_id,note` to `data/id_overrides.csv`
(NHL id is in the nhl.com player URL), then `hockey sync --skip-nhl`.

## Auth / refreshing access
There is **no cookie or token to refresh, by design** (see Hard rules). If Fantrax starts refusing:
1. `uv run hockey probe`: see which fxea call fails and the error text.
2. If league data is refused (private league), export CSVs from Fantrax and use
   `hockey import-csv`; keep scoring weights in `data/league.yaml`.
3. Options that need the user's decision, not a code change: the commissioner makes the league
   public, or the user explicitly revisits the no-cookie decision.
Container note: cloud sessions need `www.fantrax.com`, `api.nhle.com`, `api-web.nhle.com`,
`moneypuck.com` in the environment's allowed network hosts.

## Code map
`hockey/http.py` guard+cache · `sources/` fantrax_fxea, fantrax_csv, nhl, teams · `idmap/` ·
`scoring/` rules (codes↔canonical keys) + engine (pure, per-stat breakdown) · `projection/baseline.py`
(weights 5/4/3 normalized so latest season = 1.0; regressed with k phantom games of the position
mean: F/D 25, G 20; means from ≥20-GP players in the latest completed season) · `valuation.py` ·
`sync.py` orchestration · `probe.py` · `views/tables.py` · `cli.py`.
Canonical stat keys: skater `gp g a pts pm pim ppg ppa ppp shg sha shp gwg otg sog hit blk fow fol tk
gv toi_min evg evp`; goalie `gp gs w l otl ga sv sa so toi_min g a pts pim`.

## Phase plan
1. ✅ Fantrax sync, NHL stats, ID mapping, scoring engine, baseline projection, `hockey roster`.
2. Valuation v2: in-season blend + last 14/30 days + prior, empirical-Bayes regression; MoneyPuck
   xG/TOI/PP share; `hockey player NAME` (components), `hockey rank --pos`.
3. Weekly lineup optimizer (Mon–Sun schedule, goalie starts, injuries, multi-position eligibility).
4. Waivers/streaming vs weakest rostered player per position; filter ineligible (undrafted) prospects.
5. Trade analyzer: value over replacement (10-team slot counts), scarcity, roster fit, keeper and
   draft-pick value.
6. Keepers: age curve, `data/keepers.yaml` clock (3-time limit, 4 franchise tags, tag removal
   rule, reset on trade), 165-GP minors rule from career GP; optimal 10+5 set.
7. League intel + daily markdown report.
