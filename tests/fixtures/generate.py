"""Regenerates the test fixtures in this directory: `uv run python tests/fixtures/generate.py`.

NHL fixtures are hand-built in the NHL API's field names. Fantrax fixtures copy the structure and key
names of live fxea responses (probe 2026-09-29), anonymized: placeholder teams except "The Blue
Blazers", no owner/manager fields, only the players the tests need. Never paste raw probe output here.
"""

import csv
import io
import json
import pathlib

F = pathlib.Path(__file__).parent
F.mkdir(parents=True, exist_ok=True)

# nhl_id, first, last, pos_code, team(current), birth
NHL = [
    (8478402, "Connor", "McDavid", "C", "EDM", "1997-01-13"),
    (8478427, "Sebastian", "Aho", "C", "CAR", "1997-07-26"),
    (8480222, "Sebastian", "Aho", "D", "PIT", "1996-02-17"),
    (8477969, "Marcus", "Pettersson", "D", "NYR", "1996-05-08"),
    (8480012, "Elias", "Pettersson", "C", "VAN", "1998-11-12"),
    (8483678, "Elias", "Pettersson", "D", "VAN", "2004-02-16"),
    (8482116, "Tim", "Stützle", "C", "OTT", "2002-01-15"),
    (8483515, "Juraj", "Slafkovský", "L", "MTL", "2004-03-30"),
    (8478483, "Mitch", "Marner", "R", "VGK", "1997-05-05"),
    (8476468, "J.T.", "Miller", "C", "NYR", "1993-03-14"),
    (8475158, "Ryan", "O'Reilly", "C", "NSH", "1991-02-07"),
    (8480800, "Quinn", "Hughes", "D", "VAN", "1999-10-14"),
    (8481559, "Jack", "Hughes", "C", "NJD", "2001-05-14"),
    (8477967, "Thatcher", "Demko", "G", "VAN", "1995-12-08"),
    (8477424, "Juuse", "Saros", "G", "NSH", "1995-04-19"),
    (8484999, "Callup", "Rookie", "D", "CAR", "2005-01-01"),
    (8485555, "Rookie", "Newby", "C", "CAR", "2007-02-01"),  # no NHL games before 2026-27
]
rosters = {}
for pid, fn, ln, pos, team, bd in NHL:
    grp = {"G": "goalies", "D": "defensemen"}.get(pos, "forwards")
    rosters.setdefault(team, {"forwards": [], "defensemen": [], "goalies": []})[grp].append(
        {
            "id": pid,
            "firstName": {"default": fn},
            "lastName": {"default": ln},
            "positionCode": pos,
            "birthDate": bd,
            "sweaterNumber": 1,
        }
    )
(F / "nhl_rosters.json").write_text(json.dumps(rosters, indent=1, ensure_ascii=False))

