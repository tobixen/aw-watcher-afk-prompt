"""Regression tests for closing the split dialog.

Cancelling the split dialog left its window on screen, frozen: tkinter's
``Dialog.cancel`` destroys the toplevel without withdrawing it first, and
``destroy()`` only queues the X request — Tk sends it on the next pass through
the event loop.  There is no next pass: ask_split_activities returns,
ask_string returns None, and the watcher goes back to its poll sleep.  The X
server never hears about the destroy, so the window stays mapped with nobody
reading its events.

``Dialog.ok`` withdraws before destroying and ``wm withdraw`` flushes, which is
why only Cancel/Escape froze.
"""

import tkinter as tk
from datetime import UTC, datetime

import pytest

import aw_watcher_afk_prompt.dialog as aw_dialog
from aw_watcher_afk_prompt.split_dialog import SplitActivityDialog, ask_split_activities

try:
    aw_dialog.get_root()  # importing is display-free; these tests are not
except tk.TclError:  # pragma: no cover - depends on the test environment
    pytest.skip("No display available", allow_module_level=True)

# Asked at module level rather than inside the check below: a silent skip would
# leave the one assertion that protects this regression looking green.
xlib_display = pytest.importorskip("Xlib.display", reason="python-xlib not installed")


@pytest.fixture
def root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def cancelled(monkeypatch) -> list[int]:
    """Make the modal wait end the way a Cancel click ends it.

    The X window is created and the dialog closed for real; only the parts that
    would block the test — or flash a dialog onto the screen of whoever runs it
    — are stubbed out.  Returns the list the dialog's window id lands in.
    """
    window_ids: list[int] = []

    def cancel_instead_of_waiting(self, *a, **kw) -> None:  # noqa: ARG001
        window_ids.append(self.winfo_id())
        self.cancel()

    for name in ("wait_visibility", "grab_set", "deiconify"):
        monkeypatch.setattr(SplitActivityDialog, name, lambda self, *a, **kw: None)  # noqa: ARG005
    monkeypatch.setattr(SplitActivityDialog, "wait_window", cancel_instead_of_waiting)
    return window_ids


def _x_window_exists(window_id: int) -> bool:
    """Ask the X server directly, over a connection of our own."""
    from Xlib.error import BadDrawable, BadWindow

    display = xlib_display.Display()
    try:
        display.create_resource_object("window", window_id).get_attributes()
    except (BadWindow, BadDrawable):
        return False
    else:
        return True
    finally:
        display.close()


def test_cancel_leaves_no_window_on_the_x_server(root, cancelled) -> None:
    """The window must be gone even though no event loop runs afterwards."""
    assert ask_split_activities("test", "test", datetime.now(UTC), 600.0, [], parent=root) is None
    # Deliberately nothing here: no update(), no mainloop, no further dialog —
    # this is the watcher going back to sleep after ask_string returned None.

    (window_id,) = cancelled
    assert not _x_window_exists(window_id)


def test_a_dialog_that_fails_to_build_leaves_no_window(root, monkeypatch) -> None:
    """A constructor that raises never gets as far as destroying its window.

    Nor does it hand the caller a reference to destroy it by, so the window
    outlived the failure — invisible while the raise happens before the dialog
    is deiconified, which is luck rather than design.
    """
    window_ids: list[int] = []

    def boom(self, master) -> None:  # noqa: ARG001
        window_ids.append(self.winfo_id())
        raise RuntimeError("body blew up")

    for name in ("wait_visibility", "grab_set", "deiconify"):
        monkeypatch.setattr(SplitActivityDialog, name, lambda self, *a, **kw: None)  # noqa: ARG005
    monkeypatch.setattr(SplitActivityDialog, "body", boom)

    with pytest.raises(RuntimeError, match="body blew up"):
        ask_split_activities("test", "test", datetime.now(UTC), 600.0, [], parent=root)

    (window_id,) = window_ids
    assert not _x_window_exists(window_id)


def test_ask_string_reuses_the_shared_root_for_the_split_dialog(monkeypatch) -> None:
    """No second Tk root: that is what made the frozen window permanent.

    A throwaway root is a second Tk interpreter and a second X connection in
    the same process, and only the interpreter that owns a window can flush its
    removal — so the dialog's own root has to still be reachable when it closes.
    """
    captured: dict[str, object] = {}

    def fake_ask_split(title, prompt, afk_start, afk_duration_seconds, history, parent=None):  # noqa: ARG001
        captured["parent"] = parent
        return None

    monkeypatch.setattr("aw_watcher_afk_prompt.split_dialog.ask_split_activities", fake_ask_split)

    class FakeMainDialog:
        """Stands in for the dialog whose Split button the user pressed."""

        def __init__(self, *a, **kw) -> None:  # noqa: ARG002
            self.split_mode = True
            self.afk_duration_seconds = 600.0
            self.result = None

    monkeypatch.setattr(aw_dialog, "AWAfkPromptDialog", FakeMainDialog)

    assert aw_dialog.ask_string("test", "test", [], afk_start=datetime.now(UTC)) is None
    assert captured["parent"] is aw_dialog.get_root()
