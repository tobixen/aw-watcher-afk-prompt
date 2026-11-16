"""An unanswered split dialog must not hold the watcher forever.

The ordinary prompt auto-snoozes after ``timeout_ms``; pressing Split cancelled
those timers and the split dialog armed none of its own, so a split left open
held the poll loop for as long as it sat there (observed: 15:56 to 17:29).

Auto-snoozing must not throw away what was typed: the lines are kept as a draft
for the period, and pressing Split in the next prompt for it brings them back.
"""

import time
import tkinter as tk
from datetime import UTC, datetime, timedelta

import pytest

import aw_watcher_afk_prompt.dialog as aw_dialog
import aw_watcher_afk_prompt.split_dialog as split_dialog
from aw_watcher_afk_prompt.split_dialog import (
    ActivityLine,
    SplitActivityDialog,
    SplitDraft,
    TimeCalculator,
    ask_split_activities,
    fit_draft,
    has_draft,
)

START = datetime(2026, 9, 5, 15, 56, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_drafts(monkeypatch) -> None:
    monkeypatch.setattr(split_dialog, "_drafts", {})


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def modal(monkeypatch) -> list:
    """Run the dialog's event loop without a grab or a visible window.

    The test's ``on_open`` hook (appended to the returned list) runs once the
    dialog is built — that is where the "user" types.  The loop gives up after
    a few seconds so a missing timeout fails instead of hanging.
    """
    hooks: list = []

    def run(self, *a, **kw) -> None:  # noqa: ARG001
        for hook in hooks:
            hook(self)
        deadline = time.monotonic() + 3
        while self.winfo_exists() and time.monotonic() < deadline:
            self.update()
            time.sleep(0.005)
        if self.winfo_exists():
            self.cancel()
            pytest.fail("split dialog was still open after 3 s")

    for name in ("wait_visibility", "grab_set", "deiconify", "wm_deiconify"):
        monkeypatch.setattr(SplitActivityDialog, name, lambda self, *a, **kw: None)  # noqa: ARG005
    monkeypatch.setattr(SplitActivityDialog, "wait_window", run)
    return hooks


def _type_description(text: str, index: int = 0):
    def hook(dialog: SplitActivityDialog) -> None:
        dialog.activity_widgets[index].desc_var.set(text)

    return hook


def test_unanswered_split_dialog_auto_snoozes(root, modal) -> None:
    result = ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)
    assert result is None


def test_auto_snooze_keeps_the_typed_lines_as_a_draft(root, modal) -> None:
    modal.append(_type_description("lunch"))
    assert ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50) is None
    assert has_draft(START)


def test_the_draft_prefills_the_next_split_dialog(root, modal) -> None:
    modal.append(_type_description("lunch"))
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)

    modal.clear()
    seen: list[list[str]] = []
    modal.append(lambda d: seen.append([a.description for a in d.activities]))
    modal.append(lambda d: d.ok())
    result = ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)

    assert seen == [["lunch", ""]]
    assert [a.description for a in result] == ["lunch", ""]
    assert not has_draft(START)


def test_a_start_time_still_being_typed_goes_into_the_draft(root, modal) -> None:
    def type_start(dialog: SplitActivityDialog) -> None:
        dialog.activity_widgets[1].start_var.set(
            (START + timedelta(minutes=40)).astimezone(split_dialog.LOCAL_TIMEZONE).strftime("%H:%M")
        )

    modal.append(type_start)
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)
    assert split_dialog._drafts[START].activities[1].start_time == START + timedelta(minutes=40)


def test_cancel_discards_the_draft(root, modal) -> None:
    modal.append(_type_description("lunch"))
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)

    modal.clear()
    modal.append(lambda d: d.cancel())
    assert ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50) is None
    assert not has_draft(START)


def test_the_draft_is_found_when_the_period_comes_back_completed(root, modal) -> None:
    """The live dialog's period, asked about again as a completed gap.

    Its start may have been advanced past the idle countdown, and its end is
    where the user came back: both differ from the ongoing period's.
    """
    modal.append(_type_description("lunch"))
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)

    later = START + timedelta(minutes=3)
    assert has_draft(later)
    modal.clear()
    seen: list = []
    modal.append(lambda d: seen.append(list(d.activities)))
    modal.append(lambda d: d.cancel())
    ask_split_activities("t", "p", later, 3000.0, [], parent=root, timeout_ms=50)

    (activities,) = seen
    assert [a.description for a in activities] == ["lunch", ""]
    assert activities[0].start_time == later
    assert activities[-1].end_time == later + timedelta(seconds=3000)


@pytest.mark.parametrize("offset", [timedelta(minutes=11), timedelta(minutes=-1)])
def test_a_draft_does_not_reach_another_period(offset) -> None:
    split_dialog._drafts[START] = SplitDraft(TimeCalculator.split_equal(START, 3600.0, 2, ["a", "b"]))
    assert not has_draft(START + offset)


def test_the_draft_keeps_the_locks_and_the_equal_split(root, modal) -> None:
    def lock_first(dialog: SplitActivityDialog) -> None:
        dialog.activity_widgets[0].locked_var.set(True)

    modal.append(lock_first)
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)

    modal.clear()
    seen: list = []
    modal.append(lambda d: seen.append(([w.is_locked() for w in d.activity_widgets], d.equal_distribution_mode)))
    modal.append(lambda d: d.cancel())
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=50)
    assert seen == [([True, False], True)]


