"""Utility functions for aw-watcher-afk-prompt."""

import datetime
import locale
from datetime import timedelta

LOCAL_TIMEZONE = datetime.datetime.now().astimezone().tzinfo

# Age beyond which a prompted AFK period is considered "old" and gets a warning symbol.
STALE_AGE_THRESHOLD = timedelta(minutes=15)

# Symbol prepended to the age string when a prompt is stale (see STALE_AGE_THRESHOLD).
WARNING_SYMBOL = "⚠️"


def format_duration(duration: timedelta | float) -> str:
    """Format a duration in a human-readable way.

    Shows minutes for short durations, hours and minutes for medium durations,
    and days and hours for very long durations.

    Args:
        duration: Either a timedelta or total seconds as a float

    Returns:
        Formatted duration string (e.g., "45 minutes", "2 hours 30 minutes", "1 day 3 hours")
    """
    if isinstance(duration, timedelta):
        total_seconds = duration.total_seconds()
    else:
        total_seconds = duration

    total_minutes = int(total_seconds // 60)

    if total_minutes < 60:
        return f"{total_minutes} minute{'s' if total_minutes != 1 else ''}"

    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours < 24:
        parts = [f"{hours} hour{'s' if hours != 1 else ''}"]
        if minutes > 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        return " ".join(parts)

    days = hours // 24
    hours = hours % 24

    parts = [f"{days} day{'s' if days != 1 else ''}"]
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    return " ".join(parts)


def format_age(age: timedelta | float, *, stale_threshold: timedelta = STALE_AGE_THRESHOLD) -> str:
    """Format how long ago something happened, for display in the prompt.

    Sub-minute ages are reported in seconds; longer ages reuse ``format_duration``.
    When the age reaches ``stale_threshold`` the result is prefixed with
    ``WARNING_SYMBOL`` so the user can tell at a glance they are being prompted
    about an old AFK period.

    Args:
        age: Time elapsed, as a timedelta or total seconds as a float.
        stale_threshold: Age at or above which the warning symbol is added.

    Returns:
        Strings like ``"5 seconds ago"``, ``"5 minutes ago"`` or
        ``"⚠️ 3 hours ago"``.
    """
    age_td = age if isinstance(age, timedelta) else timedelta(seconds=age)

    # Guard against clock skew producing a "negative age".
    if age_td.total_seconds() < 0:
        age_td = timedelta(0)

    total_seconds = age_td.total_seconds()
    if total_seconds < 60:
        secs = int(total_seconds)
        human = f"{secs} second{'s' if secs != 1 else ''} ago"
    else:
        human = f"{format_duration(age_td)} ago"

    if age_td >= stale_threshold:
        return f"{WARNING_SYMBOL} {human}"
    return human


def format_time_local(dt: datetime.datetime, include_seconds: bool = False) -> str:
    """Format a datetime as local time using locale-appropriate format.

    Uses 24-hour clock unless the locale prefers 12-hour format.
    Always converts to local timezone first.

    Args:
        dt: Datetime to format (can be any timezone, will be converted to local)
        include_seconds: Whether to include seconds in the output

    Returns:
        Formatted time string (e.g., "14:30" or "2:30 PM" depending on locale)
    """
    # Convert to local timezone
    local_dt = dt.astimezone(LOCAL_TIMEZONE)

    # Check locale preference for time format
    # %X gives the locale's preferred time representation
    # We check if it contains AM/PM indicators to detect 12-hour preference
    try:
        locale_time_format = locale.nl_langinfo(locale.T_FMT)
        use_12_hour = (
            "%p" in locale_time_format
            or "%P" in locale_time_format
            or "%I" in locale_time_format
            or "%r" in locale_time_format
        )
    except (AttributeError, locale.Error):
        # Fallback: use 24-hour format if we can't determine locale
        use_12_hour = False

    if use_12_hour:
        if include_seconds:
            return local_dt.strftime("%I:%M:%S %p").lstrip("0")
        else:
            return local_dt.strftime("%I:%M %p").lstrip("0")
    elif include_seconds:
        return local_dt.strftime("%H:%M:%S")
    else:
        return local_dt.strftime("%H:%M")
