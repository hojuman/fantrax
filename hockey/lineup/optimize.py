"""Exact lineup assignment.

Dynamic programming over players with state = slots filled per position (C 0-2, LW 0-2, RW 0-2,
D 0-4, G 0-2: 405 states). Each player either sits or takes one slot among his eligible positions.
Filling a slot with a zero-value player (no games) beats leaving it empty by a hair, so empty slots
only happen when nobody eligible is left, and zero-game starters get flagged by the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SLOTS = {"C": 2, "LW": 2, "RW": 2, "D": 4, "G": 2}
FILL_BONUS = 1e-6


@dataclass(frozen=True)
class Candidate:
    key: str
    positions: tuple[str, ...]
    value: float


@dataclass
class Assignment:
    slot_of: dict[str, str] = field(default_factory=dict)  # candidate key -> position slot
    total: float = 0.0
    empty: dict[str, int] = field(default_factory=dict)  # position -> unfilled slots


def _offer(layer: dict, state: tuple, score: float, back: tuple, pos: str | None) -> None:
    if state not in layer or score > layer[state][0] + 1e-12:
        layer[state] = (score, back, pos)


def optimize(cands: list[Candidate], slots: dict[str, int] | None = None) -> Assignment:
    slots = slots or SLOTS
    order = list(slots)
    start = tuple(0 for _ in order)
    # layer: state -> (score, back-pointer (prev_state, prev_layer_index, chosen position or None))
    layers: list[dict[tuple, tuple[float, tuple | None, str | None]]] = [{start: (0.0, None, None)}]
    for c in cands:
        prev = layers[-1]
        cur: dict[tuple, tuple[float, tuple | None, str | None]] = {}

        for state, (score, _, _) in prev.items():
            _offer(cur, state, score, state, None)  # sit
            for pos in c.positions:
                if pos not in slots:
                    continue
                i = order.index(pos)
                if state[i] < slots[pos]:
                    nxt = state[:i] + (state[i] + 1,) + state[i + 1 :]
                    _offer(cur, nxt, score + c.value + FILL_BONUS, state, pos)
        layers.append(cur)

    final = layers[-1]
    best_state = max(final, key=lambda s: final[s][0])
    result = Assignment()
    state = best_state
    for idx in range(len(cands), 0, -1):
        _, back, pos = layers[idx][state]
        if pos is not None:
            result.slot_of[cands[idx - 1].key] = pos
            result.total += cands[idx - 1].value
        state = back
    result.empty = {
        pos: slots[pos] - best_state[i] for i, pos in enumerate(order) if best_state[i] < slots[pos]
    }
    return result
