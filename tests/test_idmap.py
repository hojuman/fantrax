import pytest

from hockey.idmap.match import FxRef, Match, Matcher, NhlRef, Unmatched, load_overrides
from hockey.idmap.normalize import basic, canonical

NHL = [
    NhlRef(8478402, "Connor McDavid", "F", "EDM"),
    NhlRef(8478427, "Sebastian Aho", "F", "CAR"),
    NhlRef(8480222, "Sebastian Aho", "D", "NYI"),
    NhlRef(8480012, "Elias Pettersson", "F", "VAN"),
    NhlRef(8483678, "Elias Pettersson", "D", "VAN"),
    NhlRef(8482116, "Tim Stützle", "F", "OTT"),
    NhlRef(8483515, "Juraj Slafkovský", "F", "MTL"),
    NhlRef(8478483, "Mitch Marner", "F", "VGK"),
    NhlRef(8476468, "J.T. Miller", "F", "NYR"),
    NhlRef(8475158, "Ryan O'Reilly", "F", "NSH"),
    NhlRef(8477424, "Juuse Saros", "G", "NSH"),
    NhlRef(8470000, "Brent Burns", "D", "COL"),
    NhlRef(8481000, "Alexander Holtz", "F", "VGK"),
    NhlRef(8482000, "Matthew Knies", "F", "TOR"),
    NhlRef(8483000, "Egor Chinakhov", "F", "CBJ"),
    NhlRef(8484000, "Max Jones", "F", "BOS"),
    NhlRef(8484001, "Max Jones", "F", "BOS"),
]


@pytest.fixture(scope="module")
def m():
    return Matcher(NHL)


def fx(fid, name, pos, team, **extra):
    return FxRef(fid, name, pos, team, extra)


def test_normalize():
    assert basic("Tim Stützle") == "tim stutzle"
    assert basic("J.T. Miller") == basic("JT Miller") == "jt miller"
    assert basic("Ryan O'Reilly") == "ryan oreilly"
    assert basic("Oliver Ekman-Larsson") == "oliver ekman larsson"
    assert canonical("Mitch Marner") == canonical("Mitchell Marner")
    assert canonical("Martin St. Louis Jr.") == "martin st louis"


@pytest.mark.parametrize(
    "ref,nhl_id,method",
    [
        (("a", "Sebastian Aho", "F", "CAR"), 8478427, "exact"),
        (("b", "Sebastian Aho", "D", "NYI"), 8480222, "exact"),
        (("c", "Elias Pettersson", "F", "VAN"), 8480012, "exact"),  # same name, same team...
        (("d", "Elias Pettersson", "D", "VAN"), 8483678, "exact"),  # ...position separates them
        (("e", "Tim Stutzle", "F", "OTT"), 8482116, "exact"),  # accents
        (("f", "Juraj Slafkovsky", "F", "MTL"), 8483515, "exact"),
        (("g", "JT Miller", "F", "NYR"), 8476468, "exact"),
        (("h", "Mitchell Marner", "F", "TOR"), 8478483, "alias"),  # nickname + stale team
        (("i", "Brent Burns", "F", "COL"), 8470000, "exact_anypos"),  # listed F on Fantrax, D in NHL
        (("j", "Alex Holtz", "F", None), 8481000, "alias"),  # free agent: no team
        (("k", "Matt Knies", "F", "TOR"), 8482000, "alias"),
        (("l", "Yegor Chinakhov", "F", "CBJ"), 8483000, "alias"),  # transliteration
        (("m", "Juuse Saros", "G", "NSH"), 8477424, "exact"),
    ],
)
def test_cascade(m, ref, nhl_id, method):
    r = m.match(fx(*ref))
    assert isinstance(r, Match), r
    assert (r.nhl_id, r.method) == (nhl_id, method)


def test_fuzzy_is_flagged(m):
    r = m.match(fx("z", "Conor McDavid", "F", "EDM"))
    assert isinstance(r, Match) and r.method == "fuzzy" and r.confidence < 0.9


def test_ambiguous_is_unmatched_with_candidates(m):
    r = m.match(fx("x", "Max Jones", "F", "BOS"))
    assert isinstance(r, Unmatched) and "ambiguous" in r.reason and len(r.candidates) == 2


def test_prospect_unmatched(m):
    r = m.match(fx("p", "Future Prospect", "F", None))
    assert isinstance(r, Unmatched) and r.reason == "no NHL player with this name"


def test_override_wins(m):
    r = m.match(fx("x", "Max Jones", "F", "BOS"), overrides={"x": 8484001})
    assert (r.nhl_id, r.method) == (8484001, "override")


def test_external_id_tier(m):
    r = m.match(fx("q", "Somebody Else", "F", None, nhlId="8478402"))
    assert (r.nhl_id, r.method) == (8478402, "external_id")


def test_sticky_keeps_previous(m):
    # Previously mapped to the NYI Aho; stays put even though the name alone is ambiguous.
    r = m.match(fx("b", "Sebastian Aho", "D", None), previous={"b": 8480222})
    assert (r.nhl_id, r.method) == (8480222, "sticky")


def test_sticky_dropped_when_names_diverge(m):
    r = m.match(fx("a", "Sebastian Aho", "F", "CAR"), previous={"a": 8478402})  # McDavid?!
    assert (r.nhl_id, r.method) == (8478427, "exact")


def test_conflicting_claims_demoted(m):
    matched, unmatched = m.match_all(
        [
            fx("a", "Connor McDavid", "F", "EDM"),
            fx("b", "Conor McDavid", "F", "EDM"),  # fuzzy duplicate
        ]
    )
    assert [x.fantrax_id for x in matched] == ["a"]
    assert unmatched[0].fantrax_id == "b" and "conflict" in unmatched[0].reason


def test_load_overrides(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("fantrax_id,nhl_id,note\nabc,8478402,McJesus\n,,\n")
    assert load_overrides(p) == {"abc": 8478402}
    assert load_overrides(tmp_path / "missing.csv") == {}
