"""UI-level tests for AWAfkPromptDialog's live refresh.

A dialog that sits unanswered goes stale in two ways: the "ended N minutes ago"
age keeps ageing and more AFK periods may pile up behind it.  These tests cover
the periodic refresh that keeps both honest.

The modal machinery of simpledialog.Dialog (wait_visibility / grab_set /
wait_window) is stubbed out so a dialog can be constructed and inspected
without blocking the test in an event loop.
"""

import tkinter as tk

import pytest

try:
    import aw_watcher_afk_prompt.dialog as aw_dialog
except tk.TclError:  # pragma: no cover - depends on the test environment
    pytest.skip("No display available", allow_module_level=True)

from datetime import UTC, datetime, timedelta


@pytest.fixture
def nonmodal(monkeypatch):
    """Make AWAfkPromptDialog constructible without entering the modal loop.

    ``deiconify`` is stubbed too, so a test run never flashes dialogs onto the
    screen of whoever is running the tests.
    """
    for name in ("wait_visibility", "grab_set", "wait_window", "deiconify"):
        monkeypatch.setattr(aw_dialog.AWAfkPromptDialog, name, lambda self, *a, **kw: None)  # noqa: ARG005
    created: list[aw_dialog.AWAfkPromptDialog] = []
    yield created
    # Destroy every window created under the shared root, not just the ones a
    # test got a handle to: a constructor that raised leaves one behind as well.
    for window in [*created, *aw_dialog.root.winfo_children()]:
        try:
            window.destroy()
        except tk.TclError:
            pass


def _make_dialog(created, **kwargs) -> aw_dialog.AWAfkPromptDialog:
    kwargs.setdefault("title", "Test")
    kwargs.setdefault("prompt", "What were you doing?")
    kwargs.setdefault("history", [])
    dialog = aw_dialog.AWAfkPromptDialog(**kwargs)
    created.append(dialog)
    return dialog


class TestQueueText:
    """The '(N of M)' line is rendered from a queue-info dict in one place."""

    def test_no_queue_info_renders_empty(self) -> None:
        assert aw_dialog._queue_text(None) == ""

    def test_with_next_interval(self) -> None:
        text = aw_dialog._queue_text({"position": 1, "total": 3, "next_str": "07:40–07:50 (10 minutes)"})
        assert text == "(1 of 3) — next: 07:40–07:50 (10 minutes)"

    def test_without_next_interval(self) -> None:
        assert aw_dialog._queue_text({"position": 3, "total": 3, "next_str": None}) == "(3 of 3) — last interval"


class TestLiveRefresh:
    """An open dialog must re-ask the caller for fresh prompt text and queue count."""

    def test_refresh_updates_prompt_and_adds_queue_line(self, nonmodal) -> None:
        """A queue that grows while the dialog waits must show up in the dialog."""
        update = {"prompt": "updated prompt", "queue_info": {"position": 1, "total": 3, "next_str": "later"}}
        dialog = _make_dialog(nonmodal, refresh=lambda: update)

        assert dialog._queue_var.get() == ""  # started with no queue

        dialog._refresh_now()

        assert dialog._prompt_label.cget("text") == "updated prompt"
        assert dialog._queue_var.get() == "(1 of 3) — next: later"
        assert dialog._queue_label.winfo_manager() == "grid"

    def test_refresh_can_clear_the_queue_line(self, nonmodal) -> None:
        """When the other periods got answered elsewhere the queue line goes away."""
        dialog = _make_dialog(
            nonmodal,
            queue_info={"position": 1, "total": 2, "next_str": "later"},
            refresh=lambda: {"queue_info": None},
        )
        assert dialog._queue_var.get() != ""

        dialog._refresh_now()

        assert dialog._queue_var.get() == ""
        assert dialog._queue_label.winfo_manager() == ""

    def test_missing_keys_leave_the_display_untouched(self, nonmodal) -> None:
        """An empty update means 'nothing new' — not 'clear everything'."""
        queue_info = {"position": 1, "total": 2, "next_str": "later"}
        dialog = _make_dialog(nonmodal, prompt="original", queue_info=queue_info, refresh=dict)

        dialog._refresh_now()

        assert dialog._prompt_label.cget("text") == "original"
        assert dialog._queue_var.get() == "(1 of 2) — next: later"

    def test_refresh_failure_is_survivable(self, nonmodal) -> None:
        """A server hiccup in the refresh callback must not kill the dialog."""

        def boom() -> dict:
            raise RuntimeError("server down")

        dialog = _make_dialog(nonmodal, prompt="original", refresh=boom)

        dialog._refresh_now()  # must not raise

        assert dialog._prompt_label.cget("text") == "original"

    def test_refresh_timer_armed_only_when_a_callback_is_given(self, nonmodal) -> None:
        assert _make_dialog(nonmodal)._refresh_timer is None
        assert _make_dialog(nonmodal, refresh=dict)._refresh_timer is not None

    def test_refresh_keeps_still_afk_marker_stripped_after_return(self, nonmodal) -> None:
        """Once the user is back, a refresh must not re-add '(still AFK)'."""
        start = datetime.now(UTC) - timedelta(minutes=10)
        dialog = _make_dialog(
            nonmodal,
            prompt="What were you doing since 07:40? (still AFK)",
            afk_start=start,
            is_ongoing=True,
            refresh=lambda: {"prompt": "What were you doing since 07:40? (still AFK)"},
        )
        dialog._mark_returned()

        dialog._refresh_now()

        assert dialog._prompt_label.cget("text") == "What were you doing since 07:40?"
