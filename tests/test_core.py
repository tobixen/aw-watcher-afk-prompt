# ruff: noqa: E501
import datetime

import aw_core

from aw_watcher_afk_prompt.core import AWAfkPromptState, adjust_gap_start_for_window_activity, get_ongoing_afk_start

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


def test_short_not_afk_merged_with_min_duration():
    """Short not-afk events below min_not_afk_duration are ignored, merging surrounding AFK periods.

    Regression scenario: three brief "laptop touches" (72s, 82s, 48s) separated a real
    5-minute gap from a 100-minute one. Without the filter the 5-min gap was below threshold
    after idle-countdown adjustment; the 100-min gap outlasted the depth window before the
    user returned, so it was never prompted for in real time.
    """
    # Timeline (seconds from epoch 0):
    #   0–1000  : real work (not-afk, 1000s)
    #   1120    : AFK starts (after ~120s idle countdown)
    #   1310–1382: brief touch 1 (72s)
    #   1390–1472: brief touch 2 (82s)
    #   1480–1528: brief touch 3 (48s)
    #   1530    : long AFK begins
    #   5130    : user returns (not-afk)
    events: list[TupleEvent] = [
        (0, 1000, NOT_AFK),  # real work ends at t=1000
        (1120, 190, AFK),  # first AFK (3m10s)
        (1310, 72, NOT_AFK),  # brief touch 1
        (1390, 82, NOT_AFK),  # brief touch 2
        (1480, 48, NOT_AFK),  # brief touch 3
        (1530, 3600, AFK),  # long AFK (60 min)
        (5130, 600, NOT_AFK),  # user returns (10 min of real work, above 120s threshold)
    ]
    events = [_tuple_to_event(t) for t in events]

    # Without filter: the only gap between not-afk events is:
    #   (t=1000 -> t=1310): 310s — passes 300s threshold, should yield one event
    #   (t=1528 -> t=5130): 3602s — but wait, the three brief touches ARE non-afk,
    #   so gaps are: 1000-1310 (310s), 1472-1480 (8s), 1528-5130 (3602s)
    # With min_not_afk_duration=120s: brief touches (72s, 82s, 48s) are filtered,
    # leaving non-afk at t=0 (1000s) and t=5130 (600s).
    # Gap between them: t=1000 to t=5130 = 4130s — one merged big gap.
    state_no_filter = AWAfkPromptState([])
    unseen_no_filter = list(state_no_filter.get_unseen_afk_events(events, INF, 300))

    state_filtered = AWAfkPromptState([])
    unseen_filtered = list(state_filtered.get_unseen_afk_events(events, INF, 300, min_not_afk_duration=120))

    # Without filter: two gaps (310s and 3602s) both above 300s threshold
    assert len(unseen_no_filter) == 2

    # With filter: one merged gap (4130s)
    assert len(unseen_filtered) == 1
    assert unseen_filtered[0].duration.total_seconds() > 4000


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


def test_advance_gap_logged_once_across_polls(caplog):
    """The 'Advancing gap start' INFO line is logged once per gap, not every poll.

    Regression: a still-pending gap was re-adjusted and re-logged at INFO on every
    poll (~every 5 s), flooding the log. It should log INFO once per distinct gap,
    then DEBUG on repeats, keyed on the advanced start (the stable AFK-event start).
    """
    import logging

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

    with caplog.at_level(logging.DEBUG, logger="aw_watcher_afk_prompt.core"):
        for _ in range(3):
            advanced = list(state.get_unseen_afk_events(afk_events, INF, 300, window_events))
            # The gap is still advanced on every poll, only the logging is deduped.
            assert advanced[0].timestamp == FIRST_DATE + datetime.timedelta(seconds=240)

    advance_infos = [
        r for r in caplog.records if r.levelno == logging.INFO and "Advancing gap start" in r.getMessage()
    ]
    assert len(advance_infos) == 1, f"expected 1 INFO advance log across 3 polls, got {len(advance_infos)}"


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


def test_expired_gap_is_deferred_and_re_presented():
    """Gaps that fall outside the depth window are deferred, not discarded.

    First poll: gap is too old → not yielded yet, added to _deferred.
    Second poll: gap is yielded from _deferred so the user gets a chance to answer.
    After mark_event_as_seen: gap is no longer re-presented.
    """
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

    # First call: gap is beyond recency_thresh → deferred, not yet yielded
    first_call = list(state.get_unseen_afk_events(events, recency_thresh=10, durration_thresh=60))
    assert first_call == [], "expired gap should not be yielded on first detection"
    assert len(state._deferred) == 1, "expired gap should be added to _deferred"

    # Second call: deferred gap is re-presented so the user can answer it
    second_call = list(state.get_unseen_afk_events(events, recency_thresh=10, durration_thresh=60))
    assert len(second_call) == 1, "deferred gap should be re-presented on next poll"

    # Once answered (marked seen), it should not appear again
    state.mark_event_as_seen(second_call[0])
    third_call = list(state.get_unseen_afk_events(events, recency_thresh=10, durration_thresh=60))
    assert third_call == [], "answered gap should not be re-presented"
    assert state._deferred == [], "_deferred should be empty after gap is answered"


