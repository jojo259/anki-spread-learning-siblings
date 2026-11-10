"""Real Anki reviewer integration against temporary synthetic collections."""

import argparse
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument('--anki-path', required=True)
parser.add_argument('--true-random-path')
parser.add_argument('--order', choices=('native', 'tr-first', 'sls-first'), default='native')
parser.add_argument('--screenshot')
args, remaining = parser.parse_known_args()
anki = Path(args.anki_path)
handles = [os.add_dll_directory(str(anki)), os.add_dll_directory(str(anki/'app_packages/pywin32_system32'))]
sys.path.insert(0, str(anki/'app_packages'))
for extra in ('win32', 'win32/lib', 'Pythonwin', 'pywin32_system32'):
    sys.path.append(str(anki/'app_packages'/extra))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if sys.platform == 'win32':
    os.environ.setdefault('QT_QPA_FONTDIR', r'C:\Windows\Fonts')

from anki.collection import Collection, AddNoteRequest
from anki.scheduler.v3 import CardAnswer
from anki import buildinfo
import anki.lang
anki.lang.set_lang('en')
import aqt
from aqt import gui_hooks
from aqt.reviewer import Reviewer

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT.parents[1] / 'work'
if not WORK.exists():
    WORK = ROOT / '.test-work'
    WORK.mkdir(exist_ok=True)


def package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


package('sls_test', ROOT)
sls = importlib.import_module('sls_test.addon')
from sls_test.engine import materialize
from sls_test.planner import Settings, Planner
tr = None
if args.order != 'native':
    if not args.true_random_path:
        raise RuntimeError('--true-random-path is required for compatibility tests')
    package('tr_test', Path(args.true_random_path))
    tr = importlib.import_module('tr_test.addon')
    if args.order == 'tr-first':
        tr.install()
        sls.install()
    else:
        sls.install()
        tr.install()
