"""Tests for the __main__ orchestration helpers (event ordering / queue info / dispatch)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import aw_core

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
    )


class TestProcessEvents:
    def test_prompts_in_chronological_order(self, monkeypatch) -> None:
        """Events handed in out of order must be prompted oldest-first."""
        seen_timestamps: list[datetime] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0):  # noqa: ARG001
            seen_timestamps.append(event.timestamp)
            return None  # cancelled — keeps the test focused on ordering

        monkeypatch.setattr(main, "prompt", fake_prompt)

        events = [_event(30), _event(10), _event(20)]
        main._process_events(_fake_state(), events, context="Test")

        assert seen_timestamps == sorted(seen_timestamps)
        assert [t.minute for t in seen_timestamps] == [10, 20, 30]

    def test_queue_info_reflects_position_and_total(self, monkeypatch) -> None:
        """Each prompt should report '(N of total)' so the user knows more are pending."""
        positions: list[tuple[int, int]] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0):  # noqa: ARG001
            assert queue_info is not None
            positions.append((queue_info["position"], queue_info["total"]))
            return None

        monkeypatch.setattr(main, "prompt", fake_prompt)

        events = [_event(30), _event(10), _event(20)]
        main._process_events(_fake_state(), events, context="Test")

        assert positions == [(1, 3), (2, 3), (3, 3)]

    def test_single_event_has_no_queue_info(self, monkeypatch) -> None:
        """A lone period should not advertise a queue."""
        captured: list = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0):  # noqa: ARG001
            captured.append(queue_info)
            return None

        monkeypatch.setattr(main, "prompt", fake_prompt)

        main._process_events(_fake_state(), [_event(10)], context="Test")

        assert captured == [None]

    def test_dispatch_normal_split_and_cancel(self, monkeypatch) -> None:
        """String -> post_event, SPLIT_MODE tuple -> post_split_events, None -> neither."""
        events = [_event(10), _event(20), _event(30)]
        responses = {10: "reading", 20: ("SPLIT_MODE", ["a", "b"]), 30: None}

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0):  # noqa: ARG001
            return responses[event.timestamp.minute]

        monkeypatch.setattr(main, "prompt", fake_prompt)

        state = _fake_state()
        main._process_events(state, events, context="Test")

        assert state.post_event.call_count == 1
        assert state.post_event.call_args.args[1] == "reading"
        assert state.post_split_events.call_count == 1
        assert state.post_split_events.call_args.args[1] == ["a", "b"]

    def test_stale_minutes_threads_through_to_prompt(self, monkeypatch) -> None:
        """_process_events must pass its stale_minutes down to each prompt."""
        captured: list[float] = []

        def fake_prompt(event, recent_events, queue_info=None, stale_minutes=15.0):  # noqa: ARG001
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
