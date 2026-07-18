"""Tests for the __main__ orchestration helpers (event ordering / queue info / dispatch)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import aw_core
import pytest

import aw_watcher_afk_prompt.__main__ as main
from aw_watcher_afk_prompt.utils import WARNING_SYMBOL


def _event(minute: int, duration_min: int = 6) -> aw_core.Event:
    """Build an AFK gap event starting at a fixed day + given minute."""
    ts = datetime(2026, 6, 7, 12, minute, 0, tzinfo=UTC)
    return aw_core.Event(id=None, timestamp=ts, duration=timedelta(minutes=duration_min))


def _fake_state() -> SimpleNamespace:
    """A stand-in for AWAfkPromptClient exposing the bits _process_events touches."""
    return SimpleNamespace(
        state=SimpleNamespace(recent_events=[]),
        post_event=MagicMock(),
        post_split_events=MagicMock(),
        get_afk_period_end=MagicMock(return_value=None),
    )


class TestProcessEvents:
    def test_prompts_in_chronological_order(self, monkeypatch) -> None:
        """Events handed in out of order must be prompted oldest-first."""
        seen_timestamps: list[datetime] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            seen_timestamps.append(event.timestamp)
            return "x"  # answered — a None (snooze) would stop the queue

        monkeypatch.setattr(main, "prompt", fake_prompt)

        events = [_event(30), _event(10), _event(20)]
        main._process_events(_fake_state(), events, context="Test")

        assert seen_timestamps == sorted(seen_timestamps)
        assert [t.minute for t in seen_timestamps] == [10, 20, 30]

    def test_queue_info_reflects_position_and_total(self, monkeypatch) -> None:
        """Each prompt should report '(N of total)' so the user knows more are pending."""
        positions: list[tuple[int, int]] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            assert queue_info is not None
            positions.append((queue_info["position"], queue_info["total"]))
            return "x"

        monkeypatch.setattr(main, "prompt", fake_prompt)

        events = [_event(30), _event(10), _event(20)]
        main._process_events(_fake_state(), events, context="Test")

        assert positions == [(1, 3), (2, 3), (3, 3)]

    def test_single_event_has_no_queue_info(self, monkeypatch) -> None:
        """A lone period should not advertise a queue."""
        captured: list = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            captured.append(queue_info)
            return "x"

        monkeypatch.setattr(main, "prompt", fake_prompt)

        main._process_events(_fake_state(), [_event(10)], context="Test")

        assert captured == [None]

    def test_dispatch_normal_split_and_snooze(self, monkeypatch) -> None:
        """String -> post_event, SPLIT_MODE tuple -> post_split_events, None -> neither."""
        events = [_event(10), _event(20), _event(30)]
        responses = {10: "reading", 20: ("SPLIT_MODE", ["a", "b"]), 30: None}

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            return responses[event.timestamp.minute]

        monkeypatch.setattr(main, "prompt", fake_prompt)

        state = _fake_state()
        snoozed = main._process_events(state, events, context="Test")

        assert snoozed is True  # last prompt returned None
        assert state.post_event.call_count == 1
        assert state.post_event.call_args.args[1] == "reading"
        assert state.post_split_events.call_count == 1
        assert state.post_split_events.call_args.args[1] == ["a", "b"]

    def test_snooze_stops_the_queue(self, monkeypatch) -> None:
        """A snoozed (None) prompt must stop the queue — the user asked to be left alone.

        The remaining periods are not lost: they are unanswered and will be
        re-found by the next deep scan once the snooze suppression expires.
        """
        prompted: list[int] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            prompted.append(event.timestamp.minute)
            return None  # snooze on the very first prompt

        monkeypatch.setattr(main, "prompt", fake_prompt)

        snoozed = main._process_events(_fake_state(), [_event(10), _event(20), _event(30)], context="Test")

        assert snoozed is True
        assert prompted == [10], "no further prompts after a snooze"

    def test_all_answered_returns_false(self, monkeypatch) -> None:
        monkeypatch.setattr(main, "prompt", lambda *a, **k: "x")
        assert main._process_events(_fake_state(), [_event(10), _event(20)], context="Test") is False

    def test_queue_extends_when_rescan_finds_new_period(self, monkeypatch) -> None:
        """A period that completes while a dialog sits unanswered must join the
        same queue run (via rescan) instead of surfacing minutes later as a
        stand-alone surprise prompt with no indication."""
        rescans = iter([[_event(20)], []])
        prompted: list[tuple[int, dict | None]] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            prompted.append((event.timestamp.minute, queue_info))
            return "x"

        monkeypatch.setattr(main, "prompt", fake_prompt)

        main._process_events(_fake_state(), [_event(10)], context="Test", rescan=lambda: next(rescans))

        assert [minute for minute, _ in prompted] == [10, 20]
        # The late-arriving period announces its position in the (now known) queue.
        assert prompted[1][1] == {"position": 2, "total": 2, "next_str": None}

    def test_ongoing_period_counted_in_total(self, monkeypatch) -> None:
        """A lone completed gap must not pretend to be the only open interval
        when the current AFK period is still running and will need an answer too."""
        captured: list[dict | None] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            captured.append(queue_info)
            return "x"

        monkeypatch.setattr(main, "prompt", fake_prompt)

        main._process_events(_fake_state(), [_event(10)], context="Test", ongoing_check=lambda: True)

        assert captured == [{"position": 1, "total": 2, "next_str": "the current AFK period (still ongoing)"}]

    def test_ongoing_period_is_last_in_next_hints(self, monkeypatch) -> None:
        """With several completed gaps AND an ongoing period, the next-hint walks
        through the completed gaps first and announces the ongoing period last."""
        captured: list[dict | None] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            captured.append(queue_info)
            return "x"

        monkeypatch.setattr(main, "prompt", fake_prompt)

        main._process_events(_fake_state(), [_event(10), _event(20)], context="Test", ongoing_check=lambda: True)

        assert [(qi["position"], qi["total"]) for qi in captured] == [(1, 3), (2, 3)]
        next_gap_start = main.format_time_local(_event(20).timestamp)
        assert next_gap_start in captured[0]["next_str"], "first prompt points at the next completed gap"
        assert captured[1]["next_str"] == "the current AFK period (still ongoing)"

    def test_rescan_failure_keeps_current_queue(self, monkeypatch) -> None:
        """A server hiccup during the queue refresh must not lose the remaining queue."""
        prompted: list[int] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            prompted.append(event.timestamp.minute)
            return "x"

        def failing_rescan():
            raise main.ConnectionError("server down")

        monkeypatch.setattr(main, "prompt", fake_prompt)

        snoozed = main._process_events(_fake_state(), [_event(10), _event(20)], context="Test", rescan=failing_rescan)

        assert snoozed is False
        assert prompted == [10, 20]

    def test_no_rescan_after_snooze(self, monkeypatch) -> None:
        """A snooze stops the queue immediately — no pointless refresh afterwards."""
        rescan = MagicMock()
        monkeypatch.setattr(main, "prompt", lambda *a, **k: None)
        main._process_events(_fake_state(), [_event(10)], context="Test", rescan=rescan)
        rescan.assert_not_called()

    def test_stale_minutes_threads_through_to_prompt(self, monkeypatch) -> None:
        """_process_events must pass its stale_minutes down to each prompt."""
        captured: list[float] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0, **kwargs):  # noqa: ARG001
            captured.append(stale_minutes)
            return None

        monkeypatch.setattr(main, "prompt", fake_prompt)
        main._process_events(_fake_state(), [_event(10)], context="Test", stale_minutes=42.0)
        assert captured == [42.0]

    def test_empty_is_noop(self, monkeypatch) -> None:
        called = False

        def fake_prompt(*a, **k):  # noqa: ARG001
            nonlocal called
            called = True

        monkeypatch.setattr(main, "prompt", fake_prompt)
        main._process_events(_fake_state(), [], context="Test")
        assert called is False


class TestPostOngoingResponse:
    """Dispatch of the live 'still AFK' dialog result.

    The ongoing dialog now offers a Split button (the AFK period is assumed to
    end the moment the user types/clicks), so its result may be a SPLIT_MODE
    tuple, a plain description, or None — and each must route like the batched
    path in _process_events.
    """

    def _ongoing(self) -> aw_core.Event:
        ts = datetime.now(UTC) - timedelta(minutes=30)
        return aw_core.Event(id=None, timestamp=ts, duration=timedelta(0))

    def test_none_posts_nothing(self) -> None:
        state = _fake_state()
        main._post_ongoing_response(state, self._ongoing(), None)
        state.post_event.assert_not_called()
        state.post_split_events.assert_not_called()

    def test_plain_string_posts_single_event_with_snapshot_duration(self) -> None:
        state = _fake_state()
        ongoing = self._ongoing()
        main._post_ongoing_response(state, ongoing, "writing code")
        state.post_split_events.assert_not_called()
        state.post_event.assert_called_once()
        posted_event, message = state.post_event.call_args.args
        assert message == "writing code"
        # The period is assumed over now, so duration is start..now (~30 min), not 0.
        assert posted_event.timestamp == ongoing.timestamp
        assert posted_event.duration.total_seconds() > 60

    def test_plain_string_uses_actual_return_time_when_known(self) -> None:
        """The posted duration must end when the afk watcher saw the user return,
        not when they got around to clicking OK (possibly minutes later)."""
        state = _fake_state()
        ongoing = self._ongoing()
        returned_at = ongoing.timestamp + timedelta(minutes=20)
        state.get_afk_period_end = MagicMock(return_value=returned_at)

        main._post_ongoing_response(state, ongoing, "writing code")

        posted_event, _ = state.post_event.call_args.args
        assert posted_event.timestamp == ongoing.timestamp
        assert posted_event.duration == timedelta(minutes=20)

    def test_split_mode_routes_to_post_split_events(self) -> None:
        state = _fake_state()
        ongoing = self._ongoing()
        activities = [object(), object()]
        main._post_ongoing_response(state, ongoing, ("SPLIT_MODE", activities))
        state.post_event.assert_not_called()
        state.post_split_events.assert_called_once_with(ongoing, activities)


class TestPromptOngoing:
    """The live 'still AFK' dialog must be able to notice the user returned."""

    def test_forwards_still_afk_check_and_marks_ongoing(self, monkeypatch) -> None:
        captured: dict = {}

        def fake_ask_string(title, prompt_text, history, **kwargs):  # noqa: ARG001
            captured.update(kwargs)
            return None

        monkeypatch.setattr(main.aw_dialog, "ask_string", fake_ask_string)

        sentinel = object()
        ongoing = aw_core.Event(id=None, timestamp=datetime.now(UTC) - timedelta(minutes=5), duration=timedelta(0))
        main.prompt_ongoing(ongoing, [], still_afk_check=sentinel)

        assert captured["is_ongoing"] is True
        assert captured["still_afk_check"] is sentinel
        assert captured["afk_duration_seconds"] is None


class TestHandleStillAfk:
    """When the shallow scan finds nothing (user still AFK), earlier *completed*
    unfilled periods must be asked about oldest-first, before the live 'still
    AFK' dialog for the just-started period — so the user always answers the
    oldest interval first, not the most recent one."""

    def _args(self, *, backfill: bool = True) -> SimpleNamespace:
        return SimpleNamespace(backfill=backfill, length=5.0, stale_warning=15.0, min_active=0.0)

    def _ongoing_event(self, minute: int = 57) -> aw_core.Event:
        ts = datetime(2026, 6, 7, 11, minute, 0, tzinfo=UTC)
        return aw_core.Event(id=None, timestamp=ts, duration=timedelta(0))

    def _state_with_ongoing(self, ongoing) -> SimpleNamespace:
        state = _fake_state()
        state.get_ongoing_afk_event = MagicMock(return_value=ongoing)
        return state

    def test_deep_scan_runs_in_while_afk_mode(self, monkeypatch) -> None:
        """The still-AFK deep scan must ask for gaps even though the user is
        currently AFK. Without the flag, get_new_afk_events_to_note bails out on
        'currently AFK' and the pending list is ALWAYS empty — so the older
        periods were only ever prompted right after the ongoing dialog closed."""
        ongoing = self._ongoing_event()
        state = self._state_with_ongoing(ongoing)
        captured: dict = {}

        def fake_deep_scan(s, a, while_afk=False):  # noqa: ARG001
            captured["while_afk"] = while_afk
            return []

        monkeypatch.setattr(main, "_deep_scan", fake_deep_scan)
        monkeypatch.setattr(main, "prompt_ongoing", lambda *a, **k: None)
        monkeypatch.setattr(main, "_post_ongoing_response", lambda *a, **k: None)

        main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)

        assert captured["while_afk"] is True

    def test_deep_scan_forwards_while_afk_flag(self) -> None:
        """_deep_scan(while_afk=True) must translate into include_while_afk=True
        on the core scan call."""
        state = SimpleNamespace(get_new_afk_events_to_note=MagicMock(return_value=iter([])))
        args = SimpleNamespace(backfill_depth=60.0, length=5.0, min_active=0.0)

        main._deep_scan(state, args, while_afk=True)

        kwargs = state.get_new_afk_events_to_note.call_args.kwargs
        assert kwargs.get("include_while_afk") is True

        main._deep_scan(state, args)
        kwargs = state.get_new_afk_events_to_note.call_args.kwargs
        assert not kwargs.get("include_while_afk")

    def test_completed_periods_prompted_before_ongoing(self, monkeypatch) -> None:
        ongoing = self._ongoing_event()
        state = self._state_with_ongoing(ongoing)
        pending = [_event(10), _event(20)]
        deep_scan_calls: list[bool] = []

        def fake_deep_scan(s, a, while_afk=False):  # noqa: ARG001
            deep_scan_calls.append(while_afk)
            return pending

        monkeypatch.setattr(main, "_deep_scan", fake_deep_scan)
        processed: dict = {}

        def fake_process(s, events, *, context, stale_minutes=15.0, rescan=None, ongoing_check=None, **kwargs):  # noqa: ARG001
            processed["events"] = events
            processed["rescan"] = rescan
            processed["ongoing_check"] = ongoing_check
            return False

        monkeypatch.setattr(main, "_process_events", fake_process)
        ongoing_shown: list = []
        monkeypatch.setattr(main, "prompt_ongoing", lambda *a, **k: ongoing_shown.append(True))
        monkeypatch.setattr(main, "_post_ongoing_response", lambda *a, **k: None)

        result = main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)

        assert processed["events"] == pending, "earlier completed periods are prompted"
        assert ongoing_shown == [], "live dialog NOT shown while earlier periods are pending"
        # The queue must be able to refresh mid-run (still in while-afk mode) and
        # must know the ongoing period exists, so its count stays truthful.
        assert processed["rescan"]() == pending
        assert deep_scan_calls == [True, True], "rescan repeats the while-afk deep scan"
        assert processed["ongoing_check"]() is True
        # Leave prompted_ongoing_start untouched so the live dialog still appears
        # once the earlier periods have been cleared.
        assert result.prompted_ongoing_start is None
        assert result.snoozed is False
        assert result.deep_scan == "now", "we just ran a fresh deep scan"

    def test_ongoing_shown_when_no_completed_pending(self, monkeypatch) -> None:
        ongoing = self._ongoing_event()
        state = self._state_with_ongoing(ongoing)
        monkeypatch.setattr(main, "_deep_scan", lambda s, a, **k: [])  # noqa: ARG005
        monkeypatch.setattr(main, "_process_events", lambda *a, **k: pytest.fail("should not prompt completed periods"))
        shown: dict = {}

        def fake_ongoing(event, recent_events, still_afk_check=None, **kwargs):  # noqa: ARG001
            shown["event"] = event
            return "answer"

        monkeypatch.setattr(main, "prompt_ongoing", fake_ongoing)
        posted: dict = {}
        monkeypatch.setattr(
            main,
            "_post_ongoing_response",
            lambda s, o, r, min_active=0.0: posted.update(r=r),  # noqa: ARG005
        )

        result = main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)

        assert shown["event"] is ongoing
        assert posted["r"] == "answer"
        assert result.prompted_ongoing_start == ongoing.timestamp
        assert result.snoozed is False
        assert result.deep_scan == "reset", "force a deep scan next loop after the live dialog closes"

    def test_noop_when_not_afk(self, monkeypatch) -> None:
        state = self._state_with_ongoing(None)
        monkeypatch.setattr(main, "_deep_scan", lambda *a: pytest.fail("no scan when not AFK"))
        result = main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)
        assert result.prompted_ongoing_start is None
        assert result.snoozed is False
        assert result.deep_scan == "keep"

    def test_noop_when_ongoing_already_prompted(self, monkeypatch) -> None:
        ongoing = self._ongoing_event()
        state = self._state_with_ongoing(ongoing)
        scanned: list = []
        monkeypatch.setattr(main, "_deep_scan", lambda *a: scanned.append("scan") or [])
        result = main._handle_still_afk(state, self._args(), prompted_ongoing_start=ongoing.timestamp)
        assert result.deep_scan == "keep"
        assert scanned == [], "neither scan nor prompt for an already-shown ongoing period"

    def test_snooze_on_completed_propagates(self, monkeypatch) -> None:
        ongoing = self._ongoing_event()
        state = self._state_with_ongoing(ongoing)
        monkeypatch.setattr(main, "_deep_scan", lambda s, a, **k: [_event(10)])  # noqa: ARG005
        monkeypatch.setattr(main, "_process_events", lambda *a, **k: True)  # user snoozed
        monkeypatch.setattr(main, "prompt_ongoing", lambda *a, **k: pytest.fail("no live dialog after snooze"))
        result = main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)
        assert result.snoozed is True
        assert result.deep_scan == "now"

    def test_snooze_on_ongoing_propagates(self, monkeypatch) -> None:
        ongoing = self._ongoing_event()
        state = self._state_with_ongoing(ongoing)
        monkeypatch.setattr(main, "_deep_scan", lambda s, a, **k: [])  # noqa: ARG005
        monkeypatch.setattr(main, "prompt_ongoing", lambda *a, **k: None)  # snooze
        monkeypatch.setattr(main, "_post_ongoing_response", lambda *a, **k: None)
        result = main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)
        assert result.snoozed is True
        assert result.prompted_ongoing_start == ongoing.timestamp
        assert result.deep_scan == "reset"

    def test_no_backfill_shows_ongoing_directly(self, monkeypatch) -> None:
        ongoing = self._ongoing_event()
        state = self._state_with_ongoing(ongoing)
        monkeypatch.setattr(main, "_deep_scan", lambda *a: pytest.fail("no scan when backfill disabled"))
        shown: list = []
        monkeypatch.setattr(main, "prompt_ongoing", lambda *a, **k: shown.append(True))
        monkeypatch.setattr(main, "_post_ongoing_response", lambda *a, **k: None)
        result = main._handle_still_afk(state, self._args(backfill=False), prompted_ongoing_start=None)
        assert shown == [True]
        assert result.deep_scan == "reset"


class TestPromptStaleThreshold:
    """The warning symbol in the prompt text must respect the configured threshold."""

    def _capture_prompt_text(self, monkeypatch, *, age_min: int, stale_minutes: float) -> str:
        captured: dict[str, str] = {}

        def fake_ask_string(title, prompt_text, *args, **kwargs):  # noqa: ARG001
            captured["text"] = prompt_text
            return None

        monkeypatch.setattr(main.aw_dialog, "ask_string", fake_ask_string)

        ended = datetime.now(UTC) - timedelta(minutes=age_min)
        event = aw_core.Event(id=None, timestamp=ended - timedelta(minutes=6), duration=timedelta(minutes=6))
        main.prompt(event, [], stale_minutes=stale_minutes)
        return captured["text"]

    def test_warning_shown_when_older_than_threshold(self, monkeypatch) -> None:
        text = self._capture_prompt_text(monkeypatch, age_min=20, stale_minutes=15)
        assert WARNING_SYMBOL in text

    def test_no_warning_when_within_threshold(self, monkeypatch) -> None:
        text = self._capture_prompt_text(monkeypatch, age_min=20, stale_minutes=30)
        assert WARNING_SYMBOL not in text


class TestLiveRefresh:
    """A dialog the user hasn't answered yet must not keep claiming that the AFK
    period ended "2 minutes ago" an hour later, nor that it is the only interval
    waiting when more piled up behind it while it sat open."""

    def _refresh_from_process_events(self, monkeypatch, *, events, **kwargs):
        """Return the refresh callback the first prompt of a queue run got.

        The fake prompt snoozes (None) so the run stops after one dialog — the
        refresh callback stays valid and callable afterwards.
        """
        captured: dict = {}

        def fake_prompt(event, recent_events, refresh=None, **kw):  # noqa: ARG001
            captured["refresh"] = refresh
            return None

        monkeypatch.setattr(main, "prompt", fake_prompt)
        main._process_events(_fake_state(), events, context="Test", **kwargs)
        return captured["refresh"]

    def test_prompt_text_is_recomputed_with_the_current_age(self) -> None:
        """The age line must be regenerated, not frozen at dialog-creation time."""
        ended = datetime.now(UTC) - timedelta(minutes=90)
        event = aw_core.Event(id=None, timestamp=ended - timedelta(minutes=10), duration=timedelta(minutes=10))

        refresh = main._make_refresh(event, answered=0, stale_minutes=15.0)

        text = refresh()["prompt"]
        assert text == main._make_prompt_text(event, 15.0)
        assert WARNING_SYMBOL in text, "a 90-minute-old period is stale and must say so"

    def test_queue_is_recounted_while_the_dialog_waits(self, monkeypatch) -> None:
        """Two more periods appearing behind the open dialog must turn it into 1 of 3."""
        pending = [_event(10), _event(20), _event(30)]
        refresh = self._refresh_from_process_events(
            monkeypatch,
            events=[_event(10)],
            rescan=lambda: pending,
        )

        queue_info = refresh()["queue_info"]
        assert (queue_info["position"], queue_info["total"]) == (1, 3)

    def test_ongoing_period_counted_in_the_live_recount(self, monkeypatch) -> None:
        """Going away again while the dialog waits also adds to the total."""
        refresh = self._refresh_from_process_events(
            monkeypatch,
            events=[_event(10)],
            rescan=lambda: [_event(10)],
            ongoing_check=lambda: True,
        )

        queue_info = refresh()["queue_info"]
        assert (queue_info["position"], queue_info["total"]) == (1, 2)
        assert queue_info["next_str"] == "the current AFK period (still ongoing)"

    def test_answered_periods_keep_counting_towards_the_position(self, monkeypatch) -> None:
        """Mid-queue, the recount must not renumber the user back to '1 of N'."""
        event = _event(20)
        refresh = main._make_refresh(event, answered=2, stale_minutes=15.0, rescan=lambda: [event])

        queue_info = refresh()["queue_info"]
        assert (queue_info["position"], queue_info["total"]) == (3, 3)

    def test_current_event_is_counted_even_if_the_rescan_drops_it(self) -> None:
        """The period being prompted is unanswered by definition, so it counts."""
        event = _event(10)
        refresh = main._make_refresh(
            event, answered=0, stale_minutes=15.0, rescan=lambda: [], ongoing_check=lambda: True
        )

        queue_info = refresh()["queue_info"]
        assert (queue_info["position"], queue_info["total"]) == (1, 2)

    def test_rescan_failure_leaves_the_queue_line_alone(self) -> None:
        """A server hiccup must not blank out or fake the queue count."""

        def failing_rescan():
            raise main.ConnectionError("server down")

        refresh = main._make_refresh(_event(10), answered=0, stale_minutes=15.0, rescan=failing_rescan)

        update = refresh()
        assert "queue_info" not in update, "unknown != no queue"
        assert "prompt" in update, "the age can still be refreshed"

    def test_without_a_rescan_only_the_age_refreshes(self) -> None:
        update = main._make_refresh(_event(10), answered=0, stale_minutes=15.0)()
        assert "queue_info" not in update
        assert "prompt" in update

    def test_process_events_passes_a_refresh_to_every_prompt(self, monkeypatch) -> None:
        refreshes: list = []

        def fake_prompt(event, recent_events, refresh=None, **kw):  # noqa: ARG001
            refreshes.append(refresh)
            return "x"

        monkeypatch.setattr(main, "prompt", fake_prompt)
        main._process_events(_fake_state(), [_event(10), _event(20)], context="Test")

        assert len(refreshes) == 2
        assert all(callable(r) for r in refreshes)


class TestQueueCountSurvivesTheUserWanderingOff:
    """The usual reason a dialog sits unanswered is that nobody is at the keyboard —
    so the recount must keep working while the user is AFK. A scan that bails out
    on "currently AFK" reports an empty queue, which would blank the count in the
    dialog and silently drop the rest of the queue between prompts."""

    def _scan_state(self) -> SimpleNamespace:
        return SimpleNamespace(get_new_afk_events_to_note=MagicMock(return_value=iter([])))

    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(backfill_depth=1440.0, length=5.0, min_active=0.0)

    def test_rescan_hook_scans_in_while_afk_mode(self) -> None:
        state = self._scan_state()

        main._rescan_hook(state, self._args())()

        kwargs = state.get_new_afk_events_to_note.call_args.kwargs
        assert kwargs.get("include_while_afk") is True

    def test_ongoing_check_hook_asks_about_the_configured_length(self) -> None:
        state = _fake_state()
        state.get_ongoing_afk_event = MagicMock(return_value=None)

        assert main._ongoing_check(state, self._args())() is False
        state.get_ongoing_afk_event.assert_called_once_with(300.0)

    def test_ongoing_check_hook_reports_a_running_period(self) -> None:
        state = _fake_state()
        state.get_ongoing_afk_event = MagicMock(return_value=_event(10))

        assert main._ongoing_check(state, self._args())() is True


class TestOngoingDialogQueue:
    """The dialog on screen while you are away is the live "still AFK" one. When
    periods pile up behind it, it has to say so too — that is the "1 of 3" the
    whole live-queue exercise is about."""

    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(
            backfill=True,
            length=5.0,
            stale_warning=15.0,
            min_active=0.0,
            backfill_depth=1440.0,
        )

    def _capture_refresh(self, monkeypatch, *, others):
        ongoing = aw_core.Event(
            id=None, timestamp=datetime(2026, 6, 7, 7, 40, tzinfo=UTC), duration=timedelta(minutes=10)
        )
        state = _fake_state()
        state.get_ongoing_afk_event = MagicMock(return_value=ongoing)
        # First call (pending check) returns nothing, so the live dialog is shown;
        # later calls are the in-dialog recount, by then more periods exist.
        scans = iter([[], others])
        monkeypatch.setattr(main, "_deep_scan", lambda s, a, **k: next(scans))  # noqa: ARG005
        monkeypatch.setattr(main, "_post_ongoing_response", lambda *a, **k: None)
        captured: dict = {}

        def fake_prompt_ongoing(event, recent_events, refresh=None, **kw):  # noqa: ARG001
            captured["refresh"] = refresh
            return None

        monkeypatch.setattr(main, "prompt_ongoing", fake_prompt_ongoing)
        main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)
        return captured["refresh"]

    def test_counts_periods_that_appear_behind_it(self, monkeypatch) -> None:
        refresh = self._capture_refresh(monkeypatch, others=[_event(10), _event(20)])

        queue_info = refresh()["queue_info"]

        assert (queue_info["position"], queue_info["total"]) == (1, 3)
        assert main.format_time_local(_event(10).timestamp) in queue_info["next_str"]

    def test_no_queue_line_when_nothing_else_is_pending(self, monkeypatch) -> None:
        refresh = self._capture_refresh(monkeypatch, others=[])

        assert refresh()["queue_info"] is None

    def test_recount_failure_leaves_the_count_alone(self, monkeypatch) -> None:
        ongoing = aw_core.Event(id=None, timestamp=datetime.now(UTC), duration=timedelta(0))
        state = _fake_state()
        state.get_ongoing_afk_event = MagicMock(return_value=ongoing)
        calls = iter([[]])

        def scan(s, a, **k):  # noqa: ARG001
            try:
                return next(calls)
            except StopIteration:
                raise main.ConnectionError("server down")

        monkeypatch.setattr(main, "_deep_scan", scan)
        monkeypatch.setattr(main, "_post_ongoing_response", lambda *a, **k: None)
        captured: dict = {}

        def fake_prompt_ongoing(event, recent_events, refresh=None, **kw):  # noqa: ARG001
            captured["refresh"] = refresh
            return None

        monkeypatch.setattr(main, "prompt_ongoing", fake_prompt_ongoing)
        main._handle_still_afk(state, self._args(), prompted_ongoing_start=None)

        assert "queue_info" not in captured["refresh"]()
