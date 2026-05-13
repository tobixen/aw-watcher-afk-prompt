# ruff: noqa: E501
import datetime

import aw_core

from aw_watcher_afk_prompt.core import AWAfkPromptState, adjust_gap_start_for_window_activity

AFK = "afk"
NOT_AFK = "not-afk"
INF = 1000 * 365 * 24 * 60 * 60  # About 1000 years of seconds. Cannot use float("inf") in timedeltas.
FIRST_DATE = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


TupleEvent = tuple[int | datetime.datetime, int, str]


def _tuple_to_event(tup: TupleEvent) -> aw_core.Event:
    match tup[0]:
        case int():
            timestamp = FIRST_DATE + datetime.timedelta(seconds=tup[0])
        case datetime.datetime():
            timestamp = tup[0]
        case str():
            timestamp = datetime.datetime.fromisoformat(tup[0])
    return aw_core.Event(timestamp=timestamp, duration=tup[1], data={"status": tup[2]})


def _event_to_tuple(event: aw_core.Event) -> tuple[int, int]:
    return (int(event.timestamp.timestamp()), int(event.duration.total_seconds()))


def test_get_unseen_afk_events_initial():
    # Just some initial tests not meant to handle particular bugs or anything.
    init_events_tups: list[TupleEvent] = [
        (0, 60, NOT_AFK),
        (60, 20, AFK),
        (80, 100, NOT_AFK),
    ]
    init_events = [_tuple_to_event(tup) for tup in init_events_tups]

    # Nothing should pass the recency threshold (gap from year 1970 is outside the 100s window).
    # Use a fresh state — this call marks the expired gap as seen, which is correct.
    assert list(AWAfkPromptState([]).get_unseen_afk_events(init_events, 100, 10)) == []

    # With INF recency window the gap IS found. Use a fresh state (not tainted by the check above).
    state = AWAfkPromptState([])
    assert [_event_to_tuple(e) for e in state.get_unseen_afk_events(init_events, INF, 10)] == [(60, 20)]

    # Should be excluded by the duration threshold.
    assert list(state.get_unseen_afk_events(init_events, INF, 21)) == []

    # Test that recency works by end timestamp and not beginning timestamp.
    now = datetime.datetime.now().astimezone(datetime.UTC)
    events = list(state.get_unseen_afk_events([*init_events, _tuple_to_event((now, 10, NOT_AFK))], 10, 21))
    assert len(events) == 1
    assert events[0].timestamp == FIRST_DATE + datetime.timedelta(seconds=80 + 100)
    assert int(events[0].duration.total_seconds()) == int(
        (now - (FIRST_DATE + datetime.timedelta(seconds=80 + 100))).total_seconds()
    )