# skater season lines: gp, g, a, pm, pim, ppg, ppp, shg, shp, gwg, sog, toi/gp(sec), hits, blk, fow, fol
SK = {
    8478402: {
        20252026: (80, 48, 90, 20, 30, 15, 45, 1, 2, 9, 320, 1320, 40, 30, 600, 500),
        20242025: (67, 26, 74, 12, 37, 8, 38, 0, 1, 5, 225, 1300, 36, 28, 500, 470),
        20232024: (76, 32, 100, 35, 30, 10, 44, 2, 3, 8, 250, 1330, 38, 34, 550, 480),
    },
    8478427: {
        20252026: (80, 35, 45, 10, 20, 11, 28, 2, 3, 7, 230, 1180, 40, 30, 800, 700),
        20242025: (82, 29, 45, 8, 22, 9, 25, 1, 2, 6, 220, 1170, 41, 29, 780, 690),
        20232024: (79, 36, 53, 20, 18, 10, 26, 3, 4, 8, 240, 1190, 45, 31, 820, 720),
    },
    8480222: {
        20252026: (78, 4, 18, 5, 20, 0, 3, 0, 0, 1, 90, 1250, 60, 110, 0, 0),
        20242025: (70, 3, 12, -2, 16, 0, 1, 0, 0, 0, 85, 1200, 55, 100, 0, 0),
    },
    8480012: {
        20252026: (80, 25, 45, 3, 12, 8, 24, 1, 1, 4, 190, 1100, 30, 30, 700, 650),
        20242025: (64, 15, 30, -17, 10, 4, 14, 0, 0, 2, 140, 1050, 25, 26, 600, 600),
        20232024: (82, 34, 55, 22, 12, 11, 33, 1, 1, 6, 220, 1120, 30, 35, 750, 700),
    },
    8477969: {20252026: (75, 3, 20, 10, 30, 0, 1, 0, 0, 0, 70, 1260, 80, 120, 0, 0)},
    8483678: {20252026: (60, 2, 9, 4, 50, 0, 0, 0, 0, 0, 60, 1000, 120, 90, 0, 0)},
    8482116: {20252026: (80, 30, 50, 5, 40, 10, 25, 0, 0, 5, 230, 1200, 80, 30, 400, 450)},
    8483515: {20252026: (80, 22, 35, 2, 30, 6, 18, 0, 0, 3, 180, 1080, 90, 20, 10, 12)},
    8478483: {20252026: (82, 28, 70, 15, 20, 8, 35, 1, 2, 5, 210, 1250, 25, 35, 20, 20)},
    8476468: {20252026: (80, 30, 45, 0, 40, 10, 30, 0, 0, 5, 200, 1200, 60, 30, 900, 800)},
    8475158: {20252026: (75, 22, 30, -5, 20, 8, 20, 1, 1, 3, 160, 1150, 40, 40, 900, 750)},
    8480800: {20252026: (74, 16, 70, 18, 20, 5, 35, 0, 0, 3, 190, 1500, 20, 70, 0, 0)},
    8481559: {20252026: (62, 27, 45, 3, 14, 8, 25, 0, 0, 4, 230, 1200, 20, 20, 300, 400)},
    8484999: {20252026: (6, 1, 0, 1, 2, 0, 0, 0, 0, 0, 5, 900, 10, 8, 0, 0)},
}
GO = {  # gp, gs, w, l, otl, ga, sv, sa, so, toi_sec
    8477967: {20252026: (50, 49, 28, 15, 5, 130, 1300, 1430, 3, 50 * 3500)},
    8477424: {
        20252026: (58, 57, 26, 25, 5, 170, 1450, 1620, 2, 58 * 3500),
        20242025: (58, 57, 20, 30, 6, 180, 1420, 1600, 1, 58 * 3500),
    },
}
names = {pid: (f"{fn} {ln}", pos, team) for pid, fn, ln, pos, team, bd in NHL}
old_team = {8478483: "TOR", 8476468: "VAN,NYR", 8480222: "NYI"}
EMPTY = ("skater/summary", "skater/realtime", "skater/faceoffwins", "goalie/summary")


def add_skaters(stats, sk):
    for pid, seasons in sk.items():
        name, pos, team = names[pid]
        for season, t in seasons.items():
            gp, g, a, pm, pim, ppg, ppp, shg, shp, gwg, sog, toi, hits, blk, fow, fol = t
            s = stats.setdefault(str(season), {k: [] for k in EMPTY})
            s["skater/summary"].append(
                {
                    "playerId": pid,
                    "skaterFullName": name,
                    "positionCode": pos,
                    "teamAbbrevs": old_team.get(pid, team) if season < 20252026 else team,
                    "gamesPlayed": gp,
                    "goals": g,
                    "assists": a,
                    "points": g + a,
                    "plusMinus": pm,
                    "penaltyMinutes": pim,
                    "ppGoals": ppg,
                    "ppPoints": ppp,
                    "shGoals": shg,
                    "shPoints": shp,
                    "gameWinningGoals": gwg,
                    "otGoals": 0,
                    "shots": sog,
                    "timeOnIcePerGame": toi,
                    "evGoals": g - ppg - shg,
                    "evPoints": g + a - ppp - shp,
                    "faceoffWinPct": None,
                }
            )
            s["skater/realtime"].append(
                {
                    "playerId": pid,
                    "hits": hits,
                    "blockedShots": blk,
                    "takeaways": gp // 4,
                    "giveaways": gp // 5,
                }
            )
            s["skater/faceoffwins"].append(
                {"playerId": pid, "totalFaceoffWins": fow, "totalFaceoffLosses": fol}
            )


