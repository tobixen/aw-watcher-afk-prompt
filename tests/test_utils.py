"""Tests for utils module."""

from datetime import UTC, datetime, timedelta, timezone

from aw_watcher_afk_prompt.utils import LOCAL_TIMEZONE, format_duration, format_time_local


class TestFormatTimeLocal:
    """Tests for the format_time_local function."""

    def test_converts_utc_to_local(self) -> None:
        """Test that UTC times are converted to local timezone."""
        utc_time = datetime(2025, 1, 15, 12, 30, 0, tzinfo=UTC)
        result = format_time_local(utc_time)

        # The result should be the local time equivalent
        expected_local = utc_time.astimezone(LOCAL_TIMEZONE)
        # Check that the formatted time matches the local conversion
        assert result == expected_local.strftime("%H:%M") or \
               result == expected_local.strftime("%I:%M %p").lstrip("0")

    def test_includes_seconds_when_requested(self) -> None:
        """Test that seconds are included when include_seconds=True."""
        utc_time = datetime(2025, 1, 15, 12, 30, 45, tzinfo=UTC)
        result = format_time_local(utc_time, include_seconds=True)

        # Should contain seconds (either HH:MM:SS or H:MM:SS AM/PM format)
        assert "45" in result or ":45" in result

    def test_no_seconds_by_default(self) -> None:
        """Test that seconds are not included by default."""
        utc_time = datetime(2025, 1, 15, 12, 30, 45, tzinfo=UTC)
        result = format_time_local(utc_time)

        # Should not contain seconds
        # Format is either HH:MM or H:MM AM/PM
        parts = result.replace(" AM", "").replace(" PM", "").split(":")
        assert len(parts) == 2  # Only hours and minutes

    def test_handles_timezone_aware_datetime(self) -> None:
        """Test that timezone-aware datetimes are handled correctly."""
        # Create a datetime in a different timezone (UTC+5)
        tz_plus5 = timezone(timedelta(hours=5))
        dt = datetime(2025, 1, 15, 17, 30, 0, tzinfo=tz_plus5)

        result = format_time_local(dt)

        # The result should be a valid time string
        assert result is not None
        assert len(result) >= 4  # At minimum "H:MM"

    def test_output_format_is_consistent(self) -> None:
        """Test that output format is consistent for same locale."""
        utc_time1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        utc_time2 = datetime(2025, 1, 15, 22, 0, 0, tzinfo=UTC)

        result1 = format_time_local(utc_time1)
        result2 = format_time_local(utc_time2)

        # Both should use same format (either both 24h or both 12h)
        is_12h_1 = "AM" in result1 or "PM" in result1
        is_12h_2 = "AM" in result2 or "PM" in result2
        assert is_12h_1 == is_12h_2


class TestFormatDuration:
    """Tests for the format_duration function."""

    def test_minutes_only(self) -> None:
        """Test durations less than an hour show just minutes."""
        assert format_duration(timedelta(minutes=45)) == "45 minutes"
        assert format_duration(timedelta(minutes=5)) == "5 minutes"
        assert format_duration(timedelta(minutes=1)) == "1 minute"

    def test_hours_and_minutes(self) -> None:
        """Test durations between 1 and 24 hours show hours and minutes."""
        assert format_duration(timedelta(hours=2, minutes=30)) == "2 hours 30 minutes"
        assert format_duration(timedelta(hours=1, minutes=0)) == "1 hour"
        assert format_duration(timedelta(hours=1, minutes=1)) == "1 hour 1 minute"
        assert format_duration(timedelta(hours=5, minutes=15)) == "5 hours 15 minutes"

    def test_days_and_hours(self) -> None:
        """Test very long durations show days and hours."""
        assert format_duration(timedelta(days=1, hours=3)) == "1 day 3 hours"
        assert format_duration(timedelta(days=2, hours=0)) == "2 days"
        assert format_duration(timedelta(days=1, hours=1)) == "1 day 1 hour"
        assert format_duration(timedelta(hours=27, minutes=23)) == "1 day 3 hours"

    def test_accepts_float_seconds(self) -> None:
        """Test that float seconds are accepted as input."""
        assert format_duration(2700.0) == "45 minutes"  # 45 * 60
        assert format_duration(5400.0) == "1 hour 30 minutes"  # 90 * 60

    def test_1643_minutes_example(self) -> None:
        """Test the specific case from the bug report (1643 minutes)."""
        result = format_duration(timedelta(minutes=1643))
        assert result == "1 day 3 hours"
