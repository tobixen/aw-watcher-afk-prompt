"""UI-level tests for AWAfkPromptDialog: live refresh and the unanswered-timeout.

A dialog that sits unanswered goes stale in two ways: the "ended N minutes ago"
age keeps ageing and more AFK periods may pile up behind it.  These tests cover
the periodic refresh that keeps both honest, plus the auto-snooze that hides a
forgotten dialog so it can come back re-raised and up to date.

The modal machinery of simpledialog.Dialog (wait_visibility / grab_set /
wait_window) is stubbed out so a dialog can be constructed and inspected
without blocking the test in an event loop.
"""

import time
import tkinter as tk

import pytest

import aw_watcher_afk_prompt.dialog as aw_dialog

try:
    aw_dialog.get_root()  # importing is display-free; these tests are not
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


class TestDegradedTkWarning:
    """A Tk without Xft cannot accept non-ASCII input and draws in a bitmap font
    (that is what a uv-managed interpreter ships). Silent failure looks like a bug
    in this watcher, so say it out loud."""

    def test_warns_when_xft_is_missing(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(aw_dialog, "tk_font_system", lambda: "x11")

        with caplog.at_level("WARNING", logger=aw_dialog.logger.name):
            assert aw_dialog.warn_on_degraded_tk() == "x11"

        assert "Xft" in caplog.text

    def test_quiet_on_a_normal_build(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(aw_dialog, "tk_font_system", lambda: "xft")

        with caplog.at_level("WARNING", logger=aw_dialog.logger.name):
            assert aw_dialog.warn_on_degraded_tk() == "xft"

        assert caplog.text == ""

    def test_quiet_when_the_build_does_not_say(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(aw_dialog, "tk_font_system", lambda: None)

        with caplog.at_level("WARNING", logger=aw_dialog.logger.name):
            aw_dialog.warn_on_degraded_tk()

        assert caplog.text == ""


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


class TestUnansweredTimeout:
    """A forgotten dialog should hide itself, so the main loop can re-raise it."""

    def test_no_timeout_when_disabled(self, nonmodal) -> None:
        assert _make_dialog(nonmodal, timeout_ms=None)._timeout_timer is None
        assert _make_dialog(nonmodal, timeout_ms=0)._timeout_timer is None

    def test_timeout_armed_for_a_completed_period(self, nonmodal) -> None:
        assert _make_dialog(nonmodal, timeout_ms=60_000)._timeout_timer is not None

    def test_ongoing_dialog_waits_for_the_user_to_return(self, nonmodal) -> None:
        """While the user is still away the dialog must stay put — they haven't
        had a chance to see it yet.  The countdown starts when they return."""
        dialog = _make_dialog(
            nonmodal,
            afk_start=datetime.now(UTC) - timedelta(minutes=10),
            is_ongoing=True,
            timeout_ms=60_000,
        )
        assert dialog._timeout_timer is None

        dialog._mark_returned()

        assert dialog._timeout_timer is not None

    def test_deferred_while_the_afk_watcher_says_nobody_is_there(self, nonmodal) -> None:
        """Also true for a *completed* period prompted while the user is still away
        (the still-AFK backfill path): burning the fuse down on a dialog nobody can
        see would leave it hidden half the time, including when they sit down."""
        away = True
        dialog = _make_dialog(nonmodal, timeout_ms=60_000, still_afk_check=lambda: away)
        assert dialog._timeout_timer is None

        away = False
        dialog._poll_afk()

        assert dialog._timeout_timer is not None

    def test_armed_immediately_when_the_user_is_present(self, nonmodal) -> None:
        dialog = _make_dialog(nonmodal, timeout_ms=60_000, still_afk_check=lambda: False)
        assert dialog._timeout_timer is not None

    def test_a_failing_check_defers_rather_than_arms(self, nonmodal) -> None:
        """If we can't tell whether anyone is there, don't hide the dialog."""

        def boom() -> bool:
            raise RuntimeError("server down")

        assert _make_dialog(nonmodal, timeout_ms=60_000, still_afk_check=boom)._timeout_timer is None

    def test_typing_restarts_the_countdown(self, nonmodal) -> None:
        """The dialog must not self-destruct mid-sentence."""
        dialog = _make_dialog(nonmodal, timeout_ms=60_000)
        first = dialog._timeout_timer

        dialog._arm_timeout()

        assert dialog._timeout_timer != first
        assert dialog.bind("<KeyPress>")  # keypresses are wired to the re-arm

    def test_timeout_closes_the_dialog_without_a_result(self, nonmodal) -> None:
        """End to end: the countdown really fires and snoozes the dialog."""
        dialog = _make_dialog(nonmodal, timeout_ms=50)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not getattr(dialog, "_snoozing", False):
            dialog.update()
            time.sleep(0.01)

        assert dialog._snoozing is True
        assert dialog.result is None