else:
    sls.install()


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sls-integration-', dir=WORK)
        self.path = Path(self.tmp.name).resolve() / 'collection.anki2'
        assert self.path.is_relative_to(WORK.resolve())
        self.col = Collection(str(self.path))
        prefs = self.col._backend.get_preferences()
        prefs.scheduling.learn_ahead_secs = 0
        self.col._backend.set_preferences(prefs)
        self.col.set_config('fsrs', False)
        sls.engine.configure(Settings())
        self.r = Reviewer.__new__(Reviewer)
        self.r.mw = types.SimpleNamespace(col=self.col)
        self.r.card = None
        self.r._v3 = None
        self.mw = types.SimpleNamespace(col=self.col, reviewer=self.r)
        aqt.mw = self.mw
        self.ids = []
        self.notes = []
        model = self.col.models.by_name('Basic (and reversed card)')
        for index in range(3):
            note = self.col.new_note(model)
            note['Front'] = f'Synthetic note {index}'
            note['Back'] = f'Synthetic answer {index}'
            self.col.add_note(note, 1)
            self.notes.append(note.id)
            self.ids.extend(c.id for c in note.cards())
        cards = [self.col.get_card(cid) for cid in self.ids]
        for i, card in enumerate(cards):
            card.queue = card.type = 1
            card.reps = 1
            card.left = 2002
            card.due = int(time.time())-3300+i*5
        self.col.update_cards(cards)

    def tearDown(self):
        aqt.mw = None
        self.col.close()
        self.tmp.cleanup()

    def rows(self):
        return self.col.db.all('select * from cards order by id')

    def fetch(self, show=True):
        self.r.card = None
        self.r._v3 = None
        self.r._get_next_v3_card()
        self.assertFalse(sls.engine.session(self.col).failed, 'Policy paused unexpectedly')
        if show and self.r.card:
            gui_hooks.reviewer_did_show_question(self.r.card)
        return self.r.card

    def answer(self, rating=CardAnswer.EASY):
        card = self.r.card
        answer = self.col.sched.build_answer(card=card, states=self.r._v3.states, rating=rating)
        changes = self.col.sched.answer_card(input=answer)
        gui_hooks.operation_did_execute(changes, self.r)
        gui_hooks.reviewer_did_answer_card(self.r, card, int(rating)+1)

    def filtered(self, mixed=False, preview=False, second=False):
        if mixed:
            cards = [self.col.get_card(cid) for cid in self.ids]
            cards[2].queue = cards[2].type = 0
            cards[2].due = 1
            cards[3].queue = cards[3].type = 2
            cards[3].due = self.col.sched.today
            cards[3].ivl = 7
            cards[3].factor = 2500
            self.col.update_cards(cards)
        deck = self.col.sched.get_or_create_filtered_deck(0)
        deck.name = 'Synthetic filtered'
        deck.config.reschedule = not preview
        deck.config.search_terms[0].search = 'deck:Default'
        deck.config.search_terms[0].limit = 10000
        if second:
            deck.config.search_terms[0].search += ' is:learn'
            deck.config.search_terms[1].search = 'deck:Default -is:learn'
            deck.config.search_terms[1].limit = 10000
        else:
            del deck.config.search_terms[1:]
        did = self.col.sched.add_or_update_filtered_deck(deck).id
        if tr:
            tr.set_enabled(self.col, did, True)
            self.col.sched._true_random_session.rng.seed(100)
        return did

    def test_fetch_is_read_only_and_current_card_stays_stable(self):
        before = self.rows()
        status = self.col._backend.get_undo_status().SerializeToString()
        card = self.fetch()
        history = tuple(sls.engine.session(self.col).history)
        for _ in range(4):
            self.assertEqual(self.fetch().id, card.id)
            self.assertEqual(tuple(sls.engine.session(self.col).history), history)
        self.assertEqual(before, self.rows())
        self.assertEqual(status, self.col._backend.get_undo_status().SerializeToString())

    def test_whole_learning_session_hard_rule_undo_redo(self):
        snapshots = []
        last = None
        for _ in self.ids:
            ready = sls.engine.rows(self.col, False, int(time.time())) if last else None
            # The first native fetch initializes active_decks.
            before = self.rows()
            card = self.fetch()
            if ready and any(row[1] != last for row in ready):
                self.assertNotEqual(card.nid, last)
            cid = card.id
            self.answer()
            snapshots.append((cid, before, self.rows()))
            last = card.nid
        self.assertIsNone(self.fetch())
        for cid, before, after in reversed(snapshots):
            out = self.col.undo()
            gui_hooks.operation_did_execute(out.changes, None)
            self.assertEqual(self.rows(), before)
            self.assertEqual(self.fetch().id, cid)
        for cid, before, after in snapshots:
            out = self.col.redo()
            gui_hooks.operation_did_execute(out.changes, None)
            self.assertEqual(self.rows(), after)

    def test_every_answer_button_fsrs_learning_relearning(self):
        for fsrs in (False, True):
            self.col.set_config('fsrs', fsrs)
            for kind in (1, 3):
                for rating in (CardAnswer.AGAIN, CardAnswer.HARD, CardAnswer.GOOD, CardAnswer.EASY):
                    with self.subTest(fsrs=fsrs, kind=kind, rating=rating):
                        cards = [self.col.get_card(cid) for cid in self.ids]
                        for card in cards:
                            card.queue = 1
                            card.type = kind
                            card.due = int(time.time())-120
                            card.left = 2002 if kind == 1 else 1001
                            card.ivl = 0 if kind == 1 else 7
                            card.factor = 2500
                        self.col.update_cards(cards)
                        session = sls.engine.session(self.col)
                        session.release_current()
                        before = self.rows()
                        card = self.fetch()
                        cid = card.id
                        self.answer(rating)
                        after = self.rows()
                        self.col.undo()
                        self.assertEqual(before, self.rows())
                        self.assertEqual(self.fetch().id, cid)
                        self.col.redo()
                        self.assertEqual(after, self.rows())
                        self.col.undo()

    def test_fast_native_card_fields_match_full_native_queue(self):
        for fsrs in (False, True):
            self.col.set_config('fsrs', fsrs)
            self.col.update_card(self.col.get_card(self.ids[0]))
            native = self.col._backend.get_queued_cards(fetch_limit=100, intraday_learning_only=False)
            for q in native.cards:
                self.assertEqual(materialize(self.col, q.card.id), q)

    def test_fast_fields_fsrs_mixed_filtered_and_preview(self):
        self.col.set_config('fsrs', True)
        self.filtered(mixed=True)
        native = self.col._backend.get_queued_cards(fetch_limit=100, intraday_learning_only=False)
        for q in native.cards:
            self.assertEqual(materialize(self.col, q.card.id), q)
        did = self.col.decks.current()['id']
        deck = self.col.sched.get_or_create_filtered_deck(did)
        deck.config.reschedule = False
        self.col.sched.add_or_update_filtered_deck(deck)
        native = self.col._backend.get_queued_cards(fetch_limit=100, intraday_learning_only=False)
        for q in native.cards:
            self.assertEqual(materialize(self.col, q.card.id), q)

    def test_fsrs_context_across_home_decks(self):
        parent = self.col.decks.id('Synthetic parent')
        one = self.col.decks.id('Synthetic parent::One')
        two = self.col.decks.id('Synthetic parent::Two')
        self.col.set_deck(self.ids[:3], one)
        self.col.set_deck(self.ids[3:], two)
        deck = self.col._backend.get_deck(two)
        deck.normal.desired_retention = .85
        self.col._backend.update_deck(deck)
        self.col.decks.select(parent)
        self.col.set_config('fsrs', True)
        self.col.update_card(self.col.get_card(self.ids[0]))
        native = self.col._backend.get_queued_cards(fetch_limit=100, intraday_learning_only=False)
        self.assertEqual(len(native.cards), len(self.ids))
        names = set()
        for q in native.cards:
            names.add(q.context.deck_name)
            self.assertEqual(materialize(self.col, q.card.id), q)
        self.assertEqual(names, {'Synthetic parent::One', 'Synthetic parent::Two'})

    def test_failure_retains_matching_original_card_and_states(self):
        original = self.col.sched.get_queued_cards().cards[0]
        sls.engine.session(self.col).history[:] = [original.card.note_id]
        before = self.rows()
        with patch('sls_test.engine.materialize', side_effect=RuntimeError('synthetic failure')):
            with self.assertLogs('sls_test.engine', level='ERROR'):
                self.r.card = None
                self.r._v3 = None
                self.r._get_next_v3_card()
        self.assertTrue(sls.engine.session(self.col).failed)
        self.assertEqual(self.r.card.id, original.card.id)
        self.assertEqual(self.r._v3.top_card().card.id, self.r.card.id)
        self.assertEqual(before, self.rows())
        # V3CardInfo normally populates custom_data on the current state.
        original.states.current.custom_data = original.card.custom_data
        self.assertEqual(self.r._v3.states, original.states)

    def test_alternative_after_two_hundred_cloze_siblings(self):
        for cid in self.ids:
            card = self.col.get_card(cid)
            card.due = int(time.time())+3600
            self.col.update_card(card)
        model = self.col.models.by_name('Cloze')
        many = self.col.new_note(model)
        many['Text'] = ' '.join('{{c%d::Synthetic %d}}' % (i,i) for i in range(1,201))
        self.col.add_note(many, 1)
        other = self.col.new_note(model)
        other['Text'] = '{{c1::Other synthetic note}}'
        self.col.add_note(other, 1)
        cards = many.cards()+other.cards()
        for i, card in enumerate(cards):
            card.type = card.queue = 1
            card.due = int(time.time())-5000+i
            card.reps = 1
            card.left = 2002
        self.col.update_cards(cards)
        sls.engine.session(self.col).history[:] = [many.id]
        self.assertEqual(self.fetch().nid, other.id)

    def test_learning_delays_are_not_changed_and_no_future_card_is_pulled(self):
        card = self.fetch()
        self.answer(CardAnswer.AGAIN)
        card = self.col.get_card(card.id)
        self.assertGreater(card.due, int(time.time()))
        self.assertNotEqual(self.fetch().id, card.id)
        cards = [self.col.get_card(cid) for cid in self.ids]
        for card in cards:
            card.type = card.queue = 1
            card.due = int(time.time())+600
        self.col.update_cards(cards)
        sls.engine.session(self.col).release_current()
        self.assertIsNone(self.fetch())

    def test_buried_suspended_and_deleted_current_cards_are_excluded(self):
        card = self.fetch()
        self.col.sched.bury_cards([card.id])
        self.assertNotEqual(self.fetch().id, card.id)
        card = self.r.card
        self.col.sched.suspend_cards([card.id])
        self.assertNotEqual(self.fetch().id, card.id)
        card = self.r.card
        self.col.remove_notes([card.nid])
        self.assertNotEqual(self.fetch().nid, card.nid)

    def test_turning_off_keeps_original_ordering(self):
        sls.engine.configure(Settings(enabled=False))
        native = self.col.sched.get_queued_cards().cards[0]
        self.assertEqual(self.fetch().id, native.card.id)

    def test_normal_review_cards_do_not_get_reordered(self):
        cards = [self.col.get_card(cid) for cid in self.ids]
        for card in cards:
            card.type = card.queue = 2
            card.due = self.col.sched.today
            card.ivl = 7
            card.factor = 2500
        self.col.update_cards(cards)
        native = self.col.sched.get_queued_cards().cards[0]
        self.assertEqual(self.fetch().id, native.card.id)
        before = self.rows()
        self.answer(CardAnswer.GOOD)
        self.col.undo()
        self.assertEqual(before, self.rows())

    def test_filtered_queue_answers_and_undoes(self):
        self.filtered()
        before = self.rows()
        card = self.fetch()
        self.answer(CardAnswer.GOOD)
        self.col.undo()
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.fetch().id, card.id)

    @unittest.skipUnless(tr, 'True Random compatibility scenario')
    def test_true_random_mixed_pool_never_repeats_when_alternative_ready(self):
        self.filtered(mixed=True, second=True)
        seen_types = set()
        last = None
        for _ in range(len(self.ids)):
            ready = sls.engine.rows(self.col, True, int(time.time()))
            card = self.fetch()
            if card is None:
                break
            if last is not None and any(row[1] != last for row in ready):
                self.assertNotEqual(card.nid, last)
            seen_types.add(card.type)
            before = self.rows()
            cid = card.id
            self.answer(CardAnswer.EASY)
            after = self.rows()
            self.col.undo()
            self.assertEqual(before, self.rows())
            self.assertEqual(self.fetch().id, cid)
            self.col.redo()
            self.assertEqual(after, self.rows())
            last = card.nid
        self.assertTrue({0,1,2}.issubset(seen_types))

    @unittest.skipUnless(tr, 'True Random compatibility scenario')
    def test_true_random_preview_and_waits(self):
        self.filtered(preview=True)
        card = self.fetch()
        before = self.rows()
        self.answer(CardAnswer.AGAIN)
        saved = self.col.get_card(card.id)
        self.assertGreater(saved.due, int(time.time()))
        self.assertNotEqual(self.fetch().id, card.id)
        self.col.undo()
        self.assertEqual(before, self.rows())
        self.assertEqual(self.fetch().id, card.id)

    @unittest.skipUnless(tr, 'True Random compatibility scenario')
    def test_true_random_fetch_limit_queries_remain_unchanged(self):
        self.filtered(mixed=True)
        out = self.col.sched.get_queued_cards(fetch_limit=100)
        self.assertEqual(len(out.cards), len(self.ids))
        self.assertFalse(self.col.sched.get_queued_cards(fetch_limit=0).cards)
        self.assertTrue(self.col.sched.get_queued_cards(intraday_learning_only=True).cards)
        self.assertEqual(self.col.sched.get_queued_cards().cards[0].card.id,
                         self.col.sched.get_queued_cards().cards[0].card.id)

    def test_settings_dialog_and_persistence_callback(self):
        from aqt.qt import QApplication
        app = QApplication.instance() or QApplication([])
        saved = []
        dialog = sls.SettingsDialog(None, Settings(), saved.append)
        self.assertTrue(dialog.enabled.isChecked())
        self.assertEqual(dialog.minutes.value(), 60)
        if args.screenshot:
            dialog.show()
            app.processEvents()
            dialog.grab().save(args.screenshot)
        dialog.minutes.setValue(90)
        dialog.accept()
        self.assertEqual(saved[0].extra_priority_after_minutes, 90)
        dialog.deleteLater()

    def test_tools_action_and_config_save(self):
        from aqt.qt import QApplication, QMainWindow, QMenu
        app = QApplication.instance() or QApplication([])
        window = QMainWindow()
        saved = []
        manager = types.SimpleNamespace(
            getConfig=lambda module: Settings().to_dict(),
            writeConfig=lambda module, value: saved.append((module, value)),
        )
        window.addonManager = manager
        window.col = self.col
        window.form = types.SimpleNamespace(menuTools=QMenu(window))
        aqt.mw = window
        sls.add_menu()
        sls.add_menu()
        self.assertEqual(len(window.form.menuTools.actions()), 1)
        def accept(dialog):
            dialog.minutes.setValue(75)
            dialog.accept()
            return 1
        with patch.object(sls.SettingsDialog, 'exec', new=accept):
            sls.open_settings()
        self.assertEqual(saved[0][1]['extra_priority_after_minutes'], 75)
        self.assertEqual(sls.engine.settings.extra_priority_after_minutes, 75)
        window.deleteLater()
        sls._action = None
        aqt.mw = self.mw

    def test_large_learning_queue_latency(self):
        self.make_large_queue(1000)
        times = []
        for _ in range(10):
            sls.engine.session(self.col).release_current()
            started = time.perf_counter()
            card = self.fetch()
            times.append((time.perf_counter()-started)*1000)
            self.assertIsNotNone(card)
        print('Reviewer 1000 learning cards', args.order,
              'median ms', round(sorted(times)[5],3), 'max ms', round(max(times),3))
        self.assertLess(max(times), 100)

    def make_large_queue(self, count):
        model = self.col.models.by_name('Basic (and reversed card)')
        requests = []
        for i in range((count-len(self.ids))//2):
            note = self.col.new_note(model)
            note['Front'] = f'Large synthetic {i}'
            note['Back'] = 'Answer'
            requests.append(AddNoteRequest(note=note, deck_id=1))
        self.col.add_notes(requests)
        ids = self.col.find_cards('')
        cards = [self.col.get_card(cid) for cid in ids]
        for i, card in enumerate(cards):
            card.queue = card.type = 1
            card.due = int(time.time())-3300+i//2
            card.reps = 1
            card.left = 2002
        self.col.update_cards(cards)

    def test_ten_thousand_cards_reviewer_latency(self):
        self.make_large_queue(10000)
        times = []
        for _ in range(5):
            sls.engine.session(self.col).release_current()
            started = time.perf_counter()
            card = self.fetch()
            times.append((time.perf_counter()-started)*1000)
            self.assertIsNotNone(card)
        print('Reviewer 10000 learning cards', args.order,
              'median ms', round(sorted(times)[2],3), 'max ms', round(max(times),3))
        self.assertLess(max(times), 100)

    @unittest.skipUnless(tr, 'True Random compatibility scenario')
    def test_true_random_active_thousand_card_latency(self):
        self.make_large_queue(1000)
        self.filtered()
        original = Reviewer._get_next_v3_card._spread_siblings_original
        baseline, added = [], []
        for _ in range(8):
            sls.engine.session(self.col).release_current()
            self.r.card = None
            self.r._v3 = None
            started = time.perf_counter()
            original(self.r)
            middle = time.perf_counter()
            sls.engine.choose(self.r)
            ended = time.perf_counter()
            baseline.append((middle-started)*1000)
            added.append((ended-middle)*1000)
        print('True Random ACTIVE 1000:', args.order,
              'existing median ms', round(sorted(baseline)[4],3),
              'added median ms', round(sorted(added)[4],3),
              'added max ms', round(max(added),3))
        self.assertLess(max(added), 50)


if __name__ == '__main__':
    print('Anki', buildinfo.version, buildinfo.buildhash, 'load order', args.order)
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
