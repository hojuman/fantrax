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
uv run hockey sync [--refresh] [--skip-moneypuck]  # Fantrax + NHL + MoneyPuck + ID mapping
uv run hockey roster [--team X] # roster: FP/GP, rest-of-season games + FP, top components, basis
uv run hockey rank [--pos D] [--available] [--owner TBB] [--sort ros] [--limit N]
uv run hockey player NAME|FXID  # every component behind one projection (prior, season, L30, L14, weights)
uv run hockey lineup [--period N] [--current] [--out NAME ...]  # best lineup for the next weekly lock
uv run hockey waivers [--pos D] [--protect NAME ...] [--max-ros-cost 10] [--period N]  # pickups + streamers
uv run hockey trade "GIVE, ..." "GET, ..." [--give-pick 2027:2] [--get-pick 2027:1] [--partner T] [--keeper-weight 0.5]
uv run hockey keepers [--horizon 3] [--keepers-file PATH]  # multi-year keeper value + best 10+5 with tags
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
| NHL stats REST, recent form | same reports with `and gameDate>="…" and gameDate<="…"` for last 14/30 days (in season only) | none | 6h |
| NHL web API | `api-web.nhle.com/v1/roster/{TEAM}/{season}` (32 calls; `/current` is a 307 to this), `/v1/standings/{date}` (team GP, in season), `/v1/schedule/{date}` (7-day `gameWeek`; `gameType` 2 = regular season, `startTimeUTC`, `homeTeam/awayTeam.abbrev`) | none | 24h / 6h / 12h |
| NHL player landing | `api-web.nhle.com/v1/player/{id}/landing`: `careerTotals.regularSeason.gamesPlayed` (absent = 0 GP), `birthDate`. Fetched per player by `hockey keepers` (your roster only) | none | 24h |
| MoneyPuck | `moneypuck.com/moneypuck/playerData/seasonSummary/{startYear}/regular/{skaters,goalies}.csv`; `playerId` = NHL id; rows per `situation` (we read `all` + `5on4`); `icetime` in seconds. Columns read: skaters `I_F_xGoals, I_F_goals, I_F_shotsOnGoal, games_played, icetime`; goalies `xGoals, goals, ongoal`. Column names are unverified until `hockey probe` runs; the parser fails loudly with the real header | none | past ∞, current 24h (404 before opening night is fine) |

League scoring (Fantrax, verified 2026-09-29), H2H points. Skaters: G 2, A 1.5, +/- 0.25, PPP 0.5,
SOG 0.1, Hit 0.08, Blk 0.08, PIM 0.1. Goalies: W 2, SHO 2, SV 0.155, GA −1. Always re-read this from
Fantrax on sync; don't hard-code it. Roster limits (Fantrax settings, verified 2026-09-29; Fantrax is
authoritative over the rules text's "27"): 28 total, 12 active, 6 reserve, 3 IR, 5 minors. IR and minors
**count toward the total**, so the slot caps bind first: **effective max 26** (`roster.effective_max`).
Phase 4: open spots = effective_max − rostered count. Minors eligibility: career + current regular-season
GP **≤ 165** (skaters and goalies). Fantrax only blocks transactions that break total roster size.
Sync still compares Fantrax `rosterInfo` with `data/league.yaml` and notes any future drift.
Sync also stores `rosterPeriods` (weekly locks, Mon ~evening ET, times vary) and `matchups` (the H2H
schedule) in `meta`, for Phase 3 and Phase 7.

`getPlayerIds` (verified 2026-09-29, 9,045 NHL players) carries **no NHL id**. Its extra fields are
`rotowireId, sportRadarId, statsIncId, shortName, teamName, teamShortName`. The team is read from
`team`, then `teamShortName`, then `teamName` (full names are mapped in `sources/teams.py`).

Rate limits (`http.py`): Fantrax ≥1s between requests, NHL ≥0.5s, MoneyPuck ≥2s; retries with
exponential backoff on 429/5xx/transport errors. Redirects are followed (max 3 hops) **only** when
the target passes `check_allowed()` on the same host; anything else raises `ReadOnlyViolation`.
MoneyPuck terms (read 2026-09-29): "free to use for non-commercial purposes… Please clearly credit
MoneyPuck.com in all cases where you are showing anything using our data as an input." The user
approved it: **every output that uses MoneyPuck data must show `sources.moneypuck.CREDIT`.** The roster,
rank and player views do this whenever `ProjectionV2.uses_moneypuck` is set; keep it that way for new views.

## Lineups (Phase 3, `hockey/lineup/`)
- Weekly locks come from Fantrax `rosterPeriods` (stored in meta; e.g. period 2 = 2026-10-05 19:00 ET →
  2026-10-12 12:59 ET). Default target = the next period that hasn't started. Games count when
  `start <= startTimeUTC < end`.
