"""Name normalization for Fantrax <-> NHL matching."""

from __future__ import annotations

import re

from unidecode import unidecode

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Short/nick first names -> the form we compare on. Only unambiguous ones: "Max" could be Maxim,
# Maxime or Maximilian, so it's left to the fuzzy tier / overrides.
NICKNAMES = {
    "mitch": "mitchell",
    "alex": "alexander",
    "matt": "matthew",
    "matty": "matthew",
    "nick": "nicholas",
    "nic": "nicholas",
    "zach": "zachary",
    "zack": "zachary",
    "mike": "michael",
    "chris": "christopher",
    "tony": "anthony",
    "josh": "joshua",
    "jake": "jacob",
    "joe": "joseph",
    "sam": "samuel",
    "will": "william",
    "dan": "daniel",
    "danny": "daniel",
    "tom": "thomas",
    "tommy": "thomas",
    "nate": "nathan",
    "cam": "cameron",
    "jon": "jonathan",
    "pat": "patrick",
    "ben": "benjamin",
    "vince": "vincent",
    "gabe": "gabriel",
    "freddie": "frederik",
    "fred": "frederick",
    "andy": "andrew",
    "drew": "andrew",
    "rob": "robert",
    "bob": "robert",
    "tim": "timothy",
    "jim": "james",
    "jimmy": "james",
    "evgenii": "evgeny",
    "yevgeni": "evgeny",
    "egor": "yegor",
    "ilia": "ilya",
    "alexei": "alexey",
    "aleksei": "alexey",
}


def basic(name: str) -> str:
    """Accent-folded, lowercase, punctuation removed: 'Tim Stützle' -> 'tim stutzle',
    "Ryan O'Reilly" -> 'ryan oreilly', 'J.T. Miller' -> 'jt miller', 'Ekman-Larsson' -> 'ekman larsson'."""
    s = unidecode(name).lower()
    s = re.sub(r"['.’`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def canonical(name: str) -> str:
    """basic() plus suffix stripping and nickname expansion of the first name."""
    parts = [p for p in basic(name).split() if p not in SUFFIXES]
    if parts:
        parts[0] = NICKNAMES.get(parts[0], parts[0])
    return " ".join(parts)
