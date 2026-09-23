"""The watcher once sat blocked for 29 hours without a word in the log.

These tests cover the fixes: server calls that cannot block forever, a timeout
treated like any other server trouble, a stack dump on demand, and a log line
for the one dialog that used to appear silently.
"""

import inspect
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aw_client.client
import aw_core
import pytest
import requests

import aw_watcher_afk_prompt.__main__ as main
from aw_watcher_afk_prompt import core

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def silent_server():
    """A TCP server that accepts connections (via the backlog) but never answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    yield sock.getsockname()[1]
    sock.close()


@pytest.fixture
def bare_client(monkeypatch) -> core.ActivityWatchClientWithTimeout:
    """A timeout client without the lock file and on-disk request queue a real one
    leaves behind in the user's data directories."""
    monkeypatch.setattr(aw_client.client, "SingleInstance", MagicMock())
    monkeypatch.setattr(aw_client.client, "RequestQueue", MagicMock())
    return core.ActivityWatchClientWithTimeout(client_name="aw-watcher-afk-prompt-test", testing=True)


class TestServerCallsTimeOut:
    def test_get_gives_up_on_a_silent_server(self, bare_client, silent_server) -> None:
        client = bare_client
        client.server_address = f"http://127.0.0.1:{silent_server}"
        client.request_timeout = (1.0, 1.0)
        outcome: dict = {}

        def call() -> None:
            try:
                client._get("info")
            except Exception as e:  # noqa: BLE001
                outcome["error"] = e

        # Run in a thread so a regression fails the test instead of hanging it.
        start = time.monotonic()
        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(10)

        assert not worker.is_alive(), "request to a silent server never returned"
        assert isinstance(outcome.get("error"), requests.exceptions.Timeout)
        assert time.monotonic() - start < 5

    @pytest.mark.parametrize(("method", "args"), [("_post", ("buckets/b/events", [])), ("_delete", ("buckets/b",))])
    def test_post_and_delete_pass_the_timeout(self, bare_client, monkeypatch, method, args) -> None:
        captured: dict = {}

        def fake_request(url, **kwargs):
            captured.update(kwargs, url=url)
            return MagicMock()

        monkeypatch.setattr(core.requests, method.lstrip("_"), fake_request)
        getattr(bare_client, method)(*args)

        assert captured["timeout"] == bare_client.request_timeout

    def test_delete_keeps_falsy_data_as_given(self, bare_client, monkeypatch) -> None:
        """Mirror aw-client: only None becomes {}."""
        captured: dict = {}
        monkeypatch.setattr(core.requests, "delete", lambda url, **k: captured.update(k) or MagicMock())
        bare_client._delete("buckets/b", data=[])
        assert captured["data"] == "[]"

    def test_read_timeout_is_short_enough_for_a_ui_callback(self) -> None:
        """Dialog refreshes call the server from Tk callbacks; a stall freezes the dialog."""
        assert core.ActivityWatchClientWithTimeout.request_timeout[1] <= 20

    def test_main_builds_the_timeout_client(self) -> None:
        """Reverting a call site to the plain client would bring the hang back."""
        assert "ActivityWatchClient(" not in inspect.getsource(main)


class TestStartupRetries:
    def test_read_timeout_at_startup_is_retried(self, monkeypatch) -> None:
        sentinel = object()
        attempts = iter([requests.exceptions.ReadTimeout("server stalled"), sentinel])

        def fake_client(*a, **k):  # noqa: ARG001
            result = next(attempts)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(main, "AWAfkPromptClient", fake_client)
        monkeypatch.setattr(main.time, "sleep", lambda s: None)  # noqa: ARG005

        assert main.get_state_retries(MagicMock()) is sentinel