- **Availability has no injury feed, by design** (no ToS-safe source). Signals: an IR slot, `--out NAME`
  (accent/case-insensitive), and the projection's games share (drops when a player sits while his
  team plays → "hasn't played lately" flag). Out players are never started, even into an empty slot.
- Goalies: expected starts per game = games_share × gs rate. Heuristic: on the 2nd night of a
  back-to-back (≤30 h apart), a goalie with start share > 0.6 gets half. Shown as "b2b".
- Optimizer (`optimize.py`): exact DP over slot-fill states (405) × players; a test checks it against
  brute force. Zero-game players fill otherwise-empty slots (and get flagged).
- Report: recommended actives, bench with reasons, plain-language moves (never sent to Fantrax), the
  current-vs-recommended gain, and flags: empty slots, 0-game starters, healthy-looking IR players,
  Minors promotions worth considering, reserve > 6 / IR > 3.

## Waivers (Phase 4, `hockey/waivers/report.py`)
- **Gain = improvement in your best starting lineup**, not player vs player:
  `V(roster − drop + pickup) − V(roster)`, where V is the lineup DP's total. Season-long pickups value
  players by `ros_fp`; streamers by expected FP in the period (same numbers as `hockey lineup`).
- **Open spots** = min(effective_max − all rostered, (12 + 6) − Active/Reserve). With an open spot, a
  pickup needs no drop.
- **Drops**: only Active/Reserve players. IR and Minors players are never suggested. Also protected:
  `franchise_tag: true` in `data/keepers.yaml` (`hockey/keepers.py`), and `--protect NAME`. Tried
  drops are the 3 lowest-ROS players plus the lowest per position group, so the run stays fast.
- **Streamers** may only use a drop that costs ≤ `--max-ros-cost` (default 10) rest-of-season points.
  Anything held back is summarized in a note.
- **Pool**: FA/W players with an NHL id. Players with no NHL record (juniors, undrafted prospects,
  ineligible under league rules) are excluded, with a note. W players are marked (a claim, not an
  instant add).
- With an open spot and no lineup-improving pickup, a "best use of your open spot" depth list (top ROS
  FAs) is shown instead of an empty table.
- Output: best pickups (ROS gain), by-position table (your weakest starter vs the best available),
  streamers (week gain + ROS change), games-per-team density, notes, MoneyPuck credit.

## Trades (Phase 5, `hockey/trade/analyze.py`)
- Evaluates both sides. **Roster fit** = change in each team's best starting lineup over the rest of the
  season (same lineup-DP measure as waivers; incoming players arrive on Reserve, IR players stay IR).
  **Keeper value** = next-season points (fp_per_gp × games_share × 82) above the keeper line (value of
  the player ranked teams × regular keepers = 100th). **Picks** (entered by hand, `season:round`) = the
  next-season value at rank kept + (round−1)×teams + mid-round, minus the waiver line (rank teams × 18).
  **Score** = lineup change + keeper_weight × (keeper + pick change). Verdict wording comes from both
  scores.
- Scarcity context: value over replacement per player (replacement = first non-starter at the slot
  league-wide: teams × slots). It's shown, not added to the score (the lineup DP already prices need).
- Notes: roster overflow (must drop), franchise-tag / keeper-clock reset (from keepers.yaml), IR
  players, pick-slot assumption. Name resolution: comma-separated names or FX ids; ambiguous names
  list the candidates; everything you receive must come from one team (`--partner` for picks-only).
- No age curve yet (Phase 6). Draft picks aren't read from Fantrax (`getDraftPicks` unverified).

## Keepers (Phase 6, `hockey/keeper/`)
- **Rules**: 10 regular + 5 minors, 4 franchise tags, max 3 keeps per regular player. The clock is off while
  minors-eligible and resets on a trade. A removed tag can never go back on that player, and untagging a
  player kept 3+ times makes him unkeepable (both enforced in `plan.build_candidate`).
- **Minors eligibility** is judged at keeper time: career GP (landing) + projected rest-of-season GP ≤ 165.
  Players about to cross it get a "graduates" note. No-NHL-record players are assumed minors-eligible
  with unknown age.