def test_clicking_restarts_the_countdown(root, modal) -> None:
    timers: list = []

    def click(dialog: SplitActivityDialog) -> None:
        timers.append(dialog._timeout_timer)
        dialog.activity_widgets[0].desc_entry.event_generate("<ButtonPress>", button=1)
        timers.append(dialog._timeout_timer)
        dialog.cancel()

    modal.append(click)
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=60_000)
    first, second = timers
    assert first is not None and second is not None and first != second


def test_closing_the_dialog_disarms_the_countdown(root, modal) -> None:
    armed: list = []

    def close(dialog: SplitActivityDialog) -> None:
        armed.append(dialog._timeout_timer)
        dialog.ok()

    modal.append(close)
    ask_split_activities("t", "p", START, 3600.0, [], parent=root, timeout_ms=60_000)
    (timer,) = armed
    assert timer is not None
    assert timer not in root.tk.splitlist(root.tk.call("after", "info"))


class TestFitDraft:
    def test_same_period_is_kept_as_is(self) -> None:
        draft = TimeCalculator.split_equal(START, 3600.0, 2, ["a", "b"])
        assert fit_draft(draft, START, 3600.0) == draft

    def test_a_period_ending_earlier_shortens_the_last_line(self) -> None:
        """The snapshot ran to the moment Split was pressed; the period ended before."""
        draft = TimeCalculator.split_equal(START, 3600.0, 2, ["a", "b"])
        fitted = fit_draft(draft, START, 3000.0)
        assert fitted[0] == draft[0]
        assert fitted[1].total_duration_seconds == 1200
        assert fitted[-1].end_time == START + timedelta(seconds=3000)

    def test_a_period_starting_later_shortens_the_first_line(self) -> None:
        draft = TimeCalculator.split_equal(START, 3600.0, 2, ["a", "b"])
        later = START + timedelta(minutes=2)
        fitted = fit_draft(draft, later, 3480.0)
        assert fitted[0].start_time == later
        assert fitted[0].total_duration_seconds == 1680
        assert fitted[1] == draft[1]

    def test_a_period_too_short_for_the_lines_is_shared_equally(self) -> None:
        draft = [
            ActivityLine("a", START, 50),
            ActivityLine("b", START + timedelta(minutes=50), 10),
        ]
        fitted = fit_draft(draft, START, 1800.0)
        assert [a.description for a in fitted] == ["a", "b"]
        assert sum(a.total_duration_seconds for a in fitted) == 1800
        assert fitted[-1].end_time == START + timedelta(seconds=1800)


class _FakeMainDialog:
    """Stands in for the ordinary prompt; the user presses Split."""

    instances: list = []

    def __init__(self, title, prompt, *a, **kw) -> None:  # noqa: ARG002
        self.prompt = prompt
        self.kwargs = kw
        self.split_mode = True
        self.afk_duration_seconds = 600.0
        self.result = None
        _FakeMainDialog.instances.append(self)


@pytest.fixture
def main_dialog(monkeypatch) -> list:
    _FakeMainDialog.instances = []
    monkeypatch.setattr(aw_dialog, "AWAfkPromptDialog", _FakeMainDialog)
    return _FakeMainDialog.instances


class TestAskString:
    def test_the_timeout_reaches_the_split_dialog(self, monkeypatch, main_dialog) -> None:  # noqa: ARG002
        captured: dict = {}

        def fake_ask_split(*a, **kw) -> None:  # noqa: ARG001
            captured.update(kw)

        monkeypatch.setattr(split_dialog, "ask_split_activities", fake_ask_split)
        aw_dialog.ask_string("t", "p", [], afk_start=START, timeout_ms=1234)
        assert captured["timeout_ms"] == 1234

    def test_a_draft_is_announced_in_the_ordinary_prompt(self, monkeypatch, main_dialog) -> None:
        """Resuming goes through the ordinary prompt, which knows about the queue,
        refreshes, and doesn't count down while the user is away."""
        split_dialog._drafts[START] = SplitDraft(TimeCalculator.split_equal(START, 600.0, 2, ["a", "b"]))
        monkeypatch.setattr(split_dialog, "ask_split_activities", lambda *a, **kw: None)  # noqa: ARG005

        def refresh() -> dict:
            return {"prompt": "fresh", "queue_info": None}

        aw_dialog.ask_string("t", "p", [], afk_start=START, afk_duration_seconds=600.0, refresh=refresh)

        (shown,) = main_dialog
        assert shown.prompt.startswith("p")
        assert "Split" in shown.prompt
        refreshed = shown.kwargs["refresh"]()
        assert refreshed["prompt"].startswith("fresh")
        assert "Split" in refreshed["prompt"]

    def test_no_draft_no_announcement(self, monkeypatch, main_dialog) -> None:
        monkeypatch.setattr(split_dialog, "ask_split_activities", lambda *a, **kw: None)  # noqa: ARG005
        aw_dialog.ask_string("t", "p", [], afk_start=START, afk_duration_seconds=600.0)
        (shown,) = main_dialog
        assert shown.prompt == "p"
