import datetime

from hockey.probe import describe_scoring, run_probe, sample_player_entry, terms_sentences, value_counts
from tests.conftest import FakeHttp, load

MONEYPUCK_LIKE = """<html><head><title>MoneyPuck.com -Download Data</title>
<style> h1 { font: 35px Helvetica; } body { min-height: 2000px; } </style>
<script>var x = "free to use";</script></head>
<body><h1>Download</h1><p>The data below is free to use for non-commercial purposes and by journalists
for ad-hoc use. Please clearly credit MoneyPuck.com in all cases where you are showing anything using our
data as an input.</p><p>There are 124 attributes for each shot.</p></body></html>"""


def test_terms_sentences_ignore_css_and_scripts():
    s = terms_sentences(MONEYPUCK_LIKE)
    assert s[0].startswith("The data below is free to use for non-commercial purposes")
    assert s[1].startswith("Please clearly credit MoneyPuck.com")
    assert not any("font" in x or "var x" in x for x in s)


def test_describe_scoring_ok_and_failure():
    ok = describe_scoring({"skater": {"G": 3, "BkS": 0.5}, "goalie": {"W": 4}})
    assert "G=3" in ok and "all codes mapped ✓" in ok
    bad = describe_scoring({"skater": {"G": 3, "Dangles": 1}})
    assert "MAPPING FAILED" in bad and "Dangles" in bad
    fixed = describe_scoring({"skater": {"Dangles": 1}}, {"scoring": {"code_aliases": {"Dangles": "g"}}})
    assert "all codes mapped ✓" in fixed
    assert "NOT FOUND" in describe_scoring(None)


def test_sample_player_and_value_counts():
    assert '"fantraxId": "04abc"' in sample_player_entry(load("fxea_getPlayerIds.json"))
    assert "no entry" in sample_player_entry({}, "nobody")
    assert value_counts([{"s": "A"}, {"s": "A"}, {"s": "B"}], "s") == "A×2, B×1"


def test_probe_end_to_end_offline(tmp_path):
    class Probe(FakeHttp):
        def get(self, url, params=None, **kw):
            if "moneypuck.com" in url:
                from hockey.http import Response

                return Response(url, 200, MONEYPUCK_LIKE, False)
            if "/schedule/" in url:
                from hockey.http import Response

                return Response(url, 200, '{"gameWeek": []}', False)
            return super().get(url, params, **kw)

    results = {r.name: r for r in run_probe(Probe(), "testleague", tmp_path, datetime.date(2026, 9, 29))}
    assert all(r.ok for r in results.values()), [r for r in results.values() if not r.ok]
    assert "all codes mapped ✓" in results["fxea getLeagueInfo"].detail
    assert "status: ACTIVE×" in results["fxea getTeamRosters"].detail
    assert "with an NHL team: 14" in results["fxea getPlayerIds"].detail
    assert "NHL roster/VAN/20262027" in results
    assert "non-commercial" in results["MoneyPuck data.htm (terms)"].detail
    assert (tmp_path / "fxea_getLeagueInfo.json").exists()
