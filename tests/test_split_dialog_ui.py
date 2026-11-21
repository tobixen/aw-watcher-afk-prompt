"""UI-level tests for SplitActivityDialog lock handling.

The lock checkboxes live in the row widgets, which are destroyed and recreated
by redraw_activities() whenever a line is added or removed. These tests verify
the lock state survives that (regression: pressing "+" unlocked all lines).

The dialog is built via __new__ + body() so the modal wait_window() machinery
of simpledialog.Dialog is bypassed.
"""

import logging
import tkinter as tk
from datetime import UTC, datetime, timedelta
from tkinter import ttk

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
    dialog.timeout_ms = None
    dialog.timed_out = False
    dialog._timeout_timer = None
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


class TestDistributeEqually:
    def test_button_evens_out_the_unlocked_lines(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        _type_minutes(dialog.activity_widgets[0], "5")
        dialog.activity_widgets[0].commit_minutes()
        dialog.activity_widgets[0].locked_var.set(True)
        _type_minutes(dialog.activity_widgets[1], "40")
        dialog.activity_widgets[1].commit_minutes()

        dialog.distribute_btn.invoke()

        assert [a.duration_minutes for a in dialog.activities] == [5, 27, 27]
        assert [w.get_duration_minutes() for w in dialog.activity_widgets] == [5, 27, 27]
        assert dialog.activity_widgets[0].is_locked() is True

    def test_button_survives_a_redraw(self, root) -> None:
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60)

        dialog.add_activity_line()
        _type_minutes(dialog.activity_widgets[0], "10")
        dialog.activity_widgets[0].commit_minutes()
        dialog.distribute_btn.invoke()

        assert [a.duration_minutes for a in dialog.activities] == [20, 20, 20]

    def test_all_locked_says_so_and_changes_nothing(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60)
        errors = []
        monkeypatch.setattr(tk.messagebox, "showerror", lambda title, msg: errors.append(msg))
        before = list(dialog.activities)
        for widget in dialog.activity_widgets:
            widget.locked_var.set(True)

        dialog.distribute_btn.invoke()

        assert dialog.activities == before
        assert "locked" in errors[0]

    def test_applies_a_start_time_still_being_typed(self, root) -> None:
        """The typed 12:10 gives the locked first line 10 minutes, and only
        then is the rest shared out."""
        start = datetime(2026, 7, 6, 12, 0, tzinfo=LOCAL_TIMEZONE)
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60, start=start)
        dialog.activity_widgets[0].locked_var.set(True)
        dialog.activity_widgets[1].start_var.set("12:10")

        dialog.distribute_btn.invoke()

        assert [a.duration_minutes for a in dialog.activities] == [10, 25, 25]


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


def _type_minutes(widget, text: str) -> None:
    """Type into a minutes field one keystroke at a time, from empty."""
    widget.duration_spinbox.delete(0, tk.END)
    for char in text:
        widget.duration_spinbox.insert(tk.END, char)


