import importlib
from pathlib import Path
import random
import sys
import time
import types
import unittest

root = Path(__file__).resolve().parents[1]
package = types.ModuleType('sls_tests')
package.__path__ = [str(root)]
sys.modules['sls_tests'] = package
from sls_tests.planner import Candidate, Planner, Settings


class Planning(unittest.TestCase):
    def cards(self, groups, now=1_000_000):
        cards = []
        for nid, times in enumerate(groups.values()):
            for minutes in times:
                cards.append(Candidate(len(cards)+1, nid, now-int(minutes*60)))
        return cards

    def sequence(self, groups, history=()):
        now = 1_000_000
        cards = self.cards(groups, now)
        letters = list(groups)
        history = [letters.index(n) for n in history]
        planner = Planner(depth=8, width=32, budget_ms=200)
        result = []
        while cards:
            cid = planner.choose(cards, history, now, Settings())
            card = next(c for c in cards if c.cid == cid)
            if history and any(c.nid != history[-1] for c in cards):
                self.assertNotEqual(card.nid, history[-1])
            self.assertEqual(card.due, min(c.due for c in cards if c.nid == card.nid))
            result.append(letters[card.nid])
            history.append(card.nid)
            cards.remove(card)
        return ''.join(result)

    def test_balanced_groups(self):
        self.assertEqual(self.sequence({'A':[55,50], 'B':[45,40], 'C':[35,30]}), 'ABCABC')

    def test_dominant_group_and_tail(self):
        self.assertEqual(self.sequence({'A':[55,54,53,52,51], 'B':[40], 'C':[10]}), 'ABACAAA')

    def test_three_siblings_use_both_other_cards(self):
        self.assertEqual(self.sequence({'A':[55,50,45], 'B':[40], 'C':[10]}), 'ABACA')

    def test_old_age_cannot_override_hard_rule(self):
        self.assertEqual(self.sequence({'A':[480,420,80], 'B':[40], 'C':[10]}), 'ABACA')

    def test_previous_card_is_respected(self):
        self.assertEqual(self.sequence({'A':[55,50], 'B':[40], 'C':[10]}, ['A']), 'BACA')

    def test_grace_and_age_priority(self):
        self.assertEqual(self.sequence({'A':[80,70], 'B':[360,300], 'C':[15,10]}), 'BABCAC')

    def test_single_group_always_progresses(self):
        self.assertEqual(self.sequence({'A':[10,9,8]}, ['A']), 'AAA')

    def test_zero_budget_still_enforces_rule(self):
        cards = self.cards({'A':[999,900], 'B':[0]})
        cid = Planner(budget_ms=0).choose(cards, [0], 1_000_000, Settings())
        self.assertEqual(next(c.nid for c in cards if c.cid == cid), 1)

    def test_alternative_far_beyond_any_search_prefix(self):
        cards = [Candidate(i+1, 0, 1_000_000-5000+i) for i in range(10000)]
        cards.append(Candidate(10001, 1, 999999))
        cid = Planner().choose(cards, [0], 1_000_000, Settings())
        self.assertEqual(cid, 10001)

    def test_randomized_hard_rule_and_oldest_sibling_invariants(self):
        rng = random.Random(20261110)
        planner = Planner(depth=4, width=8, budget_ms=1)
        for _ in range(150):
            cards = [Candidate(i+1, rng.randrange(8), 1_000_000-rng.randrange(10000))
                     for i in range(rng.randrange(1, 30))]
            previous = rng.randrange(8)
            seen = set()
            while cards:
                cid = planner.choose(cards, [previous], 1_000_000, Settings())
                chosen = next(c for c in cards if c.cid == cid)
                self.assertNotIn(cid, seen)
                if any(c.nid != previous for c in cards):
                    self.assertNotEqual(chosen.nid, previous)
                self.assertEqual(chosen.due, min(c.due for c in cards if c.nid == chosen.nid))
                seen.add(cid)
                previous = chosen.nid
                cards.remove(chosen)

    def test_settings_validation(self):
        self.assertEqual(Settings.from_dict({'extra_priority_after_minutes':'invalid'}), Settings())
        self.assertEqual(Settings.from_dict({'extra_priority_after_minutes':999999}).extra_priority_after_minutes, 1440)

    def test_large_queue_planning_budget(self):
        planner = Planner()
        for n in (100, 1000, 10000):
            cards = [Candidate(i+1, i//2, 1_000_000-10000+i//2) for i in range(n)]
            measurements = []
            for _ in range(5):
                started = time.perf_counter()
                cid = planner.choose(cards, [0], 1_000_000, Settings())
                measurements.append((time.perf_counter()-started)*1000)
                self.assertNotEqual(next(c.nid for c in cards if c.cid == cid), 0)
            print('Planner', n, 'cards: median ms', round(sorted(measurements)[2], 3),
                  'max ms', round(max(measurements), 3))
            self.assertLess(max(measurements), 100, 'Planning exceeded the responsiveness limit')


if __name__ == '__main__':
    unittest.main(verbosity=2)
