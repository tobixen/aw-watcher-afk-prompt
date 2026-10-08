"""UI-level tests for SplitActivityDialog lock handling.

The lock checkboxes live in the row widgets, which are destroyed and recreated
by redraw_activities() whenever a line is added or removed. These tests verify
the lock state survives that (regression: pressing "+" unlocked all lines).

The dialog is built via __new__ + body() so the modal wait_window() machinery
of simpledialog.Dialog is bypassed.
"""

import tkinter as tk
from datetime import UTC, datetime, timedelta

import pytest

from aw_watcher_afk_prompt.split_dialog import SplitActivityDialog, TimeCalculator
from aw_watcher_afk_prompt.utils import LOCAL_TIMEZONE


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    root.withdraw()
    yield root
    root.destroy()


def _make_dialog(
    root, num_activities: int = 2, duration_minutes: int = 60, start: datetime | None = None
) -> SplitActivityDialog:
    """Construct a SplitActivityDialog without entering the modal event loop."""
    start = start or datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
    dialog = SplitActivityDialog.__new__(SplitActivityDialog)
    dialog.prompt = "test"
    dialog.afk_start = start
    dialog.afk_duration_seconds = duration_minutes * 60.0
    dialog.afk_end = start + timedelta(minutes=duration_minutes)
    dialog.history = []
    dialog.activities = TimeCalculator.split_equal(start, dialog.afk_duration_seconds, num_activities)
    dialog.equal_distribution_mode = True
    dialog.activity_widgets = []
    dialog.result = None
    dialog.return_to_single_mode = False
    dialog.single_mode_description = ""
    dialog.body(root)
    return dialog


class TestLockPreservation:
    def test_plus_button_preserves_locks(self, root) -> None:
        """Adding a line must not reset the lock checkboxes of existing lines."""
        dialog = _make_dialog(root, num_activities=2)
        dialog.activity_widgets[0].locked_var.set(True)

        dialog.add_activity_line()

        assert len(dialog.activity_widgets) == 3
        assert dialog.activity_widgets[0].is_locked() is True
        assert dialog.activity_widgets[1].is_locked() is False
        assert dialog.activity_widgets[2].is_locked() is False

    def test_plus_button_keeps_locked_duration(self, root) -> None:
        """A locked line's duration must not change when a new line is added,
        even in equal-distribution mode (lock beats redistribution)."""
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60)
        dialog.activity_widgets[0].locked_var.set(True)  # 30-minute line

        dialog.add_activity_line()

        assert dialog.activities[0].duration_minutes == 30

    def test_plus_button_borrows_from_unlocked_line(self, root) -> None:
        """With the last line locked, the new line's minute comes from the last
        unlocked line."""
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60)
        dialog.equal_distribution_mode = False
        dialog.activity_widgets[1].locked_var.set(True)

        dialog.add_activity_line()

        assert [a.duration_minutes for a in dialog.activities] == [29, 30, 1]

    def test_remove_preserves_locks_with_shifted_indices(self, root) -> None:
        """Removing a line must keep locks attached to the right lines."""
        dialog = _make_dialog(root, num_activities=3)
        dialog.activity_widgets[2].locked_var.set(True)

        dialog.remove_activity_line(0)

        assert len(dialog.activity_widgets) == 2
        assert dialog.activity_widgets[0].is_locked() is False
        assert dialog.activity_widgets[1].is_locked() is True


class TestStartTimeEditing:
    """Typing a start time (regression: every keystroke was applied, and the
    recalculation rewrote the field being typed in)."""

    def _type(self, widget, text: str) -> None:
        widget.start_var.set("")
        for i in range(1, len(text) + 1):
            widget.start_var.set(text[:i])

    def test_typing_is_not_applied_before_commit(self, root) -> None:
        start = datetime(2026, 7, 6, 12, 0, tzinfo=LOCAL_TIMEZONE)
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60, start=start)
        before = list(dialog.activities)

        self._type(dialog.activity_widgets[1], "12:40")

        assert dialog.activities == before
        assert dialog.activity_widgets[1].start_var.get() == "12:40"

    def test_commit_applies_typed_time(self, root) -> None:
        start = datetime(2026, 7, 6, 12, 0, tzinfo=LOCAL_TIMEZONE)
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60, start=start)

        self._type(dialog.activity_widgets[1], "12:40")
        dialog.activity_widgets[1].commit_start()

        assert dialog.activities[1].start_time == start + timedelta(minutes=40)

    def test_invalid_time_is_reverted_on_commit(self, root) -> None:
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60)
        shown = dialog.activity_widgets[1].start_var.get()

        self._type(dialog.activity_widgets[1], "0:300")
        dialog.activity_widgets[1].commit_start()

        assert dialog.activity_widgets[1].start_var.get() == shown

    def test_time_after_midnight_lands_on_the_next_day(self, root) -> None:
        """A night split: 00:30 means the morning after a 22:16 start."""
        start = datetime(2026, 10, 5, 22, 16, tzinfo=LOCAL_TIMEZONE)
        dialog = _make_dialog(root, num_activities=2, duration_minutes=600, start=start)

        self._type(dialog.activity_widgets[1], "00:30")
        dialog.activity_widgets[1].commit_start()

        assert dialog.activities[1].start_time == datetime(2026, 10, 6, 0, 30, tzinfo=LOCAL_TIMEZONE)

    def test_enter_commits_without_reaching_ok(self, root) -> None:
        """<Return> in a start field must not fall through to the dialog's OK."""
        start = datetime(2026, 7, 6, 12, 0, tzinfo=LOCAL_TIMEZONE)
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60, start=start)
        ok_pressed = []
        root.bind("<Return>", lambda e: ok_pressed.append(True))
        widget = dialog.activity_widgets[1]

        self._type(widget, "12:40")
        root.deiconify()
        widget.start_entry.focus_force()
        root.update()
        widget.start_entry.event_generate("<Return>")

        assert dialog.activities[1].start_time == start + timedelta(minutes=40)
        assert ok_pressed == []

    def test_ok_with_invalid_time_names_it_and_keeps_it(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60)
        errors = []
        monkeypatch.setattr(tk.messagebox, "showerror", lambda title, msg: errors.append(msg))
        widget = dialog.activity_widgets[1]

        self._type(widget, "0:300")

        assert dialog.validate() is False
        assert "0:300" in errors[0]
        assert widget.start_var.get() == "0:300"
        assert dialog.validate() is False

    def test_move_over_twelve_hours_in_a_long_period(self, root) -> None:
        """08:30 -> 21:00 within a 08:00-23:00 period is the same day, not the
        day before (which is closer to the old start)."""
        start = datetime(2026, 10, 5, 8, 0, tzinfo=LOCAL_TIMEZONE)
        dialog = _make_dialog(root, num_activities=2, duration_minutes=900, start=start)
        widget = dialog.activity_widgets[1]
        self._type(widget, "08:30")
        widget.commit_start()

        self._type(widget, "21:00")
        widget.commit_start()

        assert dialog.activities[1].start_time == datetime(2026, 10, 5, 21, 0, tzinfo=LOCAL_TIMEZONE)

    def test_adding_a_line_keeps_a_time_still_being_typed(self, root) -> None:
        start = datetime(2026, 7, 6, 12, 0, tzinfo=LOCAL_TIMEZONE)
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60, start=start)
        self._type(dialog.activity_widgets[1], "12:40")

        dialog.add_activity_line()

        assert dialog.activities[1].start_time == start + timedelta(minutes=40)