def test_double_ask_1():
    # 2023-09-26 12:08:03 [DEBUG]: Checking for unseen in: [('2023-09-26T12:04:55.820000-04:00', 5, 'not-afk'), ('2023-09-26T11:58:16.969000-04:00', 398, 'afk'), ('2023-09-26T11:58:16.969000-04:00', 190, 'afk'), ('2023-09-26T11:55:10.545000-04:00', 186, 'not-afk'), ('2023-09-26T11:55:10.545000-04:00', 15, 'not-afk'), ('2023-09-26T11:49:27.192000-04:00', 343, 'afk'), ('2023-09-26T11:49:27.192000-04:00', 190, 'afk'), ('2023-09-26T11:11:22.127000-04:00', 2285, 'not-afk'), ('2023-09-26T11:00:43.666000-04:00', 638, 'afk'), ('2023-09-26T11:00:43.666000-04:00', 190, 'afk')]  (aw_watcher_afk_prompt.core:164)
    # 2023-09-26 12:08:03 [DEBUG]: Found event to note: {'id': None, 'timestamp': datetime.datetime(2023, 9, 26, 15, 58, 16, 969000, tzinfo=datetime.timezone.utc), 'duration': datetime.timedelta(seconds=398, microseconds=851000), 'data': {'message': 'talk to nikhil'}}  (aw_watcher_afk_prompt.core:181)
    # 2023-09-26 12:08:22 [DEBUG]: Checking for unseen in: [('2023-09-26T12:08:11.328000-04:00', 10, 'not-afk'), ('2023-09-26T12:05:00.965000-04:00', 190, 'afk'), ('2023-09-26T12:05:00.965000-04:00', 190, 'afk'), ('2023-09-26T12:04:55.820000-04:00', 5, 'not-afk'), ('2023-09-26T11:58:16.969000-04:00', 398, 'afk'), ('2023-09-26T11:58:16.969000-04:00', 190, 'afk'), ('2023-09-26T11:55:10.545000-04:00', 186, 'not-afk'), ('2023-09-26T11:55:10.545000-04:00', 15, 'not-afk'), ('2023-09-26T11:49:27.192000-04:00', 343, 'afk'), ('2023-09-26T11:49:27.192000-04:00', 190, 'afk')]  (aw_watcher_afk_prompt.core:164)
    # 2023-09-26 12:08:22 [DEBUG]: Found event to note: {'id': None, 'timestamp': datetime.datetime(2023, 9, 26, 15, 58, 16, 969000, tzinfo=datetime.timezone.utc), 'duration': datetime.timedelta(seconds=594, microseconds=359000), 'data': {'message': 'Lunch: Talking to James and Ben about virtualization'}}  (aw_watcher_afk_prompt.core:181)

    # fmt: off
    first = [                                                                                                                                                          ("2023-09-26T12:04:55.820000-04:00", 5, "not-afk"), ("2023-09-26T11:58:16.969000-04:00", 398, "afk"), ("2023-09-26T11:58:16.969000-04:00", 190, "afk"), ("2023-09-26T11:55:10.545000-04:00", 186, "not-afk"), ("2023-09-26T11:55:10.545000-04:00", 15, "not-afk"), ("2023-09-26T11:49:27.192000-04:00", 343, "afk"), ("2023-09-26T11:49:27.192000-04:00", 190, "afk"), ("2023-09-26T11:11:22.127000-04:00", 2285, "not-afk"), ("2023-09-26T11:00:43.666000-04:00", 638, "afk"), ("2023-09-26T11:00:43.666000-04:00", 190, "afk")]
    second = [("2023-09-26T12:08:11.328000-04:00", 10, "not-afk"), ("2023-09-26T12:05:00.965000-04:00", 190, "afk"), ("2023-09-26T12:05:00.965000-04:00", 190, "afk"), ("2023-09-26T12:04:55.820000-04:00", 5, "not-afk"), ("2023-09-26T11:58:16.969000-04:00", 398, "afk"), ("2023-09-26T11:58:16.969000-04:00", 190, "afk"), ("2023-09-26T11:55:10.545000-04:00", 186, "not-afk"), ("2023-09-26T11:55:10.545000-04:00", 15, "not-afk"), ("2023-09-26T11:49:27.192000-04:00", 343, "afk"), ("2023-09-26T11:49:27.192000-04:00", 190, "afk")]
    # fmt: on
    first = [_tuple_to_event(tup) for tup in first]
    second = [_tuple_to_event(tup) for tup in second]

    state = AWAfkPromptState([])
    first_unseen = list(state.get_unseen_afk_events(first, INF, 3 * 60))
    for event in first_unseen:
        state.mark_event_as_seen(event)

    second_unseen = list(state.get_unseen_afk_events(second, INF, 3 * 60))
    assert len(second_unseen) == 1
    assert second_unseen[0].timestamp == datetime.datetime.fromisoformat("2023-09-26T12:05:00.820000-04:00")
    assert int(second_unseen[0].duration.total_seconds()) == 190


