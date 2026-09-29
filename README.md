# fantrax: hockey assistant

A personal, **read-only** fantasy hockey assistant for a Fantrax NHL league. It syncs league data
from Fantrax's published API (or CSV exports), pulls NHL stats, maps Fantrax players to NHL ids,
and values players under the league's own scoring (prior seasons + this season + recent form,
with expected-goals data from MoneyPuck.com). It never makes moves on Fantrax.

```bash
uv sync
cp .env.example .env        # set FANTRAX_LEAGUE_ID (and MY_TEAM_NAME if different)
uv run hockey probe         # check every data source
uv run hockey sync          # pull + cache everything into var/hockey.db
uv run hockey roster        # your roster with projected fantasy value
uv run hockey rank --pos D --available   # best free-agent defencemen
uv run hockey player "Quinn Hughes"      # why a player projects where he does
uv run hockey lineup                     # best lineup for the next weekly lock
uv run hockey lineup --out "Sebastian Aho"   # ...with a player you know is hurt (full name)
uv run hockey waivers                    # best pickups, your weak spots, streamers for the week
uv run hockey trade "Alex Tuch" "Connor McDavid" --give-pick 2027:1   # evaluate a trade for both sides
uv run hockey keepers                    # multi-year keeper value and the best 10 + 5 (fill in data/keepers.yaml)
uv run hockey intel                      # league strengths/weaknesses and trade partners
uv run hockey report --sync              # today's one-page markdown report -> var/reports/
uv run hockey ids --unmatched            # rostered players without an NHL id
uv run pytest                            # offline tests
```

Daily report on a schedule (macOS/Linux cron, 8:15 every morning):

```
15 8 * * * cd /path/to/fantrax && uv run hockey report --sync
```

See [CLAUDE.md](CLAUDE.md) for data sources, fragile points, ID mapping and the phase plan.
