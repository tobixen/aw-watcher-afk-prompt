"""Integration tests for posting split events to ActivityWatch."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import aw_core
import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError

from aw_watcher_afk_prompt.core import AWAfkPromptClient
from aw_watcher_afk_prompt.split_dialog import ActivityLine


def test_post_split_events_creates_multiple_events() -> None:
    """Test that post_split_events creates multiple events with split metadata."""
    # Create mock client
    mock_client = Mock()
    mock_client.client_hostname = "test_host"
    mock_client.get_buckets.return_value = {
        "aw-watcher-afk_test_host": {"type": "afkstatus"},
        "aw-watcher-afk-prompt_test_host": {"type": "afktask"},
    }
    mock_client.get_events.return_value = []
    mock_client.insert_event = Mock()

    # Create client wrapper
    client = AWAfkPromptClient(mock_client, enable_lid_events=False)

    # Create test AFK event
    original_start = datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
    original_event = aw_core.Event(
        timestamp=original_start,
        duration=timedelta(minutes=30),
        data={"status": "afk"},
    )

    # Create split activities
    activities = [
        ActivityLine("lunch", original_start, 15, 0),
        ActivityLine("phone", original_start + timedelta(minutes=15), 15, 0),
    ]

    # Post split events
    client.post_split_events(original_event, activities)

    # Verify insert_event was called twice
    assert mock_client.insert_event.call_count == 2

    # Verify first call has correct metadata
    first_call = mock_client.insert_event.call_args_list[0]
    first_event = first_call[0][1]  # Second argument to insert_event
    assert first_event.data["message"] == "lunch"
    assert first_event.data["split"] is True
    assert first_event.data["split_count"] == 2
    assert first_event.data["split_index"] == 0
    assert "split_id" in first_event.data

    # Verify second call has correct metadata
    second_call = mock_client.insert_event.call_args_list[1]
    second_event = second_call[0][1]
    assert second_event.data["message"] == "phone"
    assert second_event.data["split"] is True
    assert second_event.data["split_count"] == 2
    assert second_event.data["split_index"] == 1
    assert "split_id" in second_event.data

    # Verify same split_id for both
    assert first_event.data["split_id"] == second_event.data["split_id"]


def test_post_split_events_preserves_timestamps() -> None:
    """Test that split events preserve the correct timestamps and durations."""
    mock_client = Mock()
    mock_client.client_hostname = "test_host"
    mock_client.get_buckets.return_value = {
        "aw-watcher-afk_test_host": {"type": "afkstatus"},
        "aw-watcher-afk-prompt_test_host": {"type": "afktask"},
    }
    mock_client.get_events.return_value = []
    mock_client.insert_event = Mock()

    client = AWAfkPromptClient(mock_client, enable_lid_events=False)

    original_start = datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
    original_event = aw_core.Event(
        timestamp=original_start,
        duration=timedelta(minutes=45, seconds=30),
        data={"status": "afk"},
    )

    activities = [
        ActivityLine("first", original_start, 10, 15),
        ActivityLine("second", original_start + timedelta(minutes=10, seconds=15), 20, 30),
        ActivityLine("third", original_start + timedelta(minutes=30, seconds=45), 14, 45),
    ]

    client.post_split_events(original_event, activities)

    # Verify correct number of calls
    assert mock_client.insert_event.call_count == 3

    # Check timestamps and durations
    calls = mock_client.insert_event.call_args_list

    # First activity: starts at original_start, duration 10m 15s
    first_event = calls[0][0][1]
    assert first_event.timestamp == original_start
    assert first_event.duration == timedelta(minutes=10, seconds=15)

    # Second activity: starts 10m 15s later, duration 20m 30s
    second_event = calls[1][0][1]
    assert second_event.timestamp == original_start + timedelta(minutes=10, seconds=15)
    assert second_event.duration == timedelta(minutes=20, seconds=30)

    # Third activity: starts 30m 45s later, duration 14m 45s
    third_event = calls[2][0][1]
    assert third_event.timestamp == original_start + timedelta(minutes=30, seconds=45)
    assert third_event.duration == timedelta(minutes=14, seconds=45)


def test_post_split_events_marks_original_as_seen_on_success() -> None:
    """Test that original event is marked as seen only after all splits post successfully."""
    mock_client = Mock()
    mock_client.client_hostname = "test_host"
    mock_client.get_buckets.return_value = {
        "aw-watcher-afk_test_host": {"type": "afkstatus"},
        "aw-watcher-afk-prompt_test_host": {"type": "afktask"},
    }
    mock_client.get_events.return_value = []
    mock_client.insert_event = Mock()  # All succeed

    client = AWAfkPromptClient(mock_client, enable_lid_events=False)

    original_start = datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
    original_event = aw_core.Event(
        timestamp=original_start,
        duration=timedelta(minutes=20),
        data={"status": "afk"},
    )

    activities = [
        ActivityLine("first", original_start, 10, 0),
        ActivityLine("second", original_start + timedelta(minutes=10), 10, 0),
    ]

    # State should not have the event initially
    assert not client.state.has_event(original_event)

    # Post split events
    client.post_split_events(original_event, activities)

    # State should now have the event marked as seen
    assert client.state.has_event(original_event)


def test_post_split_events_does_not_mark_seen_on_partial_failure() -> None:
    """Test that original event is NOT marked as seen if any split event fails to post."""
    mock_client = Mock()
    mock_client.client_hostname = "test_host"
    mock_client.get_buckets.return_value = {
        "aw-watcher-afk_test_host": {"type": "afkstatus"},
        "aw-watcher-afk-prompt_test_host": {"type": "afktask"},
    }
    mock_client.get_events.return_value = []

    # Make the second insert fail
    def insert_side_effect(bucket_id, event):
        if mock_client.insert_event.call_count == 2:
            raise Exception("Network error")

    mock_client.insert_event = Mock(side_effect=insert_side_effect)

    client = AWAfkPromptClient(mock_client, enable_lid_events=False)

    original_start = datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
    original_event = aw_core.Event(
        timestamp=original_start,
        duration=timedelta(minutes=20),
        data={"status": "afk"},
    )

    activities = [
        ActivityLine("first", original_start, 10, 0),
        ActivityLine("second", original_start + timedelta(minutes=10), 10, 0),
    ]

    # State should not have the event initially
    assert not client.state.has_event(original_event)

    # Post split events (second one will fail)
    client.post_split_events(original_event, activities)

    # State should still NOT have the event marked as seen
    assert not client.state.has_event(original_event)


def test_post_split_events_split_id_based_on_timestamp() -> None:
    """Test that split_id is consistently generated from the original event timestamp."""
    mock_client = Mock()
    mock_client.client_hostname = "test_host"
    mock_client.get_buckets.return_value = {
        "aw-watcher-afk_test_host": {"type": "afkstatus"},
        "aw-watcher-afk-prompt_test_host": {"type": "afktask"},
    }
    mock_client.get_events.return_value = []
    mock_client.insert_event = Mock()

    client = AWAfkPromptClient(mock_client, enable_lid_events=False)

    # Use a specific timestamp
    original_start = datetime(2025, 1, 15, 14, 30, 45, tzinfo=UTC)
    original_event = aw_core.Event(
        timestamp=original_start,
        duration=timedelta(minutes=20),
        data={"status": "afk"},
    )

    activities = [
        ActivityLine("first", original_start, 10, 0),
        ActivityLine("second", original_start + timedelta(minutes=10), 10, 0),
    ]

    client.post_split_events(original_event, activities)

    # Get the split_id from the first posted event
    first_call = mock_client.insert_event.call_args_list[0]
    first_event = first_call[0][1]
    split_id = first_event.data["split_id"]

    # Verify split_id is the timestamp as a string
    expected_split_id = str(original_start.timestamp())
    assert split_id == expected_split_id


def test_post_split_events_with_seconds() -> None:
    """Test that split events correctly handle durations with seconds."""
    mock_client = Mock()
    mock_client.client_hostname = "test_host"
    mock_client.get_buckets.return_value = {
        "aw-watcher-afk_test_host": {"type": "afkstatus"},
        "aw-watcher-afk-prompt_test_host": {"type": "afktask"},
    }
    mock_client.get_events.return_value = []
    mock_client.insert_event = Mock()

    client = AWAfkPromptClient(mock_client, enable_lid_events=False)

    original_start = datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
    original_event = aw_core.Event(
        timestamp=original_start,
        duration=timedelta(minutes=5, seconds=37),
        data={"status": "afk"},
    )

    activities = [
        ActivityLine("first", original_start, 2, 45),
        ActivityLine("second", original_start + timedelta(minutes=2, seconds=45), 2, 52),
    ]

    client.post_split_events(original_event, activities)

    # Verify durations include seconds
    first_event = mock_client.insert_event.call_args_list[0][0][1]
    assert first_event.duration == timedelta(minutes=2, seconds=45)

    second_event = mock_client.insert_event.call_args_list[1][0][1]
    assert second_event.duration == timedelta(minutes=2, seconds=52)


def _make_client() -> tuple[AWAfkPromptClient, Mock]:
    """Helper: return (AWAfkPromptClient, mock_aw_client)."""
    mock_client = Mock()
    mock_client.client_hostname = "test_host"
    mock_client.get_buckets.return_value = {
        "aw-watcher-afk_test_host": {"type": "afkstatus"},
        "aw-watcher-afk-prompt_test_host": {"type": "afktask"},
    }
    mock_client.get_events.return_value = []
    mock_client.insert_event = Mock()
    return AWAfkPromptClient(mock_client, enable_lid_events=False), mock_client


def _original_event(start: datetime | None = None) -> aw_core.Event:
    start = start or datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
    return aw_core.Event(timestamp=start, duration=timedelta(minutes=20), data={"status": "afk"})


def _activities(start: datetime | None = None) -> list:
    start = start or datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
    return [
        ActivityLine("first", start, 10, 0),
        ActivityLine("second", start + timedelta(minutes=10), 10, 0),
    ]


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


def test_post_split_events_retries_on_connection_error_then_succeeds() -> None:
    """A transient ConnectionError on one activity is retried; all posted → marked seen."""
    from aw_watcher_afk_prompt.core import _POST_RETRY_INTERVAL

    client, mock_client = _make_client()
    original = _original_event()

    call_count = 0

    def insert_side_effect(bucket_id, event):
        nonlocal call_count
        call_count += 1
        # Fail the first attempt for the second activity, succeed on retry
        if call_count == 2:
            raise RequestsConnectionError("temporary network error")

    mock_client.insert_event.side_effect = insert_side_effect

    with patch("aw_watcher_afk_prompt.core.time") as mock_time:
        client.post_split_events(original, _activities())

    # Should have retried: 1 success (first), 1 fail + 1 retry success (second) = 3 calls
    assert mock_client.insert_event.call_count == 3
    # sleep called once with the fixed retry interval
    mock_time.sleep.assert_called_once_with(_POST_RETRY_INTERVAL)
    # Fully successful → marked as seen
    assert client.state.has_event(original)


def test_post_split_events_retries_on_http_5xx_then_succeeds() -> None:
    """A transient 5xx HTTPError is retried; success on retry → marked seen."""
    client, mock_client = _make_client()
    original = _original_event()

    call_count = 0

    def insert_side_effect(bucket_id, event):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            resp = Mock()
            resp.status_code = 503
            raise HTTPError(response=resp)

    mock_client.insert_event.side_effect = insert_side_effect

    with patch("aw_watcher_afk_prompt.core.time") as mock_time:
        client.post_split_events(original, _activities())

    assert mock_client.insert_event.call_count == 3  # 1 fail + retry + 1 success
    assert mock_time.sleep.call_count >= 1
    assert client.state.has_event(original)


def test_post_split_events_gives_up_after_max_retries() -> None:
    """After exhausting retries on ConnectionError, event is NOT marked as seen."""
    client, mock_client = _make_client()
    original = _original_event()

    mock_client.insert_event.side_effect = RequestsConnectionError("server down")

    with patch("aw_watcher_afk_prompt.core.time"):
        client.post_split_events(original, _activities())

    assert not client.state.has_event(original)


def test_post_split_events_does_not_retry_on_permanent_error() -> None:
    """A non-transient exception (e.g. ValueError) is not retried."""
    client, mock_client = _make_client()
    original = _original_event()

    mock_client.insert_event.side_effect = ValueError("bad data")

    with patch("aw_watcher_afk_prompt.core.time") as mock_time:
        client.post_split_events(original, _activities())

    # No retries: called exactly once per activity (both fail immediately)
    assert mock_client.insert_event.call_count == 2
    mock_time.sleep.assert_not_called()
    assert not client.state.has_event(original)


def test_post_event_retries_on_connection_error_then_succeeds() -> None:
    """post_event retries on transient ConnectionError and marks seen on success."""
    client, mock_client = _make_client()
    original = _original_event()

    call_count = 0

    def insert_side_effect(bucket_id, event):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RequestsConnectionError("temporary")

    mock_client.insert_event.side_effect = insert_side_effect

    with patch("aw_watcher_afk_prompt.core.time"):
        client.post_event(original, "lunch")

    assert mock_client.insert_event.call_count == 2
    assert client.state.has_event(original)


def test_post_event_raises_after_exhausting_retries() -> None:
    """post_event raises after exhausting retries (caller handles re-prompt)."""
    client, mock_client = _make_client()
    original = _original_event()

    mock_client.insert_event.side_effect = RequestsConnectionError("server down")

    with patch("aw_watcher_afk_prompt.core.time"), pytest.raises(RequestsConnectionError):
        client.post_event(original, "lunch")

    assert not client.state.has_event(original)