def test_double_ask_suspend_afk():
    # This happened after opening up my computer after it was suspended but then not actually doing anything for a while.
    # 2023-10-13 08:44:54 [DEBUG]: Checking for unseen in: [('2023-10-13T08:41:50.337000-04:00', 0.0, 'not-afk'), ('2023-10-13T07:09:50.154000-04:00', 1724.216, 'not-afk'), ('2023-10-12T23:40:00.083000-04:00', 26990.07, 'afk'), ('2023-10-12T23:40:00.083000-04:00', 26984.982452, 'afk'), ('2023-10-12T23:39:49.928000-04:00', 10.155, 'not-afk'), ('2023-10-12T22:19:22.173000-04:00', 4827.754, 'afk'), ('2023-10-12T22:19:22.173000-04:00', 190.570087, 'afk'), ('2023-10-12T22:01:01.254000-04:00', 1100.919, 'not-afk'), ('2023-10-12T17:31:40.928000-04:00', 16160.325, 'afk'), ('2023-10-12T17:31:40.928000-04:00', 190.403036, 'afk')]  (aw_watcher_afk_prompt.core:160)
    # 2023-10-13 08:44:54 [DEBUG]: Found event to note: {'id': None, 'timestamp': datetime.datetime(2023, 10, 13, 11, 38, 34, 370000, tzinfo=datetime.timezone.utc), 'duration': datetime.timedelta(seconds=3795, microseconds=967000), 'data': {'message': 'Sleeping / Feeding Jupiter / Eating / Playing with Jupiter / Composter'}}  (aw_watcher_afk_prompt.core:173)
    # 2023-10-13 08:47:54 [DEBUG]: Checking for unseen in: [('2023-10-13T08:47:38.792000-04:00', 10.149, 'not-afk'), ('2023-10-13T08:41:50.337000-04:00', 348.454, 'afk'), ('2023-10-13T08:41:50.337000-04:00', 195.633261, 'afk'), ('2023-10-13T08:41:50.337000-04:00', 190.507251, 'afk'), ('2023-10-13T07:09:50.154000-04:00', 1724.216, 'not-afk'), ('2023-10-12T23:40:00.083000-04:00', 26990.07, 'afk'), ('2023-10-12T23:40:00.083000-04:00', 26984.982452, 'afk'), ('2023-10-12T23:39:49.928000-04:00', 10.155, 'not-afk'), ('2023-10-12T22:19:22.173000-04:00', 4827.754, 'afk'), ('2023-10-12T22:19:22.173000-04:00', 190.570087, 'afk')]  (aw_watcher_afk_prompt.core:160)
    # 2023-10-13 08:47:54 [DEBUG]: Found event to note: {'id': None, 'timestamp': datetime.datetime(2023, 10, 13, 11, 38, 34, 370000, tzinfo=datetime.timezone.utc), 'duration': datetime.timedelta(seconds=4144, microseconds=422000), 'data': {'message': 'Driving to work'}}  (aw_watcher_afk_prompt.core:173)

    # fmt: off
    #                                                                                                                                                                                 Notice: not-afk with zero length...                      From here back things are the same.
    first = [                                                                                                                                                                         ("2023-10-13T08:41:50.337000-04:00", 0.0, "not-afk"),    ("2023-10-13T07:09:50.154000-04:00", 1724.216, "not-afk"), ("2023-10-12T23:40:00.083000-04:00", 26990.07, "afk"), ("2023-10-12T23:40:00.083000-04:00", 26984.982452, "afk"), ("2023-10-12T23:39:49.928000-04:00", 10.155, "not-afk"), ("2023-10-12T22:19:22.173000-04:00", 4827.754, "afk"), ("2023-10-12T22:19:22.173000-04:00", 190.570087, "afk"), ("2023-10-12T22:01:01.254000-04:00", 1100.919, "not-afk"), ("2023-10-12T17:31:40.928000-04:00", 16160.325, "afk"), ("2023-10-12T17:31:40.928000-04:00", 190.403036, "afk")]
    second = [("2023-10-13T08:47:38.792000-04:00", 10.149, "not-afk"), ("2023-10-13T08:41:50.337000-04:00", 348.454, "afk"), ("2023-10-13T08:41:50.337000-04:00", 195.633261, "afk"), ("2023-10-13T08:41:50.337000-04:00", 190.507251, "afk"), ("2023-10-13T07:09:50.154000-04:00", 1724.216, "not-afk"), ("2023-10-12T23:40:00.083000-04:00", 26990.07, "afk"), ("2023-10-12T23:40:00.083000-04:00", 26984.982452, "afk"), ("2023-10-12T23:39:49.928000-04:00", 10.155, "not-afk"), ("2023-10-12T22:19:22.173000-04:00", 4827.754, "afk"), ("2023-10-12T22:19:22.173000-04:00", 190.570087, "afk")]
    # fmt: on

    first = [_tuple_to_event(tup) for tup in first]
    second = [_tuple_to_event(tup) for tup in second]

    state = AWAfkPromptState([])
    first_unseen = list(state.get_unseen_afk_events(first, INF, 3 * 60))
    for event in first_unseen:
        state.mark_event_as_seen(event)
    # This is the main thing being tested. If there are three events then we will be asked to put in duplicate information in the next step.
    assert len(first_unseen) == 2

    second_unseen = list(state.get_unseen_afk_events(second, INF, 3 * 60))
    assert len(second_unseen) == 1
    expected_start = datetime.datetime.fromisoformat("2023-10-13T07:09:50.154000-04:00") + datetime.timedelta(
        seconds=1724.216
    )
    expected_end = datetime.datetime.fromisoformat("2023-10-13T08:47:38.792000-04:00")
    assert second_unseen[0].timestamp == expected_start
    assert second_unseen[0].duration.total_seconds() == (expected_end - expected_start).total_seconds()


