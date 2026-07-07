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


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    root.withdraw()
    yield root
    root.destroy()


def _make_dialog(root, num_activities: int = 2, duration_minutes: int = 60) -> SplitActivityDialog:
    """Construct a SplitActivityDialog without entering the modal event loop."""
    start = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
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
