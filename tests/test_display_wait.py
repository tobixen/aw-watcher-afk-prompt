"""Tests for surviving a display server that is not there yet.

The hidden Tk root used to be created at import time, so a watcher started
before the compositor was up died in ``import`` -- before logging was even
configured.  systemd then restarted it every 10s: 92 identical tracebacks in the
journal, and only luck kept it under the start-limit that had already given up
on aw-watcher-window-wayland for good.  The root is therefore created lazily and
waited for, with the retry interval inside the process.
"""

import os
import subprocess
import sys
import tkinter as tk
from unittest.mock import MagicMock

import pytest

import aw_watcher_afk_prompt.dialog as aw_dialog


@pytest.fixture(autouse=True)
def no_leaked_root():
    """Keep a fake root out of the module state the other UI tests share."""
    saved = aw_dialog._root
    aw_dialog._root = None
    yield
    aw_dialog._root = saved


@pytest.fixture
def fake_tk(monkeypatch):
    """Replace tk.Tk with a stub that fails a given number of times first."""

    def install(failures: int):
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] <= failures:
                raise tk.TclError('couldn\'t connect to display ":0"')
            return MagicMock(name="tk-root")

        monkeypatch.setattr(aw_dialog.tk, "Tk", factory)
        return calls

    return install


@pytest.fixture
def slept():
    """Collect what wait_for_display would have slept, instead of sleeping."""
    return []


class TestWaitForDisplay:
    def test_returns_root_without_waiting_when_display_is_up(self, fake_tk, slept) -> None:
        calls = fake_tk(failures=0)
        root = aw_dialog.wait_for_display(timeout_minutes=15, sleep=slept.append)
        assert root is aw_dialog.get_root()
        assert calls["n"] == 1
        assert slept == []

    def test_retries_until_the_display_appears(self, fake_tk, slept) -> None:
        calls = fake_tk(failures=3)
        root = aw_dialog.wait_for_display(timeout_minutes=15, interval=5, sleep=slept.append)
        assert root is not None
        assert calls["n"] == 4
        assert slept == [5, 5, 5]

    def test_the_waited_for_root_is_the_one_the_dialogs_use(self, fake_tk, slept) -> None:
        fake_tk(failures=1)
        root = aw_dialog.wait_for_display(timeout_minutes=15, interval=5, sleep=slept.append)
        assert aw_dialog.root is root
        # ...and it is hidden, or an empty window flashes up behind every dialog.
        root.withdraw.assert_called_once()

    def test_gives_up_after_the_timeout(self, fake_tk, slept) -> None:
        """Giving up (and letting systemd retry) beats looping in silence forever."""
        calls = fake_tk(failures=1000)
        with pytest.raises(tk.TclError):
            aw_dialog.wait_for_display(timeout_minutes=1, interval=5, sleep=slept.append)
        assert calls["n"] == 12  # 60s / 5s
        assert slept == [5] * 11  # no pointless sleep after the last attempt

    def test_zero_timeout_tries_once(self, fake_tk, slept) -> None:
        calls = fake_tk(failures=1000)
        with pytest.raises(tk.TclError):
            aw_dialog.wait_for_display(timeout_minutes=0, interval=5, sleep=slept.append)
        assert calls["n"] == 1
        assert slept == []

    def test_second_call_reuses_the_root(self, fake_tk, slept) -> None:
        calls = fake_tk(failures=0)
        first = aw_dialog.wait_for_display(timeout_minutes=15, sleep=slept.append)
        second = aw_dialog.wait_for_display(timeout_minutes=15, sleep=slept.append)
        assert first is second
        assert calls["n"] == 1


def test_importing_the_dialog_module_needs_no_display() -> None:
    """The regression itself: importing must not touch the display server."""
    result = subprocess.run(
        [sys.executable, "-c", "import aw_watcher_afk_prompt.dialog"],
        env={k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