class TestMinutesEditing:
    """Typing a duration in minutes (regression: every keystroke was applied,
    so typing 45 first gave the line 4 minutes, and clearing the field logged
    a warning)."""

    def test_typing_is_not_applied_before_commit(self, root, caplog) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        before = list(dialog.activities)
        widget = dialog.activity_widgets[0]

        with caplog.at_level(logging.WARNING):
            _type_minutes(widget, "45")

        assert dialog.activities == before
        assert widget.duration_spinbox.get() == "45"
        assert caplog.records == []

    def test_commit_applies_typed_minutes(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]

        _type_minutes(widget, "40")
        assert widget.commit_minutes() is True

        assert [a.duration_minutes for a in dialog.activities] == [40, 10, 10]
        assert widget.duration_info_var.get() == ""

    @pytest.mark.parametrize("typed", ["", "abc", "0", "-5", "4.5", "\u00b2"])
    def test_invalid_minutes_are_reverted_on_commit(self, root, typed) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        before = list(dialog.activities)
        widget = dialog.activity_widgets[0]

        _type_minutes(widget, typed)
        assert widget.commit_minutes() is False

        assert dialog.activities == before
        assert widget.duration_spinbox.get() == "20"

    def test_enter_commits(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]

        _type_minutes(widget, "30")
        root.deiconify()
        widget.duration_spinbox.focus_force()
        root.update()
        widget.duration_spinbox.event_generate("<Return>")

        assert dialog.activities[0].duration_minutes == 30

    def test_enter_does_not_reach_ok(self, root) -> None:
        """Enter only commits; reaching OK as well would close the dialog
        over a value it just rejected."""
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        ok_pressed = []
        root.bind("<Return>", lambda e: ok_pressed.append(True))
        widget = dialog.activity_widgets[0]

        _type_minutes(widget, "abc")
        root.deiconify()
        widget.duration_spinbox.focus_force()
        root.update()
        widget.duration_spinbox.event_generate("<Return>")

        assert ok_pressed == []

    def test_stepping_another_line_keeps_minutes_being_typed(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]
        _type_minutes(widget, "30")

        dialog.activity_widgets[1].duration_spinbox.event_generate("<<Increment>>")

        assert widget.duration_spinbox.get() == "30"
        widget.commit_minutes()
        assert dialog.activities[0].duration_minutes == 30
        assert sum(a.duration_minutes for a in dialog.activities) == 60

    def test_ok_applies_minutes_typed_on_several_lines(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        _type_minutes(dialog.activity_widgets[0], "30")
        _type_minutes(dialog.activity_widgets[1], "25")

        assert dialog.validate() is True

        # Each is applied in turn, like typing one and then the other; the
        # second spreads its change over the unlocked lines.
        assert dialog.activities[1].duration_minutes == 25
        assert dialog.activities[0].duration_minutes != 20
        assert sum(a.duration_minutes for a in dialog.activities) == 60

    def test_arrow_step_is_applied_at_once(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]

        widget.duration_spinbox.event_generate("<<Increment>>")

        assert dialog.activities[0].duration_minutes == 21
        assert sum(a.duration_minutes for a in dialog.activities) == 60

    def test_untouched_field_after_another_lines_edit_changes_nothing(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        _type_minutes(dialog.activity_widgets[1], "30")
        dialog.activity_widgets[1].commit_minutes()
        before = list(dialog.activities)

        assert dialog.activity_widgets[0].commit_minutes() is True

        assert dialog.activities == before

    def test_ok_applies_minutes_still_being_typed(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        _type_minutes(dialog.activity_widgets[0], "30")

        assert dialog.validate() is True

        assert [a.duration_minutes for a in dialog.activities] == [30, 15, 15]

    def test_ok_with_invalid_minutes_names_them_and_keeps_them(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        errors: list[str] = []
        monkeypatch.setattr(tk.messagebox, "showerror", lambda title, msg: errors.append(msg))
        widget = dialog.activity_widgets[0]

        _type_minutes(widget, "abc")

        assert dialog.validate() is False
        assert "abc" in errors[0]
        assert widget.duration_spinbox.get() == "abc"

    def test_ok_applies_hours_still_being_typed(self, root) -> None:
        """A click on OK causes no FocusOut in the hours field either."""
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]
        widget._toggle_unit_mode()
        widget.hours_var.set("0.5")

        assert dialog.validate() is True

        assert [a.duration_minutes for a in dialog.activities] == [30, 15, 15]

    def test_adding_a_line_keeps_minutes_still_being_typed(self, root) -> None:
        dialog = _make_dialog(root, num_activities=2, duration_minutes=60)
        dialog.activity_widgets[0].locked_var.set(True)
        _type_minutes(dialog.activity_widgets[0], "40")

        dialog.add_activity_line()

        assert dialog.activities[0].duration_minutes == 40

    def test_switching_to_hours_applies_typed_minutes(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]
        _type_minutes(widget, "30")

        widget._toggle_unit_mode()

        assert dialog.activities[0].duration_minutes == 30
        assert widget.hours_var.get() == "0.5"


class TestClickOnAButton:
    """A click on a ttk button takes the focus on press, before its command
    runs, so the field being typed in sees a FocusOut first.  That must not
    quietly put the old value back and let OK pass."""

    def _click_away(self, root, dialog, entry) -> None:
        button = ttk.Button(dialog.master_frame, text="OK")
        button.grid(row=99, column=0)
        root.deiconify()
        entry.focus_force()
        root.update()
        button.event_generate("<ButtonPress-1>", x=2, y=2)
        root.update()

    def test_invalid_minutes_are_named_at_ok(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        errors: list[str] = []
        monkeypatch.setattr(tk.messagebox, "showerror", lambda title, msg: errors.append(msg))
        widget = dialog.activity_widgets[0]
        _type_minutes(widget, "abc")

        self._click_away(root, dialog, widget.duration_spinbox)

        assert dialog.validate() is False
        assert "abc" in errors[0]
        assert widget.duration_spinbox.get() == "abc"

    def test_invalid_hours_are_named_at_ok(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        errors: list[str] = []
        monkeypatch.setattr(tk.messagebox, "showerror", lambda title, msg: errors.append(msg))
        widget = dialog.activity_widgets[0]
        widget._toggle_unit_mode()
        widget.hours_var.set("abc")

        self._click_away(root, dialog, widget.hours_entry)

        assert dialog.validate() is False
        assert "abc" in errors[0]

    def test_invalid_start_time_is_named_at_ok(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        errors: list[str] = []
        monkeypatch.setattr(tk.messagebox, "showerror", lambda title, msg: errors.append(msg))
        widget = dialog.activity_widgets[1]
        widget.start_var.set("99:99")

        self._click_away(root, dialog, widget.start_entry)

        assert dialog.validate() is False
        assert "99:99" in errors[0]

    def test_valid_minutes_are_applied(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]
        _type_minutes(widget, "30")

        self._click_away(root, dialog, widget.duration_spinbox)

        assert dialog.activities[0].duration_minutes == 30


class TestDistributeEquallyWithPendingInput:
    def _errors(self, monkeypatch) -> list[str]:
        errors: list[str] = []
        monkeypatch.setattr(tk.messagebox, "showerror", lambda title, msg: errors.append(msg))
        return errors

    def test_applies_hours_still_being_typed_on_a_locked_line(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]
        widget._toggle_unit_mode()
        widget.hours_var.set("0.5")
        widget.locked_var.set(True)

        dialog.distribute_btn.invoke()

        assert [a.duration_minutes for a in dialog.activities] == [30, 15, 15]

    def test_locked_line_in_hours_mode_keeps_its_exact_duration(self, root) -> None:
        """20 minutes shows as 0.3 h, which must not end up as 18 minutes."""
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]
        widget._toggle_unit_mode()
        widget.locked_var.set(True)
        _type_minutes(dialog.activity_widgets[1], "30")
        dialog.activity_widgets[1].commit_minutes()

        dialog.distribute_btn.invoke()

        assert [a.duration_minutes for a in dialog.activities] == [20, 20, 20]

    def test_invalid_hours_are_named_and_nothing_is_distributed(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        errors = self._errors(monkeypatch)
        _type_minutes(dialog.activity_widgets[1], "30")
        dialog.activity_widgets[1].commit_minutes()
        before = list(dialog.activities)
        widget = dialog.activity_widgets[0]
        widget._toggle_unit_mode()
        widget.hours_var.set("abc")

        dialog.distribute_btn.invoke()

        assert dialog.activities == before
        assert "abc" in errors[0]

    def test_invalid_start_time_is_named_and_kept(self, root, monkeypatch) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        errors = self._errors(monkeypatch)
        _type_minutes(dialog.activity_widgets[0], "30")
        dialog.activity_widgets[0].commit_minutes()
        before = list(dialog.activities)
        dialog.activity_widgets[1].start_var.set("99:99")

        dialog.distribute_btn.invoke()

        assert dialog.activities == before
        assert "99:99" in errors[0]
        assert dialog.activity_widgets[1].start_var.get() == "99:99"


class TestHoursMode:
    def test_leaving_an_untouched_hours_field_changes_nothing(self, root) -> None:
        """20 minutes shows as 0.3 h; that rounding must not be written back."""
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]

        widget._toggle_unit_mode()
        widget.commit_hours()
        widget._toggle_unit_mode()

        assert [a.duration_minutes for a in dialog.activities] == [20, 20, 20]

    def test_typed_hours_are_applied(self, root) -> None:
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]

        widget._toggle_unit_mode()
        widget.hours_var.set("0.5")
        widget.commit_hours()

        assert dialog.activities[0].duration_minutes == 30

    def test_refreshed_field_counts_as_untouched(self, root) -> None:
        """A value another line's edit put in the field isn't typed input either."""
        dialog = _make_dialog(root, num_activities=3, duration_minutes=60)
        widget = dialog.activity_widgets[0]
        widget._toggle_unit_mode()

        _type_minutes(dialog.activity_widgets[1], "25")
        dialog.activity_widgets[1].commit_minutes()
        before = [a.duration_minutes for a in dialog.activities]
        widget.commit_hours()

        assert [a.duration_minutes for a in dialog.activities] == before