def test_long_afk_over_24_hours():
    """Test that AFK periods >= 24 hours are detected correctly.

    Regression test: previously used timedelta.seconds (which drops the days component)
    instead of timedelta.total_seconds(), causing AFK periods >= 24h to be missed.
    """
    now = datetime.datetime.now().astimezone(datetime.UTC)
    day_ago = now - datetime.timedelta(hours=25)
    events: list[TupleEvent] = [
        (day_ago, 60, NOT_AFK),
        # 25-hour gap here
        (now, 10, NOT_AFK),
    ]
    events = [_tuple_to_event(tup) for tup in events]

    state = AWAfkPromptState([])
    # duration threshold is 5 minutes (300s); the 25-hour gap should easily pass
    unseen = list(state.get_unseen_afk_events(events, INF, 300))
    assert len(unseen) == 1
    # The gap duration should be ~25 hours (minus the 60s first event)
    assert unseen[0].duration.total_seconds() > 24 * 3600


def test_afk_exactly_at_threshold():
    """Test that AFK periods exactly at the duration threshold are NOT detected (strict >).

    The threshold uses strict greater-than, so a gap of exactly 300s with
    durration_thresh=300 should not be detected.
    """
    events: list[TupleEvent] = [
        (0, 60, NOT_AFK),
        (360, 60, NOT_AFK),  # gap of exactly 300s (60 to 360)
    ]
    events = [_tuple_to_event(tup) for tup in events]

    state = AWAfkPromptState([])
    # Exactly at threshold — should NOT be detected
    assert list(state.get_unseen_afk_events(events, INF, 300)) == []
    # One second less threshold — should be detected
    assert len(list(state.get_unseen_afk_events(events, INF, 299))) == 1


# ---------------------------------------------------------------------------
# Helpers for window-activity gap-adjustment tests
# ---------------------------------------------------------------------------


def _make_window_event(start_s: int, duration_s: int) -> aw_core.Event:
    """Create a minimal window event (app/title, no 'status' key)."""
    timestamp = FIRST_DATE + datetime.timedelta(seconds=start_s)
    return aw_core.Event(timestamp=timestamp, duration=duration_s, data={"app": "foot", "title": "terminal"})


def _make_gap(start_s: int, duration_s: int) -> aw_core.Event:
    timestamp = FIRST_DATE + datetime.timedelta(seconds=start_s)
    return aw_core.Event(timestamp=timestamp, duration=datetime.timedelta(seconds=duration_s), data={})


# ---------------------------------------------------------------------------
# Tests for adjust_gap_start_for_window_activity
# ---------------------------------------------------------------------------


def test_adjust_gap_start_advances_when_window_activity():
    """Gap start is advanced to the AFK event start when window events exist in [gap_start, afk_start).

    Scenario:
    - T=0-120:   not-afk (last heartbeat)
    - T=120-240: idle countdown — window heartbeat present, but not-afk ended
    - T=240-600: afk event in bucket  (idle timeout triggered)
    - T=600-660: not-afk (user returns)

    The gap in not-afk events is T=120 to T=600 (480 s).
    Window activity exists at T=120 to T=240, so that period was actually active.
    Expected: gap start advances from T=120 to T=240; duration shrinks from 480 s to 360 s.
    """
    afk_events = [
        _tuple_to_event(t)
        for t in [
            (0, 120, NOT_AFK),
            (240, 360, AFK),
            (600, 60, NOT_AFK),
        ]
    ]
    window_events = [_make_window_event(120, 120)]

    gap = _make_gap(120, 480)
    adjusted = adjust_gap_start_for_window_activity(gap, afk_events, window_events)

    assert adjusted.timestamp == FIRST_DATE + datetime.timedelta(seconds=240)
    assert adjusted.duration == datetime.timedelta(seconds=360)


def test_adjust_gap_start_unchanged_no_window_activity():
    """Gap is not changed when there are no window events (suspend/poweroff scenario)."""
    afk_events = [
        _tuple_to_event(t)
        for t in [
            (0, 120, NOT_AFK),
            (240, 360, AFK),
            (600, 60, NOT_AFK),
        ]
    ]
    window_events: list[aw_core.Event] = []

    gap = _make_gap(120, 480)
    adjusted = adjust_gap_start_for_window_activity(gap, afk_events, window_events)

    assert adjusted.timestamp == gap.timestamp
    assert adjusted.duration == gap.duration