def test_min_active_preserves_most_recent_as_right_boundary():
    """The most recent not-afk event is kept even when shorter than min_not_afk_duration.

    Models the boot/resume scenario: the watcher just started, the first not-afk
    event is only 2 s old (below min_active=90s), but the preceding boot gap (60 min)
    must still be detected — the short event is the right boundary, not a "brief touch".

    Regression: without this fix, the 2s event was filtered, leaving no right boundary,
    so no gap between the last pre-boot activity and the current session was detected.
    """
    now = datetime.datetime.now().astimezone(datetime.UTC)
    events = [
        # Long activity 70 minutes ago (survives min_active filter)
        aw_core.Event(
            timestamp=now - datetime.timedelta(minutes=75),
            duration=datetime.timedelta(minutes=5),
            data={"status": NOT_AFK},
        ),
        # 60-minute gap here (the boot/suspend period)
        # Very short return event: 2s, shorter than min_active=90s
        aw_core.Event(
            timestamp=now - datetime.timedelta(seconds=2),
            duration=datetime.timedelta(seconds=2),
            data={"status": NOT_AFK},
        ),
    ]

    state = AWAfkPromptState([])
    unseen_no_filter = list(state.get_unseen_afk_events(events, INF, 300))
    assert len(unseen_no_filter) == 1, "sanity check: gap detected without filter"

    state2 = AWAfkPromptState([])
    unseen_filtered = list(state2.get_unseen_afk_events(events, INF, 300, min_not_afk_duration=90))
    assert len(unseen_filtered) == 1, "boot gap must be detected even when return event is shorter than min_active"
    assert unseen_filtered[0].duration.total_seconds() > 60 * 60 - 30


def test_stale_events_dont_suppress_boot_gap_via_right_boundary():
    """Stale events mixed in from an inactive bucket don't hide the real boot gap.

    Models the aw-watcher-lid scenario: the lid watcher has been inactive for months.
    With min_active=90s, all recent post-boot events (83s each) are filtered,
    leaving only stale events. Without the fix, only stale events generate gaps.
    The real boot gap needs the 2s return event preserved as the right boundary.

    Uses a 24h recency window: stale events (100d old) produce gaps that end
    100d ago (excluded), while the boot gap ends at now-2s (included).
    """
    now = datetime.datetime.now().astimezone(datetime.UTC)
    months_ago = now - datetime.timedelta(days=100)

    events = [
        # One stale not-afk event from 100 days ago (survives min_active, but gap ends outside 24h)
        aw_core.Event(
            timestamp=months_ago,
            duration=datetime.timedelta(minutes=30),
            data={"status": NOT_AFK},
        ),
        # Long pre-boot activity from 70 min ago (survives min_active, left boundary)
        aw_core.Event(
            timestamp=now - datetime.timedelta(minutes=75),
            duration=datetime.timedelta(minutes=5),
            data={"status": NOT_AFK},
        ),
        # Recent post-boot events: all 83s, below min_active=90s.
        # They have 7s gaps between them (matching real aw-watcher-afk heartbeat pattern)
        # so period_union does NOT merge them into a single longer event.
        aw_core.Event(
            timestamp=now - datetime.timedelta(seconds=180),
            duration=datetime.timedelta(seconds=83),
            data={"status": NOT_AFK},
        ),
        aw_core.Event(
            timestamp=now - datetime.timedelta(seconds=90),
            duration=datetime.timedelta(seconds=83),
            data={"status": NOT_AFK},
        ),
        # The ongoing return event: 2s (right boundary, must be preserved)
        aw_core.Event(
            timestamp=now - datetime.timedelta(seconds=2),
            duration=datetime.timedelta(seconds=2),
            data={"status": NOT_AFK},
        ),
    ]

    # Use a 60-min recency window: the stale-to-preboot gap ends at now-75min which is
    # outside the 60-min window (expires), but the boot gap ends at now-2s (inside).
    recency_60min = 60 * 60

    state = AWAfkPromptState([])
    # With min_active=90s and 60-min recency: only the boot gap (70 min) should be found.
    # Without the fix, 2s event is filtered → no right boundary → boot gap not detected.
    unseen = list(state.get_unseen_afk_events(events, recency_60min, 300, min_not_afk_duration=90))

    boot_gaps = [e for e in unseen if e.duration.total_seconds() > 50 * 60]
    assert len(boot_gaps) == 1, "boot gap (~70 min) must be detected — stale events must not suppress it"


# ---------------------------------------------------------------------------
# Tests for get_ongoing_afk_start
# ---------------------------------------------------------------------------


def test_get_ongoing_afk_start_returns_none_when_not_afk():
    """Returns None when most recent event is not-afk."""
    events: list[TupleEvent] = [
        (0, 60, NOT_AFK),
        (60, 30, AFK),
        (90, 10, NOT_AFK),
    ]
    result = get_ongoing_afk_start([_tuple_to_event(t) for t in events])
    assert result is None


def test_get_ongoing_afk_start_returns_none_when_empty():
    """Returns None for empty event list."""
    assert get_ongoing_afk_start([]) is None


def test_get_ongoing_afk_start_returns_end_of_last_not_afk():
    """Returns end of the last not-afk event when currently AFK."""
    events: list[TupleEvent] = [
        (0, 100, NOT_AFK),
        (100, 50, NOT_AFK),
        (150, 300, AFK),
    ]
    result = get_ongoing_afk_start([_tuple_to_event(t) for t in events])
    expected = FIRST_DATE + datetime.timedelta(seconds=150)  # end of not-afk at t=100, duration=50
    assert result == expected


def test_get_ongoing_afk_start_returns_none_when_no_not_afk_events():
    """Returns None when all events are AFK (no not-afk boundary found)."""
    events: list[TupleEvent] = [
        (0, 300, AFK),
        (300, 300, AFK),
    ]
    result = get_ongoing_afk_start([_tuple_to_event(t) for t in events])
    assert result is None
