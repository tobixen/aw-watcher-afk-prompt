"""Tests for the __main__ orchestration helpers (event ordering / queue info / dispatch)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import aw_core

import aw_watcher_afk_prompt.__main__ as main


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

        def fake_prompt(event, recent_events, queue_info=None):  # noqa: ARG001
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

        def fake_prompt(event, recent_events, queue_info=None):  # noqa: ARG001
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

        def fake_prompt(event, recent_events, queue_info=None):  # noqa: ARG001
            captured.append(queue_info)
            return None

        monkeypatch.setattr(main, "prompt", fake_prompt)

        main._process_events(_fake_state(), [_event(10)], context="Test")

        assert captured == [None]

    def test_dispatch_normal_split_and_cancel(self, monkeypatch) -> None:
        """String -> post_event, SPLIT_MODE tuple -> post_split_events, None -> neither."""
        events = [_event(10), _event(20), _event(30)]
        responses = {10: "reading", 20: ("SPLIT_MODE", ["a", "b"]), 30: None}

        def fake_prompt(event, recent_events, queue_info=None):  # noqa: ARG001
            return responses[event.timestamp.minute]

        monkeypatch.setattr(main, "prompt", fake_prompt)

        state = _fake_state()
        main._process_events(state, events, context="Test")

        assert state.post_event.call_count == 1
        assert state.post_event.call_args.args[1] == "reading"
        assert state.post_split_events.call_count == 1
        assert state.post_split_events.call_args.args[1] == ["a", "b"]

    def test_empty_is_noop(self, monkeypatch) -> None:
        called = False

        def fake_prompt(*a, **k):  # noqa: ARG001
            nonlocal called
            called = True

        monkeypatch.setattr(main, "prompt", fake_prompt)
        main._process_events(_fake_state(), [], context="Test")
        assert called is False
