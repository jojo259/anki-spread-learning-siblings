"""Bounded planning over lightweight card metadata; no Anki imports or writes."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter


@dataclass(frozen=True, slots=True)
class Candidate:
    cid: int
    nid: int
    due: int


@dataclass(frozen=True, slots=True)
class Settings:
    enabled: bool = True
    extra_priority_after_minutes: int = 60
    overdue_weight: float = 0.25

    @classmethod
    def from_dict(cls, value):
        value = value or {}
        try:
            return cls(
                enabled=bool(value.get('enabled', True)),
                extra_priority_after_minutes=max(0, min(1440, int(value.get('extra_priority_after_minutes', 60)))),
                overdue_weight=max(0.0, min(1.0, float(value.get('overdue_weight', .25)))),
            )
        except (TypeError, ValueError, OverflowError):
            return cls()

    def to_dict(self):
        return {'enabled': self.enabled,
                'extra_priority_after_minutes': self.extra_priority_after_minutes,
                'overdue_weight': self.overdue_weight}


class Planner:
    """Choose one card using a bounded beam search, then replan after an answer.

    The hard sibling rule is checked against ALL due note groups before search
    limits are applied. Time limits can reduce spacing quality, never weaken
    that rule. Within each note, candidates retain oldest-due order.
    """

    def __init__(self, *, depth=6, width=12, branch=8, budget_ms=4.0):
        self.depth = depth
        self.width = width
        self.branch = branch
        self.budget_ms = budget_ms

    def choose(self, cards, history, now, settings):
        if not cards:
            return None
        started = perf_counter()
        groups = {}
        for card in cards:
            groups.setdefault(card.nid, []).append(card)
        for items in groups.values():
            items.sort(key=lambda c: (c.due, c.cid))
        last = history[-1] if history else None
        eligible = [nid for nid in groups if nid != last] or list(groups)
        positions = {}
        for pos, nid in enumerate(history[-64:], start=-min(64, len(history))):
            positions.setdefault(nid, []).append(pos)
        grace = settings.extra_priority_after_minutes * 60
        ages = {nid: [max(0.0, (now-c.due-grace)/3600) for c in items]
                for nid, items in groups.items()}

        def penalty(nid, pos, past):
            return sum(1.0 / (pos-old)**2 for old in past.get(nid, ()))

        # A valid fallback is selected before the bounded search. It is also
        # sufficient when each remaining note has just one card.
        def root_key(nid):
            c = groups[nid][0]
            return (penalty(nid, 0, positions)-settings.overdue_weight*ages[nid][0], c.due, c.cid)

        ranked = sorted(eligible, key=root_key)
        fallback = groups[ranked[0]][0]
        if len(groups) == 1 or all(len(v) == 1 for v in groups.values()):
            return fallback.cid
        if (perf_counter()-started)*1000 >= self.budget_ms:
            return fallback.cid

        # Limit search breadth while retaining both urgent and populous notes.
        universe = list(dict.fromkeys(
            ranked[:32] + sorted(groups, key=lambda n: (-len(groups[n]), groups[n][0].due))[:16]
        ))
        index = {nid: i for i, nid in enumerate(universe)}
        total_age = sum(sum(a) for a in ages.values())
        # cost, card path, count-used tuple, last note, past positions, remaining age
        beam = [(0.0, (), (0,)*len(universe), last, positions, total_age)]
        best = fallback.cid
        deadline = started + self.budget_ms/1000
        due_by_id = {c.cid: c.due for c in cards}
        for pos in range(min(self.depth, len(cards))):
            next_beam = []
            expired = False
            for cost, path, used, previous, past, remaining_age in beam:
                if perf_counter() >= deadline:
                    expired = True
                    break
                available = [n for n in universe if used[index[n]] < len(groups[n])]
                different = [n for n in available if n != previous]
                choices = different or available
                if not choices:
                    continue
                options = []
                for nid in choices:
                    i = index[nid]
                    card = groups[nid][used[i]]
                    left_age = remaining_age-ages[nid][used[i]]
                    # Charging ALL unserved age at each step equals the full
                    # sum(position * age). It avoids short lookahead mistakenly
                    # preferring fresh cards simply by omitting older cards.
                    step = penalty(nid, pos, past) + settings.overdue_weight*left_age
                    options.append((step, card.due, card.cid, nid, left_age))
                options.sort()
                selected = options[:self.branch]
                populous = sorted(options, key=lambda x: -(len(groups[x[3]])-used[index[x[3]]]))[:2]
                selected += [x for x in populous if x not in selected]
                for step, due, cid, nid, left_age in selected:
                    new_used = list(used)
                    new_used[index[nid]] += 1
                    new_past = dict(past)
                    new_past[nid] = past.get(nid, []) + [pos]
                    next_beam.append((cost+step, path+(cid,), tuple(new_used), nid, new_past, left_age))
            if expired or not next_beam:
                break
            # Older cards resolve equal-cost arrangements deterministically.
            next_beam.sort(key=lambda s: (s[0], tuple(due_by_id[c] for c in s[1]), s[1]))
            beam = next_beam[:self.width]
            best = beam[0][1][0]
        # The first step always obeys the global hard constraint, even when
        # planning stopped early or a group was excluded from lookahead.
        chosen = next(c for c in cards if c.cid == best)
        if chosen.nid == last and any(c.nid != last for c in cards):
            return fallback.cid
        return best
