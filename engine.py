"""Reviewer integration that leaves native scheduling and True Random intact."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time

from anki.cards import Card
from anki.scheduler.v3 import QueuedCards

from .planner import Candidate, Planner, Settings

log = logging.getLogger(__name__)
MASK = (1 << 64)-1


def materialize(col, cid):
    """Get native states for ONE chosen card, avoiding a full-state queue scan.

    This matches get_queued_cards' returned card/context, including transient
    FSRS memory fields. All scheduling calculations remain native Anki calls.
    The existing native queue must already be initialized by the reviewer.
    """
    q = QueuedCards.QueuedCard()
    q.card.CopyFrom(col._backend.get_card(cid))
    q.states.CopyFrom(col._backend.get_scheduling_states(cid))
    normal = q.states.current.normal
    if q.states.current.WhichOneof('kind') == 'filtered':
        if q.states.current.filtered.WhichOneof('kind') == 'rescheduling':
            normal = q.states.current.filtered.rescheduling.original_state
        else:
            normal = None
    if normal is not None:
        kind = normal.WhichOneof('kind')
        state = getattr(normal, kind) if kind else None
        if kind == 'relearning':
            state = state.learning
        if state is not None and hasattr(state, 'memory_state') and state.HasField('memory_state'):
            q.card.memory_state.CopyFrom(state.memory_state)
    if col.get_config('fsrs', False):
        computed = col._backend.compute_memory_state(cid)
        q.card.decay = computed.decay
        # Preview states omit normal scheduling fields, but the native queued
        # card still includes an estimated FSRS memory state for legacy cards.
        if not q.card.HasField('memory_state') and computed.HasField('state'):
            q.card.memory_state.CopyFrom(computed.state)
        if normal is None and q.card.ctype != 0 and not q.card.HasField('memory_state'):
            # Legacy preview cards without a review log need Anki's own SM-2
            # memory estimate, which the one-card stats API does not expose.
            # Use the complete native representation instead of reproducing
            # any FSRS calculation in this add-on. Ordinary learning cards do
            # not take this fallback path.
            count = col.db.scalar('select count(*) from cards where did in (select id from active_decks)')
            native = col._backend.get_queued_cards(fetch_limit=max(1, count), intraday_learning_only=False)
            for item in native.cards:
                if item.card.id == cid:
                    return item
            raise RuntimeError('Selected preview card is no longer in the native queue')
    q.queue = (QueuedCards.NEW if q.card.queue == 0 else
               QueuedCards.REVIEW if q.card.queue == 2 else QueuedCards.LEARNING)
    q.context.deck_name = col.decks.get(q.card.original_deck_id or q.card.deck_id)['name']
    q.context.seed = ((((cid << 8) & MASK) | (cid >> 56)) + q.card.reps) & MASK
    for name in ('decay', 'desired_retention'):
        if q.card.HasField(name):
            setattr(q.context, name, getattr(q.card, name))
    return q


@dataclass
class Session:
    history: list[int] = field(default_factory=list)
    current_id: int | None = None
    shown_id: int | None = None
    before_current: tuple[int, ...] = ()
    deck_id: int | None = None
    force_id: int | None = None
    undo_records: dict = field(default_factory=dict)
    failed: bool = False

    def release_current(self):
        self.current_id = None
        self.shown_id = None


class Engine:
    def __init__(self, settings=None, planner=None):
        self.settings = settings or Settings()
        self.planner = planner or Planner()

    def session(self, col):
        if not hasattr(col.sched, '_spread_learning_siblings'):
            col.sched._spread_learning_siblings = Session()
        return col.sched._spread_learning_siblings

    def configure(self, settings):
        self.settings = settings

    def rows(self, col, true_random, now):
        # Reading active_decks uses the native queue's deck scope and never
        # opens another collection. Learning cards have no daily gather limit.
        effective = 'case when odid != 0 and due < 0 and odue > 1000000000 then odue else due end'
        if true_random:
            return col.db.all(
                f'''select id, nid, {effective}, queue from cards
                    where did = ? and (
                        queue = 0 or
                        (queue in (1,4) and {effective} <= ?) or
                        (queue in (2,3) and due <= ?))''',
                col.decks.current()['id'], now, col.sched.today,
            )
        return col.db.all(
            f'''select id, nid, {effective}, queue from cards
                where queue = 1 and type in (1,3)
                and did in (select id from active_decks)
                and {effective} <= ?''', now,
        )

    def choose(self, reviewer):
        col = reviewer.mw.col
        session = self.session(col)
        if not self.settings.enabled or session.failed or not reviewer.card or not reviewer._v3:
            return
        deck = col.decks.current()
        did = deck['id']
        if did != session.deck_id:
            session.release_current()
            session.deck_id = did
        true_random = bool(deck.get('dyn') and str(did) in col.get_config('true_random_filtered_decks', [])
                           and hasattr(col.sched, '_true_random_session'))
        base = reviewer._v3.top_card()
        if not true_random and base.card.queue != 1 and not session.force_id:
            session.release_current()
            return
        now = int(time.time())
        rows = self.rows(col, true_random, now)
        by_id = {row[0]: row for row in rows}
        cid = base.card.id
        if session.force_id is not None:
            # Undo explicitly reopens the reverted card instead of treating it
            # as a fresh ordinary selection subject to the spacing rule.
            force = session.force_id
            session.force_id = None
            if force in by_id:
                cid = force
        elif session.current_id in by_id:
            cid = session.current_id
        elif rows:
            last = session.history[-1] if session.history else None
            if true_random and cid in by_id and by_id[cid][1] == last:
                alternatives = {r[0] for r in rows if r[1] != last}
                if alternatives:
                    # Rejection sampling of True Random's existing shuffled
                    # order keeps its random mixing of card types.
                    order = col.sched._true_random_session.order
                    cid = next((c for c in order if c in alternatives), min(alternatives))
            if cid in by_id and by_id[cid][3] == 1:
                learning = [Candidate(r[0], r[1], r[2]) for r in rows if r[3] == 1]
                planned = self.planner.choose(learning, session.history, now, self.settings)
                if planned is not None:
                    cid = planned
        if cid != base.card.id:
            from aqt.reviewer import V3CardInfo
            chosen = materialize(col, cid)
            out = QueuedCards()
            out.CopyFrom(reviewer._v3.queued_cards)
            del out.cards[:]
            out.cards.add().CopyFrom(chosen)
            v3 = V3CardInfo.from_queue(out)
            card = Card(col, backend_card=chosen.card)
            card.start_timer()
            reviewer._v3 = v3
            reviewer.card = card
        if session.current_id != cid:
            session.before_current = tuple(session.history)
        session.current_id = cid

    def shown(self, col, card):
        session = self.session(col)
        if not self.settings.enabled or session.failed or session.shown_id == card.id:
            return
        if session.current_id != card.id:
            session.before_current = tuple(session.history)
            session.current_id = card.id
        session.history.append(card.nid)
        session.history[:] = session.history[-64:]
        session.shown_id = card.id

    def answered(self, col, card):
        session = self.session(col)
        if not self.settings.enabled or session.failed:
            return
        counter = col._backend.get_undo_status().last_step
        session.undo_records[counter] = (card.id, session.before_current, tuple(session.history))
        while len(session.undo_records) > 100:
            session.undo_records.pop(next(iter(session.undo_records)))
        session.release_current()

    def undone(self, col, out, redo=False):
        session = self.session(col)
        session.release_current()
        record = session.undo_records.get(out.counter)
        if record:
            cid, before, after = record
            session.history[:] = after if redo else before
            session.force_id = None if redo else cid
        else:
            session.force_id = None

    def pause(self, col):
        session = self.session(col)
        already_failed = session.failed
        session.failed = True
        log.exception('Spread Learning Siblings paused; retaining the original reviewer selection')
        if not already_failed:
            from aqt import mw
            from aqt.qt import QWidget
            if isinstance(mw, QWidget):
                from aqt.utils import tooltip
                tooltip('Spread Learning Siblings paused. Open its Tools settings and press OK to retry.',
                        parent=mw, period=6000)
