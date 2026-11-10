"""Register the reviewer policy and a small native settings dialog."""

from anki.collection import Collection
from aqt import gui_hooks
from aqt.reviewer import Reviewer
from aqt.qt import (
    QAction, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QSpinBox, QVBoxLayout,
)

from .engine import Engine
from .planner import Settings

engine = Engine()
_installed = False
_action = None


def load_settings():
    from aqt import mw
    if mw:
        engine.configure(Settings.from_dict(mw.addonManager.getConfig(__name__)))
        if mw.col:
            engine.session(mw.col).failed = False


class SettingsDialog(QDialog):
    def __init__(self, parent, settings, save):
        super().__init__(parent)
        self.setWindowTitle('Spread Learning Siblings')
        self._save = save
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox('Spread learning siblings apart')
        self.enabled.setChecked(settings.enabled)
        layout.addWidget(self.enabled)
        explanation = QLabel(
            'Avoid consecutive siblings whenever another same-day learning note is due. '
            'Spread siblings across the available cards while gently favoring older cards. '
            'Learning delays stay the same.'
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        form = QFormLayout()
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 1440)
        self.minutes.setSuffix(' min overdue')
        self.minutes.setValue(settings.extra_priority_after_minutes)
        form.addRow('Extra age priority starts after:', self.minutes)
        self.strength = QComboBox()
        for label, value in [('Very gentle', .1), ('Gentle', .25), ('Stronger', .5)]:
            self.strength.addItem(label, value)
        index = min(range(self.strength.count()),
                    key=lambda i: abs(self.strength.itemData(i)-settings.overdue_weight))
        self.strength.setCurrentIndex(index)
        form.addRow('Overdue preference:', self.strength)
        layout.addLayout(form)
        detail = QLabel(
            'With the default 60 minutes, cards less than an hour overdue have equal age priority. '
            'This does not add a wait. Siblings are cards generated from the same note. '
            'In True Random decks, random card-type mixing continues and sibling repeats '
            'are also avoided across its ready cards.'
        )
        detail.setWordWrap(True)
        layout.addWidget(detail)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, self.sizeHint().height())

    def accept(self):
        settings = Settings(self.enabled.isChecked(), self.minutes.value(), self.strength.currentData())
        self._save(settings)
        super().accept()


def open_settings():
    from aqt import mw
    def save(settings):
        mw.addonManager.writeConfig(__name__, settings.to_dict())
        engine.configure(settings)
        if mw.col:
            engine.session(mw.col).failed = False
    dialog = SettingsDialog(mw, engine.settings, save)
    dialog.exec()


def add_menu():
    global _action
    from aqt import mw
    if mw and _action is None:
        _action = QAction('Spread Learning Siblings…', mw)
        _action.triggered.connect(open_settings)
        mw.form.menuTools.addAction(_action)


def on_show(card):
    engine.shown(card.col, card)


def on_answer(reviewer, card, ease):
    engine.answered(reviewer.mw.col, card)


def on_end():
    from aqt import mw
    if mw and mw.col:
        engine.session(mw.col).release_current()


def install():
    global _installed
    if _installed:
        return
    _installed = True
    original_get = Reviewer._get_next_v3_card
    original_undo = Collection.undo
    original_redo = Collection.redo

    def get_next(self):
        original_get(self)
        try:
            engine.choose(self)
        except Exception:
            engine.pause(self.mw.col)

    def undo(self):
        out = original_undo(self)
        try:
            engine.undone(self, out)
        except Exception:
            engine.pause(self)
        return out

    def redo(self):
        out = original_redo(self)
        try:
            engine.undone(self, out, redo=True)
        except Exception:
            engine.pause(self)
        return out

    get_next._spread_siblings_original = original_get
    Reviewer._get_next_v3_card = get_next
    Collection.undo = undo
    Collection.redo = redo
    gui_hooks.reviewer_did_show_question.append(on_show)
    gui_hooks.reviewer_did_answer_card.append(on_answer)
    gui_hooks.reviewer_will_end.append(on_end)
    gui_hooks.profile_did_open.append(load_settings)
    gui_hooks.main_window_did_init.append(add_menu)
