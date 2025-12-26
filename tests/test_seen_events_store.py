"""Tests for the SeenEventsStore persistent storage."""

import datetime
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import aw_core
import pytest

from aw_watcher_ask_away.core import SeenEventsStore


@pytest.fixture
def temp_config_dir(tmp_path):
    """Provide a temporary config directory."""
    config_dir = tmp_path / "aw-watcher-ask-away"
    config_dir.mkdir(parents=True, exist_ok=True)
    with patch("appdirs.user_config_dir", return_value=str(config_dir)):
        yield config_dir


def make_event(timestamp: datetime.datetime, duration_seconds: float) -> aw_core.Event:
    """Create a test event."""
    return aw_core.Event(
        timestamp=timestamp,
        duration=datetime.timedelta(seconds=duration_seconds),
        data={"status": "afk"}
    )


class TestSeenEventsStore:
    def test_add_and_has_overlap(self, temp_config_dir):
        """Test adding an event and checking for overlap."""
        store = SeenEventsStore()

        now = datetime.datetime.now(datetime.UTC)
        event = make_event(now, 300)  # 5 minute event

        # Initially no overlap
        assert not store.has_overlap(event)

        # Add the event
        store.add(event)

        # Now should have overlap
        assert store.has_overlap(event)

    def test_overlap_with_similar_event(self, temp_config_dir):
        """Test that similar (95%+ overlap) events are detected."""
        store = SeenEventsStore()

        now = datetime.datetime.now(datetime.UTC)
        event1 = make_event(now, 300)  # 5 minutes

        store.add(event1)

        # Slightly different event (same start, slightly different duration)
        event2 = make_event(now, 305)  # 5 min 5 sec
        assert store.has_overlap(event2)  # Should still detect as overlap (>95%)

        # Event with same duration but offset by 10 seconds
        event3 = make_event(now + datetime.timedelta(seconds=10), 300)
        assert store.has_overlap(event3)  # Still >95% overlap

    def test_no_overlap_with_different_event(self, temp_config_dir):
        """Test that non-overlapping events are not detected."""
        store = SeenEventsStore()

        now = datetime.datetime.now(datetime.UTC)
        event1 = make_event(now, 300)

        store.add(event1)

        # Event 10 minutes later - no overlap
        event2 = make_event(now + datetime.timedelta(minutes=10), 300)
        assert not store.has_overlap(event2)

    def test_persistence(self, temp_config_dir):
        """Test that events persist across store instances."""
        now = datetime.datetime.now(datetime.UTC)
        event = make_event(now, 300)

        # Add event in first store instance
        store1 = SeenEventsStore()
        store1.add(event)

        # Create new store instance - should load from file
        store2 = SeenEventsStore()
        assert store2.has_overlap(event)

    def test_old_events_cleaned_up(self, temp_config_dir):
        """Test that events older than max_age_days are cleaned up."""
        # Create an old event (10 days ago)
        old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)
        old_event = make_event(old_time, 300)

        # Manually write to the store file
        store_file = temp_config_dir / "seen_events.json"
        store_file.write_text(json.dumps({
            old_time.isoformat(): {
                "timestamp": old_time.isoformat(),
                "duration": 300
            }
        }))

        # Create store with 7 day max age - old event should be cleaned up
        store = SeenEventsStore(max_age_days=7)
        assert not store.has_overlap(old_event)

    def test_recent_events_preserved(self, temp_config_dir):
        """Test that recent events within max_age_days are preserved."""
        # Create a recent event (3 days ago)
        recent_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=3)
        recent_event = make_event(recent_time, 300)

        # Manually write to the store file
        store_file = temp_config_dir / "seen_events.json"
        store_file.write_text(json.dumps({
            recent_time.isoformat(): {
                "timestamp": recent_time.isoformat(),
                "duration": 300
            }
        }))

        # Create store with 7 day max age - recent event should be preserved
        store = SeenEventsStore(max_age_days=7)
        assert store.has_overlap(recent_event)

    def test_handles_corrupted_file(self, temp_config_dir):
        """Test that corrupted JSON file is handled gracefully."""
        store_file = temp_config_dir / "seen_events.json"
        store_file.write_text("not valid json {{{")

        # Should not raise, just start with empty store
        store = SeenEventsStore()
        assert not store.has_overlap(make_event(datetime.datetime.now(datetime.UTC), 300))

    def test_handles_missing_file(self, temp_config_dir):
        """Test that missing file is handled gracefully."""
        # Don't create any file - store should start empty
        store = SeenEventsStore()
        assert not store.has_overlap(make_event(datetime.datetime.now(datetime.UTC), 300))