- **Value** = each future season's projected points (this season's value aged with `aging.py`: YoY
  factor by age and position), discounted 0.85/yr, for as many seasons as he can be kept (horizon 3;
  untagged = min(horizon, 3 − times_kept) unless minors-eligible). Regular slots count points above the
  keeper line (the ~100th player's next-season value); minors slots count raw points. Minors-eligible
  players not yet playing much are assumed ~30% of games (about 25) next season.
- **Best set**: exact DP over (regular, tags, minors) with options release / minors / regular /
  regular+tag. It warns when a tag moves (permanent), a tagged player is released, clock entries are
  missing from keepers.yaml, or regular slots go unused.
- The trade analyzer's next-season value is aged one year too (birth dates from NHL rosters).

## Known fragile points
1. **fxea league access.** Verified working for Talladega Nights on 2026-09-29: getLeagueInfo
   (10 teams, pool 8,747, 8 skater + 4 goalie scoring codes), getTeamRosters (248 rows), and
   getStandings. If Fantrax ever refuses, sync reports it and falls back: rosters via
   `hockey import-csv`, scoring via `data/league.yaml`.
2. **Fantrax errors arrive as HTTP 200** with an error object. `fantrax_fxea.body_error()` checks
   every body; error bodies are never cached.
3. **Fantrax response shapes** were verified live on 2026-09-29, and the parsers target them first:
   - getLeagueInfo scoring lives in `scoringSystem.scoringCategorySettings[].configs[]`, with
     `points`, `position.code` (DEFAULT, or a position for per-position weights → F/D overrides),
     `scoringCategory.shortName`, and a group of `HOCKEY_SKATING`/`HOCKEY_GOALIE`.
   - The same weights appear again as strings in `scoringSystem.scoringCategories`
     (`{"Default": "points0.155"}`). That copy is the fallback, and sync flags any disagreement.
   - `teamInfo` has only id + name; team short names come from `matchups`.
   - getTeamRosters items are `{id, position, status}`, with statuses ACTIVE / RESERVE /
     INJURED_RESERVE / MINORS.
   - getPlayerIds entries are `{fantraxId, name "Last, First", position, team (NHL codes, "(N/A)"),
     rotowireId?, statsIncId?, sportRadarId?}`.
   If Fantrax reshapes a response, the heuristic fallback plus `FantraxShapeError` take over.
   Fixtures come from `tests/fixtures/generate.py`: real structure, **anonymized at the user's
   request** (placeholder teams except "The Blue Blazers", no owner/manager/handle fields, only the
   players the tests need). Never copy raw `var/probe/` output into the repo.
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
changes, prospects with no NHL games (expected unmatched). Also from the real feed: a team-less
"OReilly, Ryan" namesake that must lose to the real one (conflict demotion), duplicate Fantrax names
with no NHL record (two Hugo Petterssons), and an Aho listed with his new team after a trade.
**What to watch:** sync reports `unmatched: rostered` (should be only rookies/prospects; names are
listed) separately from `unmatched: pool, no NHL record` (thousands of juniors/Europeans: expected).
`hockey ids --unmatched` shows rostered players only; add `--all` for the pool.
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
(the prior: weights 5/4/3 normalized so latest season = 1.0; regressed with k phantom games of the
position mean: F/D 25, G 20; means from ≥20-GP players in the latest completed season) ·
`projection/inseason.py` (Phase 2 model, all constants at the top of the file with the rationale in its
docstring: per-stat stabilization n0 in games, recency bonus 0.5 + 0.5 for the last 30/14 days, goals
blended 40% toward ixG, TOI/PP-TOI role factors (clipped), goalie SV% regressed by 1,500 shots,
games share with 20 team games of prior weight, rookie default = 30th-percentile rate at the position)
· `valuation.py` (builds a `ProjectionV2` for any player from the db) ·
`lineup/` (periods, availability, optimize, report) · `waivers/report.py` · `trade/analyze.py` ·
`keeper/` (aging, plan) · `keepers.py` (keepers.yaml loader) ·
`sync.py` orchestration · `probe.py` ·
`views/tables.py` · `cli.py`.
Canonical stat keys: skater `gp g a pts pm pim ppg ppa ppp shg sha shp gwg otg sog hit blk fow fol tk
gv toi_min evg evp`; goalie `gp gs w l otl ga sv sa so toi_min g a pts pim`.

## Phase plan
1. ✅ Fantrax sync, NHL stats, ID mapping, scoring engine, baseline projection, `hockey roster`.
2. ✅ Valuation v2: in-season blend + last 14/30 days + prior, per-stat regression; MoneyPuck
   xG/TOI/PP share; rest-of-season games; rookie default; `hockey player`, `hockey rank`.
3. ✅ Weekly lineup optimizer (`hockey lineup`): period schedule, goalie starts + b2b, availability
   signals + `--out`, multi-position DP, moves and flags.
4. ✅ Waivers (`hockey waivers`): lineup-delta gains with best drop, by-position view, streamers with a
   ROS-cost limit, schedule density, keeper/--protect protection, no-record prospects excluded.
5. ✅ Trade analyzer (`hockey trade`): both-sides roster fit (lineup DP), value over replacement,
   keeper value above the keeper line, draft-pick value, roster overflow, keeper-clock notes.
6. ✅ Keepers (`hockey keepers`): age curve, keeper clock + tag rules from `data/keepers.yaml`,
   165-GP minors rule from career GP at keeper time, exact best 10 + 5 with tag assignment.
7. League intel + daily markdown report.
