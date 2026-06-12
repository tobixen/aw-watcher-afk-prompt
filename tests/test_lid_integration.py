"""Tests for lid watcher integration."""

import datetime
from unittest.mock import MagicMock

import aw_core
import pytest

from aw_watcher_afk_prompt.core import (
    AWAfkPromptClient,
    AWAfkPromptError,
    AWAfkPromptState,
    find_lid_bucket,
    is_afk,
)


def test_find_lid_bucket_found() -> None:
    """Test finding lid bucket when it exists."""
    buckets = {
        "aw-watcher-afk_hostname": {},
        "aw-watcher-lid_hostname": {},
    }

    lid_bucket = find_lid_bucket(buckets)
    assert lid_bucket == "aw-watcher-lid_hostname"


def test_find_lid_bucket_not_found() -> None:
    """Test finding lid bucket when it doesn't exist."""
    buckets = {
        "aw-watcher-afk_hostname": {},
    }

    lid_bucket = find_lid_bucket(buckets)
    assert lid_bucket is None


def test_find_lid_bucket_multiple() -> None:
    """Test error when multiple lid buckets found."""
    buckets = {
        "aw-watcher-lid_hostname1": {},
        "aw-watcher-lid_hostname2": {},
    }

    with pytest.raises(AWAfkPromptError, match="too many lid buckets"):
        find_lid_bucket(buckets)


def test_is_afk_regular() -> None:
    """Test is_afk with regular AFK event."""
    event = aw_core.Event(data={"status": "afk"})
    assert is_afk(event) is True


def test_is_afk_system() -> None:
    """Test is_afk with system-afk event from lid watcher."""
    event = aw_core.Event(data={"status": "system-afk"})
    assert is_afk(event) is True


def test_is_afk_not_afk() -> None:
    """Test is_afk with not-afk event."""
    event = aw_core.Event(data={"status": "not-afk"})
    assert is_afk(event) is False


# ---------------------------------------------------------------------------
# Lid "open" events must never count as user presence
# ---------------------------------------------------------------------------

UTC = datetime.UTC
T0 = datetime.datetime(2026, 6, 11, 3, 0, 0, tzinfo=UTC)


def _afk_event(offset_s: int, duration_s: int, status: str) -> aw_core.Event:
    return aw_core.Event(
        timestamp=T0 + datetime.timedelta(seconds=offset_s),
        duration=datetime.timedelta(seconds=duration_s),
        data={"status": status},
    )


def _lid_event(offset_s: int, duration_s: int, lid_state: str) -> aw_core.Event:
    status = "system-afk" if lid_state == "closed" else "not-afk"
    return aw_core.Event(
        timestamp=T0 + datetime.timedelta(seconds=offset_s),
        duration=datetime.timedelta(seconds=duration_s),
        data={"status": status, "lid_state": lid_state, "event_source": "lid"},
    )


def _make_client(afk_events: list[aw_core.Event], lid_events: list[aw_core.Event]) -> AWAfkPromptClient:
    """Build an AWAfkPromptClient with a fake server, bypassing __init__."""
    inst = AWAfkPromptClient.__new__(AWAfkPromptClient)
    inst.client = MagicMock()
    inst.afk_bucket_id = "aw-watcher-afk_test"
    inst.lid_bucket_id = "aw-watcher-lid_test"
    inst.window_bucket_id = None
    inst.history_limit = 100
    inst.state = AWAfkPromptState([])

    def get_events(bucket_id: str, limit: int = 100, start=None, end=None):
        events = afk_events if bucket_id == inst.afk_bucket_id else lid_events
        if start is not None:
            events = [e for e in events if e.timestamp >= start]
        return list(events)

    inst.client.get_events.side_effect = get_events
    return inst


def test_fetch_excludes_lid_open_events() -> None:
    """Lid-open ("not-afk") events say nothing about presence and must be dropped at fetch time.

    aw-watcher-lid emits one "not-afk" event spanning the *entire* time the lid
    is open, whether or not anyone is at the keyboard. Only the system-afk
    (closed/suspend) lid events carry information for gap detection.
    """
    afk = [_afk_event(0, 100, "not-afk"), _afk_event(2000, 100, "not-afk")]
    lid = [_lid_event(0, 1000, "closed"), _lid_event(1000, 1100, "open")]

    client = _make_client(afk, lid)
    all_events, _ = client._fetch_events_with_dynamic_limit()

    lid_open = [e for e in all_events if e.data.get("lid_state") == "open"]
    lid_closed = [e for e in all_events if e.data.get("lid_state") == "closed"]
    assert lid_open == [], "lid open (not-afk) events must not enter gap detection"
    assert len(lid_closed) == 1, "lid closed (system-afk) events must be kept"


def test_fetch_excludes_lid_open_events_time_bounded() -> None:
    """The time-bounded (backfill) fetch path must apply the same lid filtering."""
    afk = [_afk_event(0, 100, "not-afk"), _afk_event(2000, 100, "not-afk")]
    lid = [_lid_event(0, 2100, "open")]

    client = _make_client(afk, lid)
    all_events, _ = client._fetch_events_with_dynamic_limit(start_time=T0)

    assert all(e.data.get("lid_state") != "open" for e in all_events)


def test_lid_open_event_does_not_mask_afk_gap() -> None:
    """Regression for the lost period 2026-06-11 05:46:52-06:16:16 (local).

    The user was away 05:46-06:14 with the laptop lid open. aw-watcher-lid had
    posted a single "open / not-afk" event spanning 05:06:47-06:41:17, which —
    once finalized — completely covered the gap in the real not-afk events, so
    the period was never prompted for and was silently lost.
    """
    # Shape of the real data, in seconds relative to T0 (= 05:00 local):
    #   not-afk  2595-2691  (05:43:15-05:44:51)
    #   afk      2812-4476  (05:46:52-06:14:36)
    #   not-afk  4480-4576  (06:14:40-06:16:16)  brief return
    #   afk      4700-6071  (06:18:20-06:41:11)
    #   not-afk  6075-6135  (06:41:15-)          real return
    afk = [
        _afk_event(2595, 96, "not-afk"),
        _afk_event(2812, 1664, "afk"),
        _afk_event(4480, 96, "not-afk"),
        _afk_event(4700, 1371, "afk"),
        _afk_event(6075, 60, "not-afk"),
    ]
    # The killer: lid open "not-afk" event spanning 05:06:47-06:41:17 (407-6077)
    lid = [_lid_event(407, 5670, "open")]

    client = _make_client(afk, lid)
    all_events, _ = client._fetch_events_with_dynamic_limit()

    inf = 10**9
    gaps = list(AWAfkPromptState([]).get_unseen_afk_events(all_events, inf, 300))

    gap_starts = [int((g.timestamp - T0).total_seconds()) for g in gaps]
    assert 2691 in gap_starts, f"the 05:44:51-06:14:40 gap must be detected, got starts={gap_starts}"