class TestTimeoutIsServerTrouble:
    def test_still_afk_deep_scan_timeout_does_not_escape(self, monkeypatch) -> None:
        """A read timeout is not a ConnectionError; it must still be handled."""
        ongoing = aw_core.Event(id=None, timestamp=datetime(2026, 6, 7, 11, 57, tzinfo=UTC), duration=timedelta(0))
        state = SimpleNamespace(
            state=SimpleNamespace(recent_events=[]),
            get_ongoing_afk_event=MagicMock(return_value=ongoing),
        )
        args = SimpleNamespace(backfill=True, length=5.0, stale_warning=15.0, min_active=0.0, prompt_timeout=5.0)

        def timing_out_deep_scan(*a, **k):  # noqa: ARG001
            raise requests.exceptions.ReadTimeout("server stalled")

        shown: list = []
        monkeypatch.setattr(main, "_deep_scan", timing_out_deep_scan)
        monkeypatch.setattr(main, "prompt_ongoing", lambda event, *a, **k: shown.append(event))
        monkeypatch.setattr(main, "_post_ongoing_response", lambda *a, **k: None)

        main._handle_still_afk(state, args, prompted_ongoing_start=None)

        assert shown == [ongoing]


class TestStackDumpOnDemand:
    @pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="no SIGUSR1 on this platform")
    def test_sigusr1_writes_every_threads_stack(self) -> None:
        code = (
            "import os, signal\n"
            "from aw_watcher_afk_prompt.__main__ import enable_stack_dumps\n"
            "enable_stack_dumps()\n"
            "os.kill(os.getpid(), signal.SIGUSR1)\n"
            "print('still alive')\n"
        )
        env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)

        assert result.returncode == 0, result.stderr
        assert "still alive" in result.stdout
        assert "most recent call first" in result.stderr


class TestOngoingDialogIsLogged:
    def test_showing_the_live_dialog_is_logged(self, monkeypatch, caplog) -> None:
        """Without this, the log could not tell whether a dialog was on screen."""
        monkeypatch.setattr(main.aw_dialog, "ask_string", lambda *a, **k: None)
        start = datetime.now(UTC) - timedelta(minutes=5)
        ongoing = aw_core.Event(id=None, timestamp=start, duration=timedelta(0))

        with caplog.at_level(logging.INFO, logger="aw_watcher_afk_prompt"):
            main.prompt_ongoing(ongoing, [])

        assert any("ongoing" in r.getMessage().lower() and r.levelno == logging.INFO for r in caplog.records)


def _bare_prompt_client() -> core.AWAfkPromptClient:
    client = object.__new__(core.AWAfkPromptClient)
    client.client = MagicMock()
    client.bucket_id = "aw-watcher-afk-prompt_test"
    client.state = MagicMock()
    return client


class TestPostRetries:
    def test_read_timeout_is_not_retried(self, monkeypatch) -> None:
        """The server may have saved the event before the answer stalled: a retry
        could write it twice. Not marking it seen leaves the next scan to ask the
        server whether it is there."""
        client = _bare_prompt_client()
        client.client.insert_event.side_effect = requests.exceptions.ReadTimeout("server stalled")
        monkeypatch.setattr(core.time, "sleep", lambda s: None)  # noqa: ARG005
        event = aw_core.Event(timestamp=datetime.now(UTC), duration=timedelta(minutes=10))

        with pytest.raises(requests.exceptions.ReadTimeout):
            client.post_event(event, "reading")

        assert client.client.insert_event.call_count == 1
        client.state.mark_event_as_seen.assert_not_called()

    def test_connect_timeout_is_retried(self, monkeypatch) -> None:
        """Nothing reached the server, so trying again cannot duplicate."""
        client = _bare_prompt_client()
        client.client.insert_event.side_effect = [requests.exceptions.ConnectTimeout("no route"), None]
        monkeypatch.setattr(core.time, "sleep", lambda s: None)  # noqa: ARG005
        event = aw_core.Event(timestamp=datetime.now(UTC), duration=timedelta(minutes=10))

        client.post_event(event, "reading")

        assert client.client.insert_event.call_count == 2
        client.state.mark_event_as_seen.assert_called_once()


def test_presence_check_skips_on_timeout() -> None:
    """An optional check must not abort the whole poll when the server stalls."""
    client = _bare_prompt_client()
    client.presence_buckets = ["aw-watcher-web-firefox_test"]
    client.client.get_buckets.side_effect = requests.exceptions.ReadTimeout("server stalled")

    assert client.get_presence_last_seen() is None