def test_adjust_gap_start_unchanged_no_afk_event():
    """Gap is not changed when no AFK event is found within the gap (no snap target)."""
    afk_events = [
        _tuple_to_event(t)
        for t in [
            (0, 120, NOT_AFK),
            (600, 60, NOT_AFK),
        ]
    ]
    window_events = [_make_window_event(120, 120)]

    gap = _make_gap(120, 480)
    adjusted = adjust_gap_start_for_window_activity(gap, afk_events, window_events)

    assert adjusted.timestamp == gap.timestamp
    assert adjusted.duration == gap.duration


def test_adjust_gap_start_unchanged_afk_at_gap_start():
    """Gap is not changed when afk event starts exactly at gap start (nothing to advance)."""
    afk_events = [
        _tuple_to_event(t)
        for t in [
            (0, 120, NOT_AFK),
            (120, 480, AFK),  # afk starts exactly at gap start
            (600, 60, NOT_AFK),
        ]
    ]
    window_events = [_make_window_event(120, 30)]

    gap = _make_gap(120, 480)
    adjusted = adjust_gap_start_for_window_activity(gap, afk_events, window_events)

    assert adjusted.timestamp == gap.timestamp
    assert adjusted.duration == gap.duration


# ---------------------------------------------------------------------------
# Integration test: get_unseen_afk_events with window_events parameter
# ---------------------------------------------------------------------------


def test_get_unseen_afk_events_with_window_advances_gap_start():
    """With window events, the detected gap start is advanced past the 2-min idle countdown."""
    afk_events = [
        _tuple_to_event(t)
        for t in [
            (0, 120, NOT_AFK),
            (240, 360, AFK),
            (600, 60, NOT_AFK),
        ]
    ]
    window_events = [_make_window_event(120, 120)]

    state = AWAfkPromptState([])
    unseen = list(state.get_unseen_afk_events(afk_events, INF, 300, window_events))

    assert len(unseen) == 1
    assert unseen[0].timestamp == FIRST_DATE + datetime.timedelta(seconds=240)
    assert int(unseen[0].duration.total_seconds()) == 360


def test_get_unseen_afk_events_no_window_param_unchanged():
    """Without window events, get_unseen_afk_events behaves as before (no regression)."""
    afk_events = [
        _tuple_to_event(t)
        for t in [
            (0, 120, NOT_AFK),
            (240, 360, AFK),
            (600, 60, NOT_AFK),
        ]
    ]

    state = AWAfkPromptState([])
    # Gap from 120-600 (480 s), threshold 300 s → should be found
    unseen = list(state.get_unseen_afk_events(afk_events, INF, 300))

    assert len(unseen) == 1
    assert unseen[0].timestamp == FIRST_DATE + datetime.timedelta(seconds=120)
    assert int(unseen[0].duration.total_seconds()) == 480


def test_expired_gap_not_repeated():
    """Gaps that expire from the depth window (too old) must be auto-marked as seen.

    If they aren't, every poll cycle re-reports the same expired gap, blocking
    any new gaps from ever being shown.
    """
    # Build two not-afk events with an AFK gap between them, all in the past.
    old_start = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
    events = [
        aw_core.Event(timestamp=old_start, duration=datetime.timedelta(seconds=100), data={"status": NOT_AFK}),
        aw_core.Event(
            timestamp=old_start + datetime.timedelta(seconds=200),
            duration=datetime.timedelta(seconds=100),
            data={"status": NOT_AFK},
        ),
    ]
    state = AWAfkPromptState([])

    # recency_thresh=10 means the gap (ended in year 2000) is way too old to prompt.
    # But it is long enough (100 s > 60 s duration threshold).
    first_call = list(state.get_unseen_afk_events(events, recency_thresh=10, durration_thresh=60))
    assert first_call == [], "expired gap should not be yielded"

    # On a second call with the same data the gap must not be reported again (was a bug).
    second_call = list(state.get_unseen_afk_events(events, recency_thresh=10, durration_thresh=60))
    assert second_call == [], "expired gap must not be re-reported after it was already noted"
    # Verify it was actually added to the in-memory seen set, not just silently dropped.
    assert state.has_event(
        aw_core.Event(
            timestamp=old_start + datetime.timedelta(seconds=100),
            duration=datetime.timedelta(seconds=100),
            data={},
        )
    ), "expired gap should have been marked as seen"