def add_goalies(stats, go):
    for pid, seasons in go.items():
        name, pos, team = names[pid]
        for season, (gp, gs, w, losses, otl, ga, sv, sa, so, toi) in seasons.items():
            s = stats.setdefault(str(season), {k: [] for k in EMPTY})
            s["goalie/summary"].append(
                {
                    "playerId": pid,
                    "goalieFullName": name,
                    "teamAbbrevs": team,
                    "gamesPlayed": gp,
                    "gamesStarted": gs,
                    "wins": w,
                    "losses": losses,
                    "otLosses": otl,
                    "goalsAgainst": ga,
                    "saves": sv,
                    "shotsAgainst": sa,
                    "shutouts": so,
                    "timeOnIce": toi,
                    "goals": 0,
                    "assists": 1,
                    "points": 1,
                    "penaltyMinutes": 0,
                    "savePct": sv / sa,
                    "goalsAgainstAverage": ga * 3600 / toi,
                }
            )


stats = {}
add_skaters(stats, SK)
add_goalies(stats, GO)
(F / "nhl_stats.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False))

# ------------------------------------------------------------------ in-season scenario (2026-11-15)
# Every team has played 15 games. Aho (CAR C) is on a heater that expected goals don't support and
# got PP1 time; McDavid is normal; Newby is a rookie with games but no prior; Saros is struggling.
LIVE = 20262027
CUR_SK = {
    8478402: {LIVE: (15, 9, 18, 5, 4, 3, 9, 0, 0, 2, 60, 1330, 8, 6, 110, 95)},
    8478427: {LIVE: (15, 13, 7, 6, 2, 5, 8, 0, 0, 3, 45, 1290, 6, 5, 160, 140)},
    8480800: {LIVE: (15, 3, 14, 4, 2, 1, 7, 0, 0, 1, 36, 1510, 4, 13, 0, 0)},
    8485555: {LIVE: (12, 4, 5, 2, 4, 1, 2, 0, 0, 1, 25, 980, 10, 4, 60, 70)},
}
CUR_GO = {8477424: {LIVE: (12, 12, 4, 7, 1, 44, 300, 344, 0, 12 * 3500)}}
W30_SK = {
    8478402: {LIVE: (10, 6, 12, 3, 2, 2, 6, 0, 0, 1, 40, 1330, 5, 4, 75, 62)},
    8478427: {LIVE: (10, 10, 5, 5, 2, 4, 6, 0, 0, 2, 31, 1300, 4, 3, 110, 90)},
    8485555: {LIVE: (8, 3, 4, 1, 2, 1, 2, 0, 0, 1, 17, 1000, 7, 3, 40, 45)},
}
W14_SK = {
    8478427: {LIVE: (6, 7, 3, 4, 0, 3, 4, 0, 0, 2, 19, 1310, 2, 2, 66, 55)},
    8485555: {LIVE: (5, 2, 3, 1, 0, 1, 2, 0, 0, 0, 11, 1010, 4, 2, 25, 28)},
}
W30_GO = {8477424: {LIVE: (8, 8, 2, 5, 1, 31, 190, 221, 0, 8 * 3500)}}
live, w30, w14 = {}, {}, {}
add_skaters(live, CUR_SK)
add_goalies(live, CUR_GO)
add_skaters(w30, W30_SK)
add_goalies(w30, W30_GO)
add_skaters(w14, W14_SK)
(F / "nhl_stats_live.json").write_text(json.dumps(live, indent=1, ensure_ascii=False))
(F / "nhl_windows.json").write_text(
    json.dumps({"last30": w30[str(LIVE)], "last14": w14[str(LIVE)]}, indent=1, ensure_ascii=False)
)
TEAMS_NHL = [
    "ANA",
    "BOS",
    "BUF",
    "CGY",
    "CAR",
    "CHI",
    "COL",
    "CBJ",
    "DAL",
    "DET",
    "EDM",
    "FLA",
    "LAK",
    "MIN",
    "MTL",
    "NSH",
    "NJD",
    "NYI",
    "NYR",
    "OTT",
    "PHI",
    "PIT",
    "SJS",
    "SEA",
    "STL",
    "TBL",
    "TOR",
    "UTA",
    "VAN",
    "VGK",
    "WSH",
    "WPG",
]
(F / "nhl_standings.json").write_text(
    json.dumps(
        {"standings": [{"teamAbbrev": {"default": t}, "gamesPlayed": 15} for t in TEAMS_NHL]}, indent=1
    )
)

# ------------------------------------------------------------------ MoneyPuck season summaries
# Real files have ~150 columns; these carry the ones we read plus a few identifying ones, and three
# situations so the parser has to pick the right rows. icetime is in seconds.
MP_DIR = F / "moneypuck"
MP_DIR.mkdir(exist_ok=True)
IXG = {(8478402, 20252026): 38.5, (8478427, 20252026): 33.0, (8478427, LIVE): 6.5}  # others: goals * 0.9
PP_SEC = {(8478427, LIVE): 15 * 240}  # Aho promoted to PP1: 4:00/GP vs 2:00 before


def mp_files(season_stats, season):
    sk_cols = [
        "playerId",
        "season",
        "name",
        "team",
        "position",
        "situation",
        "games_played",
        "icetime",
        "I_F_xGoals",
        "I_F_goals",
        "I_F_shotsOnGoal",
        "I_F_points",
    ]
    go_cols = [
        "playerId",
        "season",
        "name",
        "team",
        "position",
        "situation",
        "games_played",
        "icetime",
        "xGoals",
        "goals",
        "ongoal",
    ]
    sk, go = io.StringIO(), io.StringIO()
    ws, wg = csv.writer(sk), csv.writer(go)
    ws.writerow(sk_cols)
    wg.writerow(go_cols)
    year = season // 10000
    for r in season_stats.get("skater/summary", []):
        pid, gp, g = r["playerId"], r["gamesPlayed"], r["goals"]
        pos = "D" if r["positionCode"] == "D" else r["positionCode"]
        ixg = IXG.get((pid, season), round(g * 0.9, 1))
        toi = r["timeOnIcePerGame"] * gp
        pp = PP_SEC.get((pid, season), gp * (120 if r["ppPoints"] >= 20 or season == LIVE else 30))
        for sit, frac in (("all", 1.0), ("5on5", 0.8), ("5on4", None)):
            ice = pp if frac is None else toi * frac
            scale = (ice / toi) if toi else 0
            ws.writerow(
                [
                    pid,
                    year,
                    r["skaterFullName"],
                    r["teamAbbrevs"],
                    pos,
                    sit,
                    gp,
                    round(ice),
                    round(ixg * scale, 2),
                    round(g * scale),
                    round(r["shots"] * scale),
                    r["points"],
                ]
            )
    for r in season_stats.get("goalie/summary", []):
        for sit in ("all", "5on5"):
            f = 1.0 if sit == "all" else 0.8
            wg.writerow(
                [
                    r["playerId"],
                    year,
                    r["goalieFullName"],
                    r["teamAbbrevs"],
                    "G",
                    sit,
                    r["gamesPlayed"],
                    round(r["timeOnIce"] * f),
                    round(r["goalsAgainst"] * 0.95 * f, 2),
                    round(r["goalsAgainst"] * f),
                    round(r["shotsAgainst"] * f),
                ]
            )
    (MP_DIR / f"{year}_skaters.csv").write_text(sk.getvalue())
    (MP_DIR / f"{year}_goalies.csv").write_text(go.getvalue())


for season_key, season_stats in {**stats, **live}.items():
    mp_files(season_stats, int(season_key))

# ------------------------------------------------------------------ Fantrax (real structure)
# Shapes and key names copied from live responses (probe 2026-09-29). Anonymized per the user's
# request: placeholder teams (except "The Blue Blazers"), no owner/manager/handle fields, and only
# the players the tests need. Real Fantrax ids are kept for the ID-mapping traps from the sample.
FX = [  # fantraxId, name, position, team, extras
    ("03rmx", "Aho, Sebastian", "C", "CAR", {"rotowireId": 4900, "statsIncId": 6777}),
    ("03el6", "Aho, Sebastian", "D", "PIT", {"rotowireId": 5572, "statsIncId": 7654}),
    ("05lun", "Aho, Karri", "D", "(N/A)", {}),
    ("048x9", "Pettersson, Elias", "C", "VAN", {"rotowireId": 5418, "statsIncId": 7520}),
    ("060v8", "Pettersson, Elias", "D", "VAN", {"rotowireId": 6803}),
    ("03daz", "Pettersson, Marcus", "D", "NYR", {"rotowireId": 4421, "statsIncId": 6407}),
    ("05uxb", "Pettersson, Hugo", "C", "(N/A)", {}),
    ("074qj", "Pettersson, Hugo", "LW", "(N/A)", {}),
    ("04qz4", "OReilly, Ryan", "RW", "(N/A)", {}),  # namesake with no team: must not steal the real one
    ("02ore", "O'Reilly, Ryan", "C", "NSH", {"statsIncId": 5000}),
    ("04abc", "McDavid, Connor", "C", "EDM", {"rotowireId": 3700}),
    ("05stu", "Stutzle, Tim", "C", "OTT", {}),
    ("06sla", "Slafkovsky, Juraj", "LW", "MTL", {}),
    ("03mar", "Marner, Mitchell", "RW", "VGK", {}),  # synthetic: exercises the nickname tier
    ("03mil", "Miller, JT", "C", "NYR", {}),
    ("05qhu", "Hughes, Quinn", "D", "VAN", {}),
    ("05jhu", "Hughes, Jack", "C", "NJD", {}),
    ("04dem", "Demko, Thatcher", "G", "VAN", {}),
    ("04sar", "Saros, Juuse", "G", "NSH", {}),
    ("07pro", "Prospect, Future", "C", "(N/A)", {}),
    ("08new", "Newby, Rookie", "C", "CAR", {}),  # rookie on an NHL roster, free agent here
]
(F / "fxea_getPlayerIds.json").write_text(
    json.dumps(
        {fid: {"fantraxId": fid, "name": n, "position": p, "team": t, **x} for fid, n, p, t, x in FX},
        indent=1,
    )
)

MINE = "tbb0000000000000"
TEAMS = {MINE: ("The Blue Blazers", "TBB")} | {
    f"team{i:02d}0000000000": (f"Team {i:02d}", f"T{i:02d}") for i in range(2, 11)
}
T2, T3 = "team020000000000", "team030000000000"
ROSTERS = {
    MINE: [
        ("03rmx", "C", "ACTIVE"),
        ("05stu", "C", "ACTIVE"),
        ("03mar", "RW", "ACTIVE"),
        ("060v8", "D", "ACTIVE"),
        ("05qhu", "D", "ACTIVE"),
        ("04sar", "G", "ACTIVE"),
        ("03mil", "C", "RESERVE"),
        ("03daz", "D", "INJURED_RESERVE"),
        ("07pro", "C", "MINORS"),
    ],
    T2: [("04abc", "C", "ACTIVE"), ("03el6", "D", "ACTIVE"), ("048x9", "C", "ACTIVE")],
    T3: [("02ore", "C", "ACTIVE"), ("04dem", "G", "RESERVE")],
}
owner = {fid: tid for tid, items in ROSTERS.items() for fid, _, _ in items}


def side(tid):
    return {"id": tid, "name": TEAMS[tid][0], "shortName": TEAMS[tid][1]}


ids = list(TEAMS)
matchups = []
for period in (1, 2):  # round-robin circle method, two weeks is plenty for tests
    rot = [ids[0]] + ids[1:][period - 1 :] + ids[1:][: period - 1]
    pairs = [(rot[i], rot[-1 - i]) for i in range(5)]
    matchups.append({"matchupList": [{"away": side(a), "home": side(h)} for a, h in pairs], "period": period})


def cfg(points, code, cid, name, short):
    return {
        "cumulative": True,
        "points": points,
        "position": {"code": "DEFAULT", "id": "-1", "name": "Default", "shortName": "Default"},
        "scoringCategory": {"code": code, "id": cid, "name": name, "shortName": short},
    }


league_info = {
    "draftSettings": {"draftType": "snake"},
    "draftType": "snake",
    "endDate": "2027-04-10",
    "leagueHistoryId": "history000000000",
    "leagueName": "Test League",
    "matchups": matchups,
    "playerInfo": {
        fid: {"eligiblePos": "RW,C" if fid == "03mar" else p, "status": owner.get(fid, "FA")}
        for fid, n, p, t, x in FX
    },
    "playoffs": {"used": False},
    "poolSettings": {"duplicatePlayerType": "NONE", "playerSourceType": "ALL_TEAMS"},
    "rosterInfo": {
        "maxTotalActivePlayers": 12,
        "maxTotalPlayers": 28,
        "maxTotalReservePlayers": 6,
        "positionConstraints": {
            "C": {"maxActive": 2},
            "D": {"maxActive": 4},
            "G": {"maxActive": 2},
            "LW": {"maxActive": 2},
            "RW": {"maxActive": 2},
        },
    },
    "rosterPeriods": [
        {"endDate": "2026-10-05T18:59:59.0-0400", "number": 1, "startDate": "2026-09-29T17:00:00.0-0400"},
        {"endDate": "2026-10-12T12:59:59.0-0400", "number": 2, "startDate": "2026-10-05T19:00:00.0-0400"},
    ],
    "scoringPeriods": [
        {"endDate": "2026-10-05T18:59:59.0-0400", "number": 1, "startDate": "2026-09-29T17:00:00.0-0400"},
        {"endDate": "2026-10-12T12:59:59.0-0400", "number": 2, "startDate": "2026-10-05T19:00:00.0-0400"},
    ],
    # Verbatim from the live response: the league's real weights (not personal data).
    "scoringSystem": {
        "scoringCategories": {
            "GOALIE": {
                "GA": {"Default": "points-1"},
                "SHO": {"Default": "points2"},
                "SV": {"Default": "points0.155"},
                "W": {"Default": "points2"},
            },
            "SKATING": {
                "+/-": {"Default": "points0.25"},
                "A": {"Default": "points1.5"},
                "Blk": {"Default": "points0.08"},
                "G": {"Default": "points2"},
                "Hit": {"Default": "points0.08"},
                "PIM": {"Default": "points0.1"},
                "PPP": {"Default": "points0.5"},
                "SOG": {"Default": "points0.1"},
            },
        },
        "scoringCategorySettings": [
            {
                "configs": [
                    cfg(1.5, "INDIVIDUAL_ASSISTS", "2090", "Assists", "A"),
                    cfg(0.08, "INDIVIDUAL_BLOCKS", "2092", "Blocks", "Blk"),
                    cfg(2.0, "INDIVIDUAL_GOALS", "2130", "Goals", "G"),
                    cfg(0.08, "INDIVIDUAL_HITS", "2147", "Hits", "Hit"),
                    cfg(0.1, "INDIVIDUAL_PENALTY_MINUTES", "2170", "Penalty Minutes", "PIM"),
                    cfg(0.25, "INDIVIDUAL_PLUS_MINUS", "2181", "Plus/Minus", "+/-"),
                    cfg(0.1, "INDIVIDUAL_SHOTS_ON_GOAL", "2270", "Shots on Goal", "SOG"),
                    cfg(0.5, "INDIVIDUAL_POWER_PLAY_POINTS", "2327", "Power Play Points", "PPP"),
                ],
                "group": {"code": "HOCKEY_SKATING", "id": "2010", "name": "Skaters", "shortName": "Skt"},
            },
            {
                "configs": [
                    cfg(-1.0, "INDIVIDUAL_GOALS_AGAINST", "2140", "Goals Against", "GA"),
                    cfg(0.155, "INDIVIDUAL_SAVES", "2230", "Saves", "SV"),
                    cfg(2.0, "INDIVIDUAL_SHUTOUTS", "2290", "Shutouts", "SHO"),
                    cfg(2.0, "INDIVIDUAL_GOALIE_WINS", "231b", "Wins (Goalies only)", "W"),
                ],
                "group": {"code": "HOCKEY_GOALIE", "id": "2020", "name": "Goalies", "shortName": "Goal"},
            },
        ],
        "type": "HEAD_TO_HEAD_POINTS_BASED",
    },
    "seasonYear": 2026,
    "startDate": "2026-09-29",
    "teamInfo": {tid: {"id": tid, "name": name} for tid, (name, short) in TEAMS.items()},
}
(F / "fxea_getLeagueInfo.json").write_text(json.dumps(league_info, indent=1))

rosters = {
    "period": 1,
    "rosters": {
        tid: {
            "rosterItems": [
                {"id": fid, "position": pos, "status": st} for fid, pos, st in ROSTERS.get(tid, [])
            ],
            "teamName": name,
        }
        for tid, (name, short) in TEAMS.items()
    },
}
(F / "fxea_getTeamRosters.json").write_text(json.dumps(rosters, indent=1))
(F / "fxea_getStandings.json").write_text(
    json.dumps([{"teamId": tid, "rank": i} for i, tid in enumerate(TEAMS, 1)], indent=1)
)
(F / "fxea_error.json").write_text(json.dumps({"error": "This league is not public"}))

# Fantrax Players-page CSV (stats view); FPts computed with the league's real weights.
w_sk = {"G": 2, "A": 1.5, "+/-": 0.25, "PIM": 0.1, "PPP": 0.5, "SOG": 0.1, "Hit": 0.08, "Blk": 0.08}
w_g = {"W": 2, "GA": -1, "SV": 0.155, "SHO": 2}
rows = [
    (
        "*04abc*",
        "Connor McDavid",
        "EDM",
        "C",
        "T02",
        {"GP": 80, "G": 48, "A": 90, "+/-": 20, "PIM": 30, "PPP": 45, "SOG": 320, "Hit": 40, "Blk": 30},
    ),
    (
        "*03rmx*",
        "Sebastian Aho",
        "CAR",
        "C,LW",
        "TBB",
        {"GP": 80, "G": 35, "A": 45, "+/-": 10, "PIM": 20, "PPP": 28, "SOG": 230, "Hit": 40, "Blk": 30},
    ),
    (
        "*05qhu*",
        "Quinn Hughes",
        "VAN",
        "D",
        "TBB",
        {"GP": 74, "G": 16, "A": 70, "+/-": 18, "PIM": 20, "PPP": 35, "SOG": 190, "Hit": 20, "Blk": 70},
    ),
    (
        "*05jhu*",
        "Jack Hughes",
        "NJD",
        "C",
        "FA",
        {"GP": 62, "G": 27, "A": 45, "+/-": 3, "PIM": 14, "PPP": 25, "SOG": 230, "Hit": 20, "Blk": 20},
    ),
    (
        "*04sar*",
        "Juuse Saros",
        "NSH",
        "G",
        "TBB",
        {"GP": 58, "W": 26, "GA": 170, "SV": 1450, "SHO": 2, "GAA": 2.93},
    ),
]
cols = [
    "ID",
    "Player",
    "Team",
    "Position",
    "RkOv",
    "Status",
    "Age",
    "Opponent",
    "FPts",
    "FP/G",
    "GP",
    "G",
    "A",
    "+/-",
    "PIM",
    "PPP",
    "SOG",
    "Hit",
    "Blk",
    "W",
    "GA",
    "SV",
    "SHO",
    "GAA",
]

buf = io.StringIO()
wr = csv.writer(buf)
wr.writerow(cols)
for i, (fid, name, team, pos, status, st) in enumerate(rows, 1):
    w = w_g if pos == "G" else w_sk
    fpts = round(sum(st.get(k, 0) * v for k, v in w.items()), 2)
    wr.writerow(
        [fid, name, team, pos, i, status, 25, "", fpts, round(fpts / st["GP"], 2)]
        + [st.get(c, "") for c in cols[10:]]
    )
(F / "fantrax_players.csv").write_text(buf.getvalue())
print("fixtures written")

# ------------------------------------------------------------------ schedule, roster period 2
# Period 2 runs 2026-10-05 19:00 ET (23:00 UTC) to 2026-10-12 12:59:59 ET (16:59:59 UTC).
GAMES = [  # startTimeUTC, away, home, gameType
    ("2026-10-05T22:59:00Z", "MTL", "TOR", 2),  # 18:59 ET: before the lock -> not in period 2
    ("2026-10-05T23:00:00Z", "BOS", "OTT", 2),  # 19:00 ET: exactly at the lock -> counts
    ("2026-10-06T23:00:00Z", "NSH", "CAR", 2),
    ("2026-10-06T23:30:00Z", "CAR", "WSH", 1),  # preseason game type: ignored
    ("2026-10-07T02:00:00Z", "VAN", "EDM", 2),
    ("2026-10-08T02:00:00Z", "VGK", "VAN", 2),  # VAN back-to-back (24 h later)
    ("2026-10-08T23:00:00Z", "CAR", "PIT", 2),
    ("2026-10-08T23:30:00Z", "OTT", "NJD", 2),
    ("2026-10-09T23:00:00Z", "EDM", "NSH", 2),
    ("2026-10-10T02:00:00Z", "NJD", "VGK", 2),
    ("2026-10-10T23:00:00Z", "PIT", "CAR", 2),
    ("2026-10-11T19:00:00Z", "CAR", "OTT", 2),  # CAR back-to-back
    ("2026-10-11T23:00:00Z", "NSH", "EDM", 2),
    ("2026-10-12T02:00:00Z", "VGK", "NJD", 2),
    ("2026-10-12T17:00:00Z", "VAN", "NYR", 2),  # 13:00 ET: after period 2 ends
]
days: dict[str, list] = {}
for i, (start, away, home, gtype) in enumerate(GAMES):
    local_day = start[:10]
    days.setdefault(local_day, []).append(
        {
            "id": 2026020000 + i,
            "season": 20262027,
            "gameType": gtype,
            "startTimeUTC": start,
            "awayTeam": {"abbrev": away},
            "homeTeam": {"abbrev": home},
        }
    )
(F / "nhl_schedule.json").write_text(
    json.dumps([{"date": d, "games": g} for d, g in sorted(days.items())], indent=1)
)

# ------------------------------------------------------------------ player landing pages (career GP)
CAREER_GP = {
    8478402: 750,
    8478427: 700,
    8480222: 420,
    8477969: 600,
    8480012: 610,
    8483678: 100,
    8482116: 360,
    8483515: 300,
    8478483: 700,
    8476468: 950,
    8475158: 1150,
    8480800: 480,
    8481559: 440,
    8477967: 320,
    8477424: 450,
    8484999: 6,  # 8485555 (Newby) has no NHL games: no careerTotals at all
}
landing = {}
for pid, fn, ln, pos, team, bd in NHL:
    page = {
        "playerId": pid,
        "firstName": {"default": fn},
        "lastName": {"default": ln},
        "position": pos,
        "birthDate": bd,
        "currentTeamAbbrev": team,
    }
    if pid in CAREER_GP:
        page["careerTotals"] = {"regularSeason": {"gamesPlayed": CAREER_GP[pid], "goals": 0}}
    landing[str(pid)] = page
(F / "nhl_landing.json").write_text(json.dumps(landing, indent=1, ensure_ascii=False))
