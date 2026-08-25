"""Tests for noticing that the AFK feed itself has died.

Every prompt this watcher can ever make is derived from gaps *between* not-afk
events in the afk bucket.  When whatever writes that bucket stops, there is no
right-hand boundary for a gap, so the watcher detects nothing and says nothing.
Observed in the wild: a reboot left aw-watcher-window-wayland dead, the afk
bucket stopped 20 hours before the user went to bed, and the morning brought no
prompt at all -- with 20 hours of clean INFO-level silence in the journal.

The hard part is that silence is not evidence.  The feed says nothing at all
while the user is away, so "no events for N minutes" describes every lunch break
as well as every dead watcher.  What separates them is silence across a moment
when the feed had to speak -- a startup, a resume -- which is what these tests
pin down.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import aw_core

import aw_watcher_afk_prompt.__main__ as main
from aw_watcher_afk_prompt.core import AWAfkPromptClient

STALE_AFTER = 10.0  # minutes; the feed_stale default


def _feed_client(
    lags_seconds: list[float] | None,
    lid_events: list[aw_core.Event] | None = None,
) -> AWAfkPromptClient:
    """An AWAfkPromptClient whose afk bucket holds events ending N seconds ago.

    ``None`` models a bucket that has never held an event at all. ``lid_events``
    populates an optional lid bucket verbatim.
    """
    now = datetime.datetime.now(datetime.UTC)
    inst = AWAfkPromptClient.__new__(AWAfkPromptClient)
    inst.client = MagicMock()
    inst.afk_bucket_id = "aw-watcher-afk_test"
    inst.lid_bucket_id = "aw-watcher-lid_test" if lid_events is not None else None
    inst.window_bucket_id = None
    inst.history_limit = 100

    events = [
        aw_core.Event(
            timestamp=now - datetime.timedelta(seconds=lag + 60),
            duration=datetime.timedelta(seconds=60),
            data={"status": "not-afk"},
        )
        for lag in (lags_seconds or [])
    ]

    def get_events(bucket_id, limit=100, start=None, end=None):  # noqa: ARG001
        bucket = events if bucket_id == inst.afk_bucket_id else (lid_events or [])
        return sorted(bucket, key=lambda e: e.timestamp)[-limit:]

    inst.client.get_events.side_effect = get_events
    return inst


def _lid_event(seconds_ago: float, status: str, duration: float = 0.0) -> aw_core.Event:
    """A lid-watcher event: its *timestamp* is the lid/suspend transition."""
    now = datetime.datetime.now(datetime.UTC)
    return aw_core.Event(
        timestamp=now - datetime.timedelta(seconds=seconds_ago),
        duration=datetime.timedelta(seconds=duration),
        data={"status": status, "event_source": "lid"},
    )


class TestFeedLastSeen:
    def test_reports_the_end_of_the_newest_event(self) -> None:
        """A live feed keeps extending its newest event, so this tracks 'now'."""
        last_seen = _feed_client([5.0]).get_feed_last_seen()
        assert last_seen is not None
        assert (datetime.datetime.now(datetime.UTC) - last_seen).total_seconds() < 30

    def test_only_the_newest_event_counts(self) -> None:
        """Older events must not make a stopped feed look alive."""
        last_seen = _feed_client([20 * 3600, 21 * 3600, 22 * 3600]).get_feed_last_seen()
        assert last_seen is not None
        lag = (datetime.datetime.now(datetime.UTC) - last_seen).total_seconds()
        assert 20 * 3600 <= lag < 20 * 3600 + 30

    def test_empty_feed_has_never_been_seen(self) -> None:
        assert _feed_client(None).get_feed_last_seen() is None

    def test_only_one_event_is_fetched(self) -> None:
        """The check runs on every poll, so it must stay a cheap query."""
        client = _feed_client([5.0])
        client.get_feed_last_seen()
        _, kwargs = client.client.get_events.call_args
        assert kwargs["limit"] == 1


def _clocks(mono: float, wall_offset_seconds: float = 0.0) -> main._Clocks:
    """Clocks at a fixed wall time, so 'lag' in the tests is exact."""
    wall = datetime.datetime(2026, 8, 25, 6, 50, tzinfo=datetime.UTC) + datetime.timedelta(seconds=wall_offset_seconds)
    return main._Clocks(mono, wall)


def _state(last_seen: datetime.datetime | None, answered_at: datetime.datetime | None = None) -> SimpleNamespace:
    """A client with no lid watcher, so the startup/resume anchor stands alone."""
    return SimpleNamespace(
        afk_bucket_id="aw-watcher-afk_test",
        get_feed_last_seen=MagicMock(return_value=last_seen),
        get_lid_presence_last_seen=MagicMock(return_value=None),
        get_presence_last_seen=MagicMock(return_value=None),
        last_answer_at=answered_at,
    )


class TestResumeDetection:
    """CLOCK_MONOTONIC stops while suspended; the wall clock does not."""

    def test_ordinary_poll_is_not_a_resume(self) -> None:
        assert not main._resumed(_clocks(1000.0, 0), _clocks(1005.0, 5))

    def test_suspend_shows_up_as_clock_drift(self) -> None:
        # 8 hours of wall clock, a couple of seconds of monotonic.
        assert main._resumed(_clocks(1000.0, 0), _clocks(1002.0, 8 * 3600))

    def test_a_slow_poll_is_not_a_resume(self) -> None:
        """A blocked poll (a dialog sat open) advances both clocks together."""
        assert not main._resumed(_clocks(1000.0, 0), _clocks(1000.0 + 3600, 3600))


class TestCheckAfkFeed:
    """The startup and resume anchors: silence across a moment the feed had to speak."""

    def test_zero_disables_the_check(self) -> None:
        assert main._FeedState.armed(0, "this watcher started") == main._FeedState.disarmed()

    def test_disabled_never_queries_the_server(self) -> None:
        state = _state(None)
        assert main._check_afk_feed(state, 0, main._FeedState.disarmed()) == main._FeedState.disarmed()
        state.get_feed_last_seen.assert_not_called()

    def test_a_reporting_feed_disarms_the_check(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        armed = main._FeedState.armed(STALE_AFTER, "this watcher started", _clocks(0, -30).wall)
        feed = main._check_afk_feed(_state(_clocks(0, -10).wall), STALE_AFTER, armed, now=_clocks(0).wall)
        assert feed == main._FeedState.disarmed()
        box.assert_not_called()

    def test_a_feed_quiet_since_just_before_startup_is_alive(self, monkeypatch) -> None:
        """Restarting the watcher and walking away must not raise an alarm: a feed
        that reported a minute before startup is not dead, it has nothing to say."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        armed = main._FeedState.armed(STALE_AFTER, "this watcher started", _clocks(0, -3600).wall)
        feed = main._check_afk_feed(_state(_clocks(0, -3660).wall), STALE_AFTER, armed, now=_clocks(0).wall)
        assert feed == main._FeedState.disarmed()
        box.assert_not_called()

    def test_silence_is_given_the_full_grace_period(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        armed = main._FeedState.armed(STALE_AFTER, "this watcher started", _clocks(0, -(STALE_AFTER * 60 - 60)).wall)
        feed = main._check_afk_feed(_state(_clocks(0, -20 * 3600).wall), STALE_AFTER, armed, now=_clocks(0).wall)
        assert feed == armed  # still watching, nothing said yet
        box.assert_not_called()

    def test_persistent_silence_after_startup_notifies_once(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        armed = main._FeedState.armed(STALE_AFTER, "this watcher started", _clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        dead = _state(_clocks(0, -20 * 3600).wall)

        feed = main._check_afk_feed(dead, STALE_AFTER, armed, now=_clocks(0).wall)
        assert feed.notified
        assert box.call_count == 1
        message = " ".join(str(a) for a in box.call_args[0])
        # The unwritten bucket belongs in the message: it is the one thing that
        # says *which* watcher to go and restart.
        assert "aw-watcher-afk_test" in message
        # And the age, not just a clock time -- "last reported 14:04" reads as
        # today for a feed that died yesterday.
        assert "20 hours" in message and "ago" in message
        # ...and which anchor caught it, so the claim can be checked.
        assert "this watcher started" in message

        feed = main._check_afk_feed(dead, STALE_AFTER, feed, now=_clocks(0, 3600).wall)
        assert box.call_count == 1

    def test_a_feed_that_never_reported_anything_notifies(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        armed = main._FeedState.armed(STALE_AFTER, "this watcher started", _clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        feed = main._check_afk_feed(_state(None), STALE_AFTER, armed, now=_clocks(0).wall)
        assert feed.notified
        assert box.call_count == 1

    def test_re_arming_after_a_resume_can_notify_again(self, monkeypatch) -> None:
        """The morning case, with no lid watcher installed: dead feed, machine
        suspended all night, resume detected from the clocks drifting apart."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        dead = _state(_clocks(0, -20 * 3600).wall)

        armed = main._FeedState.armed(STALE_AFTER, "this watcher started", _clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        feed = main._check_afk_feed(dead, STALE_AFTER, armed, now=_clocks(0).wall)
        assert box.call_count == 1

        # ...suspend, resume, and the loop re-arms at the moment of the resume.
        feed = main._FeedState.armed(STALE_AFTER, "the machine resumed", _clocks(0, 8 * 3600).wall)
        feed = main._check_afk_feed(dead, STALE_AFTER, feed, now=_clocks(0, 8 * 3600 + STALE_AFTER * 60 + 60).wall)
        assert box.call_count == 2

    def test_an_ordinary_long_absence_stays_silent(self, monkeypatch) -> None:
        """The whole point: an hour away is silence from the feed too, and must
        not be reported as a dead feed."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        # Startup two hours ago, the feed reported until the user left an hour ago.
        armed = main._FeedState.armed(STALE_AFTER, "this watcher started", _clocks(0, -2 * 3600).wall)
        state = _state(_clocks(0, -3600).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, armed, now=_clocks(0).wall)
        assert feed == main._FeedState.disarmed()
        # ...and from there on, no amount of further silence says anything.
        feed = main._check_afk_feed(state, STALE_AFTER, feed, now=_clocks(0, 9 * 3600).wall)
        box.assert_not_called()


class TestLidPresence:
    """The lid watcher is a separate process, so its word is independent evidence."""

    def test_returns_the_newest_presence_transition(self) -> None:
        client = _feed_client([], lid_events=[_lid_event(9 * 3600, "not-afk"), _lid_event(1200, "not-afk")])
        at = client.get_lid_presence_last_seen()
        assert at is not None
        assert 1200 <= (datetime.datetime.now(datetime.UTC) - at).total_seconds() < 1230

    def test_ignores_afk_lid_events(self) -> None:
        """A closed lid or a suspend says the user is *gone*, not present."""
        client = _feed_client([], lid_events=[_lid_event(1200, "system-afk"), _lid_event(600, "system-afk")])
        assert client.get_lid_presence_last_seen() is None

    def test_uses_the_transition_not_the_span(self) -> None:
        """aw-watcher-lid's not-afk event spans the whole time the lid is open, so
        its *end* tracks now even while the user sleeps beside it. Only the moment
        the lid was opened is evidence that a human did something."""
        client = _feed_client([], lid_events=[_lid_event(8 * 3600, "not-afk", duration=8 * 3600)])
        at = client.get_lid_presence_last_seen()
        assert at is not None
        assert (datetime.datetime.now(datetime.UTC) - at).total_seconds() >= 8 * 3600

    def test_no_lid_watcher_no_evidence(self) -> None:
        assert _feed_client([]).get_lid_presence_last_seen() is None


def _lid_state(last_seen: datetime.datetime | None, lid_at: datetime.datetime | None) -> SimpleNamespace:
    return SimpleNamespace(
        afk_bucket_id="aw-watcher-afk_test",
        get_feed_last_seen=MagicMock(return_value=last_seen),
        get_lid_presence_last_seen=MagicMock(return_value=lid_at),
        get_presence_last_seen=MagicMock(return_value=None),
        last_answer_at=None,
    )


class TestLidEvidence:
    """The morning case, without needing to have observed the suspend ourselves."""

    def test_lid_opened_while_the_feed_stays_silent(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _lid_state(_clocks(0, -20 * 3600).wall, _clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed.notified
        assert box.call_count == 1
        assert "lid" in " ".join(str(a) for a in box.call_args[0]).lower()

    def test_the_feed_gets_its_grace_after_the_lid_opens(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _lid_state(_clocks(0, -20 * 3600).wall, _clocks(0, -120).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert not feed.notified
        box.assert_not_called()

    def test_an_absence_with_the_lid_open_is_not_evidence(self, monkeypatch) -> None:
        """Left the desk hours ago, lid open, feed quiet because nobody is there:
        the newest lid transition is older than the feed's last word, which is
        exactly what a healthy feed looks like."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _lid_state(_clocks(0, -3 * 3600).wall, _clocks(0, -9 * 3600).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed == main._FeedState.disarmed()
        box.assert_not_called()

    def test_notifies_once(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _lid_state(_clocks(0, -20 * 3600).wall, _clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        feed = main._FeedState.disarmed()
        for _ in range(3):
            feed = main._check_afk_feed(state, STALE_AFTER, feed, now=_clocks(0).wall)
        assert box.call_count == 1

    def test_a_recovered_feed_can_be_reported_dead_again(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        lid_at = _clocks(0, -(STALE_AFTER * 60 + 60)).wall
        feed = main._check_afk_feed(
            _lid_state(_clocks(0, -20 * 3600).wall, lid_at),
            STALE_AFTER,
            main._FeedState.disarmed(),
            now=_clocks(0).wall,
        )
        assert box.call_count == 1
        # feed comes back: reports something after the lid opened
        feed = main._check_afk_feed(_lid_state(_clocks(0, -30).wall, lid_at), STALE_AFTER, feed, now=_clocks(0).wall)
        assert not feed.notified
        # ...and dies again before the next lid transition
        feed = main._check_afk_feed(
            _lid_state(_clocks(0, -2 * 3600).wall, _clocks(0, -3600).wall), STALE_AFTER, feed, now=_clocks(0).wall
        )
        assert box.call_count == 2

    def test_a_live_feed_costs_no_lid_query(self, monkeypatch) -> None:
        """The check runs every poll; don't query a second bucket for nothing."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _lid_state(_clocks(0, -30).wall, None)
        main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        state.get_lid_presence_last_seen.assert_not_called()


class TestAnsweredPromptEvidence:
    """Someone typing an answer is the least deniable presence signal there is."""

    def test_posting_an_answer_is_recorded(self) -> None:
        client = _feed_client([5.0])
        assert client.last_answer_at is None
        client.state = MagicMock()
        client.bucket_id = "aw-watcher-afk-prompt_test"
        client.post_event(
            aw_core.Event(timestamp=datetime.datetime.now(datetime.UTC), duration=datetime.timedelta(minutes=6)),
            "lunch",
        )
        assert client.last_answer_at is not None
        assert (datetime.datetime.now(datetime.UTC) - client.last_answer_at).total_seconds() < 30

    def test_a_split_answer_is_recorded_too(self) -> None:
        """Answering in split mode goes through post_split_events, not post_event.
        Missing it there meant a split answer was no evidence at all."""
        client = _feed_client([5.0])
        assert client.last_answer_at is None
        client.state = MagicMock()
        client.bucket_id = "aw-watcher-afk-prompt_test"
        client.post_split_events(
            aw_core.Event(timestamp=datetime.datetime.now(datetime.UTC), duration=datetime.timedelta(minutes=30)),
            [],
        )
        assert client.last_answer_at is not None
        assert (datetime.datetime.now(datetime.UTC) - client.last_answer_at).total_seconds() < 30

    def test_an_answered_prompt_with_a_silent_feed_convicts_it(self, monkeypatch) -> None:
        """Your point 1: the prompt was answered, so somebody is demonstrably
        here, and the feed still has not noticed."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _state(_clocks(0, -20 * 3600).wall, answered_at=_clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed.notified
        assert box.call_count == 1
        assert "answered" in " ".join(str(a) for a in box.call_args[0]).lower()

    def test_the_feed_gets_its_grace_after_an_answer(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _state(_clocks(0, -20 * 3600).wall, answered_at=_clocks(0, -60).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert not feed.notified
        box.assert_not_called()

    def test_an_answer_older_than_the_feeds_last_word_proves_nothing(self, monkeypatch) -> None:
        """Answered a prompt, kept working, then went for lunch: the feed reported
        after the answer, so its later silence is just the lunch."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _state(_clocks(0, -3600).wall, answered_at=_clocks(0, -2 * 3600).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed == main._FeedState.disarmed()
        box.assert_not_called()


def _bucket_client(patterns, buckets: dict[str, float | None]) -> AWAfkPromptClient:
    """A client whose other buckets hold one event each, N seconds old.

    ``None`` for a bucket means it exists but is empty.
    """
    now = datetime.datetime.now(datetime.UTC)
    inst = AWAfkPromptClient.__new__(AWAfkPromptClient)
    inst.client = MagicMock()
    inst.afk_bucket_id = "aw-watcher-afk_test"
    inst.lid_bucket_id = "aw-watcher-lid_test"
    inst.window_bucket_id = "aw-watcher-window_test"
    inst.bucket_id = "aw-watcher-afk-prompt_test"
    inst.history_limit = 100
    inst.presence_buckets = patterns
    inst.__dict__["_all_buckets"] = dict.fromkeys(buckets, {})
    inst.client.get_buckets.return_value = dict.fromkeys(buckets, {})

    def get_events(bucket_id, limit=100, start=None, end=None):  # noqa: ARG001
        age = buckets.get(bucket_id)
        if age is None:
            return []
        # A span, to pin down which end of it counts as evidence.
        return [
            aw_core.Event(
                timestamp=now - datetime.timedelta(seconds=age),
                duration=datetime.timedelta(seconds=age),
                data={"app": "whatever"},
            )
        ]

    inst.client.get_events.side_effect = get_events
    return inst


class TestPresenceBuckets:
    """Other watchers reporting while the feed says nothing: your point 2."""

    def test_matches_configured_buckets_by_substring(self) -> None:
        client = _bucket_client(
            ["aw-watcher-web", "aw-watcher-emacs"],
            {"aw-watcher-web-firefox_test": 3600, "aw-watcher-emacs_test": 1200, "aw-watcher-tmux": 60},
        )
        at = client.get_presence_last_seen()
        assert at is not None
        # The emacs event (20 min) wins over the web one (1 h); tmux is not
        # configured here, so its 1-minute event is ignored.
        assert 1200 <= (datetime.datetime.now(datetime.UTC) - at).total_seconds() < 1230

    def test_the_feeds_own_buckets_are_never_presence_evidence(self) -> None:
        """The afk, window, lid and own buckets are either the feed itself or
        already handled; a pattern matching them would be circular."""
        client = _bucket_client(
            ["aw-watcher"],
            {
                "aw-watcher-afk_test": 60,
                "aw-watcher-window_test": 60,
                "aw-watcher-lid_test": 60,
                "aw-watcher-afk-prompt_test": 60,
            },
        )
        assert client.get_presence_last_seen() is None

    def test_uses_the_start_not_the_end_of_the_span(self) -> None:
        """Same reason as the lid watcher: an event whose end tracks "now" (a tab
        left open, an editor left running) would report presence all night. When
        the activity *started* is the part that says a human did something."""
        client = _bucket_client(["aw-watcher-web"], {"aw-watcher-web-firefox_test": 4 * 3600})
        at = client.get_presence_last_seen()
        assert at is not None
        assert (datetime.datetime.now(datetime.UTC) - at).total_seconds() >= 4 * 3600

    def test_nothing_configured_means_no_queries(self) -> None:
        client = _bucket_client([], {"aw-watcher-web-firefox_test": 60})
        assert client.get_presence_last_seen() is None
        client.client.get_events.assert_not_called()

    def test_empty_buckets_are_no_evidence(self) -> None:
        client = _bucket_client(["aw-watcher-web"], {"aw-watcher-web-firefox_test": None})
        assert client.get_presence_last_seen() is None


def _presence_state(last_seen: datetime.datetime | None, presence_at: datetime.datetime | None) -> SimpleNamespace:
    return SimpleNamespace(
        afk_bucket_id="aw-watcher-afk_test",
        get_feed_last_seen=MagicMock(return_value=last_seen),
        get_lid_presence_last_seen=MagicMock(return_value=None),
        get_presence_last_seen=MagicMock(return_value=presence_at),
        last_answer_at=None,
    )


class TestPresenceBucketEvidence:
    def test_another_watcher_reporting_convicts_a_silent_feed(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _presence_state(_clocks(0, -20 * 3600).wall, _clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed.notified
        assert "another watcher" in " ".join(str(a) for a in box.call_args[0]).lower()

    def test_a_bucket_still_reporting_right_now_convicts(self, monkeypatch) -> None:
        """A presence bucket is a running commentary, not an arrival: its newest
        event is always near now, so waiting stale_after from *it* would wait
        forever. The feed's debt is measured against the commentary instead."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _presence_state(_clocks(0, -20 * 3600).wall, _clocks(0, -30).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed.notified
        assert box.call_count == 1

    def test_a_bucket_barely_ahead_of_the_feed_waits(self, monkeypatch) -> None:
        """Activity a couple of minutes past the feed's last word is the feed
        being a little behind, not the feed being dead."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _presence_state(_clocks(0, -3600).wall, _clocks(0, -3480).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert not feed.notified
        box.assert_not_called()

    def test_reports_older_than_the_feeds_last_word_prove_nothing(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _presence_state(_clocks(0, -3600).wall, _clocks(0, -2 * 3600).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed == main._FeedState.disarmed()
        box.assert_not_called()


class TestPresenceBucketDefaults:
    def test_no_presence_buckets_by_default(self) -> None:
        """Measured against a real overnight bucket: aw-watcher-web-chrome emitted
        294 events between 22:10 and 05:29 with the user asleep — a 90-second
        heartbeat on the active tab, same title throughout, and no window event
        beside any of them. A 90-second heartbeat beats any feed_stale window, so
        shipping that bucket enabled by default would convict a healthy feed and
        raise a blocking dialog in the middle of the night. The list is the user's
        to write; the default asserts nothing about their watchers."""
        from aw_watcher_afk_prompt.config import DEFAULT_CONFIG

        assert "presence_buckets = []" in DEFAULT_CONFIG

    def test_a_heartbeating_bucket_is_why(self, monkeypatch) -> None:
        """What such a bucket does once it *is* configured: it convicts. That is
        the deal the config comment spells out, pinned here so it cannot be
        mistaken for an accident."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        # asleep for hours; the feed rightly quiet, the browser heartbeating
        state = _presence_state(_clocks(0, -7 * 3600).wall, _clocks(0, -90).wall)
        main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert box.call_count == 1


class TestPresenceGraceOnAnEmptyFeed:
    def test_an_empty_feed_still_gets_its_grace(self, monkeypatch) -> None:
        """With no afk events at all, a presence report used to convict on the
        first poll — no wait — while every other anchor waits feed_stale."""
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _presence_state(None, _clocks(0, -60).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert not feed.notified
        box.assert_not_called()

    def test_an_empty_feed_convicts_once_the_grace_is_up(self, monkeypatch) -> None:
        box = MagicMock()
        monkeypatch.setattr(main.messagebox, "showwarning", box)
        state = _presence_state(None, _clocks(0, -(STALE_AFTER * 60 + 60)).wall)
        feed = main._check_afk_feed(state, STALE_AFTER, main._FeedState.disarmed(), now=_clocks(0).wall)
        assert feed.notified


class TestCheckCost:
    """This runs on every poll, throughout every absence."""

    def test_a_notified_stall_stops_querying(self) -> None:
        """Once said, nothing more can be learned by asking again — and a dead
        feed means asking forever, every poll."""
        state = _lid_state(_clocks(0, -20 * 3600).wall, _clocks(0, -3600).wall)
        notified = main._FeedState(None, "", notified=True)
        main._check_afk_feed(state, STALE_AFTER, notified, now=_clocks(0).wall)
        state.get_lid_presence_last_seen.assert_not_called()
        state.get_presence_last_seen.assert_not_called()


class TestBucketDiscovery:
    def test_a_bucket_registered_after_startup_is_found(self) -> None:
        """The bucket list is cached at construction, so a browser started after
        this watcher would otherwise never be seen for the process lifetime."""
        client = _bucket_client(["aw-watcher-web"], {})
        assert client.get_presence_last_seen() is None
        now = datetime.datetime.now(datetime.UTC)
        client.client.get_buckets.return_value = {"aw-watcher-web-firefox_test": {}}
        client.client.get_events.side_effect = lambda bucket_id, limit=100, start=None, end=None: [  # noqa: ARG005
            aw_core.Event(timestamp=now - datetime.timedelta(seconds=600), duration=datetime.timedelta(0), data={})
        ]
        assert client.get_presence_last_seen() is not None
