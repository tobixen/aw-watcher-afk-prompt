# ruff: noqa: EM101, EM102
import datetime
import json
import logging
import time
from collections import deque
from collections.abc import Iterable, Iterator
from copy import deepcopy
from functools import cached_property
from itertools import pairwise
from pathlib import Path
from typing import Any

import appdirs
import aw_core
import aw_transform
from aw_client.client import ActivityWatchClient
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError

from aw_watcher_afk_prompt.utils import LOCAL_TIMEZONE

# Import ActivityLine for split mode support
try:
    from aw_watcher_afk_prompt.split_dialog import ActivityLine
except ImportError:
    # Fallback if split_dialog not available
    ActivityLine = None

WATCHER_NAME = "aw-watcher-afk-prompt"
DATA_KEY = "message"

_POST_MAX_RETRIES = 13  # 1 initial attempt + 12 retries × 10 s ≈ 2 minutes
_POST_RETRY_INTERVAL = 10  # seconds between retries on transient errors


def _is_transient_error(exc: Exception) -> bool:
    """Return True for errors that are worth retrying (server/network glitches)."""
    if isinstance(exc, RequestsConnectionError):
        return True
    if isinstance(exc, HTTPError):
        resp = getattr(exc, "response", None)
        return resp is not None and resp.status_code >= 500
    return False


"""What field in the event data to store the user's message in."""


class AWAfkPromptError(Exception):
    pass


logger = logging.getLogger(__name__)


def find_afk_bucket(buckets: dict[str, Any]) -> str:
    # Find aw-watcher-afk bucket, excluding our own bucket and lid bucket
    afk_buckets = [
        bucket
        for bucket in buckets
        if "aw-watcher-afk" in bucket
        and "lid" not in bucket
        and "afk-prompt" not in bucket  # Exclude our own bucket
        and "ask-away" not in bucket  # Exclude old bucket name
    ]
    logger.debug(f"All buckets: {list(buckets.keys())}")
    logger.debug(f"Matching AFK buckets: {afk_buckets}")
    match afk_buckets:
        case []:
            logger.error("Cannot find the afk bucket. Is aw-watcher-afk running?")
            raise AWAfkPromptError("Cannot find the afk bucket. Is aw-watcher-afk running?")
        case [bucket]:
            return bucket
        case _:
            logger.error(f"Found too many afk buckets: {afk_buckets}")
            raise AWAfkPromptError(f"Found too many afk buckets: {afk_buckets}.")


def find_window_bucket(buckets: dict[str, Any]) -> str | None:
    """Find the window watcher bucket (aw-watcher-window or aw-watcher-window-wayland).

    Returns None if not found (window watcher is optional for this purpose).
    """
    window_buckets = [b for b in buckets if "aw-watcher-window" in b]
    if len(window_buckets) == 0:
        return None
    if len(window_buckets) == 1:
        return window_buckets[0]
    # Multiple window buckets (e.g. Wayland + X11 co-existing) — pick the first alphabetically
    logger.warning(f"Found multiple window buckets: {window_buckets}, using {sorted(window_buckets)[0]}")
    return sorted(window_buckets)[0]


def find_lid_bucket(buckets: dict[str, Any]):
    """Find the lid watcher bucket (aw-watcher-lid).

    Returns None if not found (lid watcher is optional).
    """
    lid_buckets = [bucket for bucket in buckets if "lid" in bucket]
    if len(lid_buckets) == 0:
        return None
    if len(lid_buckets) == 1:
        return lid_buckets[0]
    raise AWAfkPromptError(f"Found too many lid buckets: {buckets}.")


def is_afk(event: aw_core.Event) -> bool:
    """Check if event represents an AFK state.

    Handles both regular AFK ("afk") and system-level AFK ("system-afk" from lid/suspend events).
    """
    return event.data["status"] in ("afk", "system-afk")


def filter_lid_events_for_presence(lid_events: list[aw_core.Event]) -> list[aw_core.Event]:
    """Keep only the AFK-ish lid events (lid closed / suspend).

    aw-watcher-lid emits a "not-afk" event spanning the *entire* time the lid is
    open, regardless of whether anyone is at the keyboard.  Treating that as
    presence hides any AFK gap the event covers: once the open event is
    finalized (on the next lid close) the gap disappears from the union of
    not-afk events and is silently lost (observed 2026-06-11: a 29-minute away
    period was masked by a 95-minute lid-open event and never prompted for).

    Lid events may only ever *add* AFK evidence, never presence.
    """
    return [e for e in lid_events if is_afk(e)]


def squash_overlaps(events: list[aw_core.Event]) -> list[aw_core.Event]:
    # Make a deep copy because the period_union function edits the events instead of returning new ones.
    return aw_transform.sort_by_timestamp(aw_transform.period_union(deepcopy(events), []))


def get_utc_now() -> datetime.datetime:
    return datetime.datetime.now().astimezone(datetime.UTC)


def adjust_gap_start_for_window_activity(
    gap: aw_core.Event,
    afk_events: list[aw_core.Event],
    window_events: list[aw_core.Event],
) -> aw_core.Event:
    """Advance a gap's start time to the confirmed AFK-event start when window activity exists.

    During the idle-timeout countdown (typically ~2 min), the AFK watcher keeps
    sending not-afk heartbeats but stops at the last real interaction. The gap
    between the final not-afk heartbeat and the AFK event start can contain
    genuine window-focus changes (e.g. reading a website, watching a video).

    If window events exist in [gap_start, afk_event_start), the user was
    demonstrably still at the computer, so we trust the idle-timeout and
    advance the gap start to ``afk_event_start``.

    If there are *no* window events in that window (suspend/poweroff), we leave
    the gap unchanged so it still covers the full unknown period.

    Parameters
    ----------
    gap:
        The detected gap in not-afk events (its .data dict may be empty).
    afk_events:
        All events from the AFK bucket (mix of "afk" and "not-afk").
    window_events:
        Window-watcher events for the same time range.
    """
    gap_start = gap.timestamp
    gap_end = gap.timestamp + gap.duration

    # Find the earliest "afk" event whose start falls within (gap_start, gap_end).
    afk_starts = [e.timestamp for e in afk_events if is_afk(e) and gap_start < e.timestamp < gap_end]
    if not afk_starts:
        return gap  # No AFK event to snap to

    afk_event_start = min(afk_starts)

    # Check for window activity strictly inside [gap_start, afk_event_start).
    has_window_activity = any(
        w.duration.total_seconds() > 0 and w.timestamp < afk_event_start and (w.timestamp + w.duration) > gap_start
        for w in window_events
    )
    if not has_window_activity:
        return gap  # Likely suspend/poweroff — leave gap unchanged

    new_duration = gap_end - afk_event_start
    if new_duration.total_seconds() <= 0:
        return gap

    # The caller logs the advance (deduped per gap) — keeping it out of this pure
    # function avoids re-logging at INFO on every poll for a still-pending gap.
    return aw_core.Event(None, afk_event_start, new_duration, gap.data)


def get_ongoing_afk_start(events: list[aw_core.Event]) -> datetime.datetime | None:
    """Return the start of the currently-ongoing AFK period, or None if not currently AFK.

    The start is defined as the end of the last not-afk event (when activity stopped).
    Returns None if the most recent event is not-afk, the list is empty, or there are
    no not-afk events to anchor the start.
    """
    if not events:
        return None
    most_recent = events[-1]
    if not is_afk(most_recent):
        return None
    non_afk = [e for e in events if not is_afk(e)]
    if not non_afk:
        return None
    last_non_afk = non_afk[-1]
    return last_non_afk.timestamp + last_non_afk.duration


def get_gaps(events: list[aw_core.Event]) -> Iterator[aw_core.Event]:
    flattened_events = aw_transform.sort_by_timestamp(squash_overlaps(events))
    for first, second in pairwise(flattened_events):
        first_end = first.timestamp + first.duration
        if first_end < second.timestamp:
            yield aw_core.Event(None, first_end, second.timestamp - first_end)


class SeenEventsStore:
    """Persistent storage for seen events to survive restarts.

    Stores event timestamps and durations in a JSON file to prevent
    re-prompting for events that were already handled in previous sessions.
    """

    def __init__(self, max_age_days: int = 7):
        """Initialize the seen events store.

        Args:
            max_age_days: Events older than this will be cleaned up on load
        """
        config_dir = Path(appdirs.user_config_dir("aw-watcher-afk-prompt"))
        config_dir.mkdir(parents=True, exist_ok=True)
        self._store_file = config_dir / "seen_events.json"
        self._max_age_days = max_age_days
        self._seen: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """Load seen events from file and clean up old entries."""
        if self._store_file.exists():
            try:
                with self._store_file.open() as f:
                    data = json.load(f)
                    # Clean up old entries
                    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=self._max_age_days)
                    for key, value in data.items():
                        try:
                            ts = datetime.datetime.fromisoformat(value["timestamp"])
                            if ts > cutoff:
                                self._seen[key] = value
                        except (KeyError, ValueError):
                            continue
                    logger.info(f"Loaded {len(self._seen)} seen events from persistent store")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load seen events: {e}")

    def _save(self) -> None:
        """Save seen events to file."""
        try:
            with self._store_file.open("w") as f:
                json.dump(self._seen, f, indent=2)
        except OSError as e:
            logger.warning(f"Failed to save seen events: {e}")

    def _make_key(self, event: aw_core.Event) -> str:
        """Create a unique key for an event based on timestamp."""
        return event.timestamp.isoformat()

    def add(self, event: aw_core.Event) -> None:
        """Mark an event as seen."""
        key = self._make_key(event)
        self._seen[key] = {
            "timestamp": event.timestamp.isoformat(),
            "duration": event.duration.total_seconds(),
        }
        self._save()

    def has_overlap(self, event: aw_core.Event, overlap_thresh: float = 0.95) -> bool:
        """Check if we've seen an event that overlaps significantly with this one."""
        new_start = event.timestamp
        new_end = event.timestamp + event.duration

        for value in self._seen.values():
            try:
                seen_start = datetime.datetime.fromisoformat(value["timestamp"])
                seen_end = seen_start + datetime.timedelta(seconds=value["duration"])

                # Calculate overlap
                overlap_start = max(seen_start, new_start)
                overlap_end = min(seen_end, new_end)
                overlap = (overlap_end - overlap_start).total_seconds()

                if overlap <= 0:
                    continue

                # Compare against smaller duration
                min_duration = min(event.duration.total_seconds(), value["duration"])
                if min_duration > 0 and overlap / min_duration > overlap_thresh:
                    return True
            except (KeyError, ValueError):
                continue

        return False


class AWAfkPromptClient:
    def __init__(self, client: ActivityWatchClient, enable_lid_events: bool = True, history_limit: int = 100):
        self.client = client
        self.bucket_id = f"{WATCHER_NAME}_{self.client.client_hostname}"
        self.enable_lid_events = enable_lid_events
        self.history_limit = history_limit

        if self.bucket_id not in self._all_buckets:
            # Create bucket synchronously - we need it to exist before fetching events.
            # (queued=True would defer creation, causing 404 on the get_events call below)
            client.create_bucket(self.bucket_id, event_type="afktask")

        # Initialize persistent seen events store
        self.seen_store = SeenEventsStore()

        # Load recent events for history display (still using deque for in-memory)
        recent_events = deque(maxlen=100)
        recent_events.extend(aw_transform.sort_by_timestamp(client.get_events(self.bucket_id, limit=100)))
        self.state = AWAfkPromptState(recent_events, self.seen_store)

        self.afk_bucket_id = find_afk_bucket(self._all_buckets)
        self.window_bucket_id = find_window_bucket(self._all_buckets)
        if self.window_bucket_id:
            logger.info(f"Window watcher detected: {self.window_bucket_id}")
        else:
            logger.info("Window watcher not found, gap-start adjustment disabled")

        # Check for optional lid watcher integration (aw-watcher-lid)
        # See: https://github.com/tobixen/aw-watcher-lid
        self.lid_bucket_id = None
        if enable_lid_events:
            self.lid_bucket_id = find_lid_bucket(self._all_buckets)
            if self.lid_bucket_id:
                logger.info(f"Lid watcher detected: {self.lid_bucket_id}")
            else:
                logger.info("Lid watcher not found, will only use regular AFK events")
        else:
            logger.info("Lid watcher integration disabled in config")

    @cached_property
    def _all_buckets(self) -> dict[str, Any]:
        return self.client.get_buckets()

    def post_event(self, event: aw_core.Event, message: str) -> None:
        """Post a single event with error handling.

        Retries on transient server/network errors (ConnectionError, 5xx).
        Only marks the event as "seen" after successful posting to avoid data loss.
        """
        event.data[DATA_KEY] = message
        event["id"] = None  # Wipe the ID so we don't edit the AFK event

        for attempt in range(_POST_MAX_RETRIES):
            try:
                self.client.insert_event(self.bucket_id, event)
                logger.info(f"Successfully posted event: {message}")
                self.state.mark_event_as_seen(event)
                return
            except Exception as e:
                if _is_transient_error(e) and attempt < _POST_MAX_RETRIES - 1:
                    logger.warning(
                        f"Transient error posting event (attempt {attempt + 1}/{_POST_MAX_RETRIES}), "
                        f"retrying in {_POST_RETRY_INTERVAL}s: {e}"
                    )
                    time.sleep(_POST_RETRY_INTERVAL)
                else:
                    logger.error(f"Failed to post event after {attempt + 1} attempt(s): {e}")
                    # Don't mark as seen - event will be prompted again
                    raise

    def post_split_events(self, original_event: aw_core.Event, activities: list):
        """Post multiple events from split mode with error handling.

        Args:
            original_event: The original AFK event that was split
            activities: List of ActivityLine objects from split mode
        """
        if ActivityLine is None:
            logger.error("ActivityLine not available, cannot post split events")
            return

        posted_count = 0
        failed_count = 0

        # Generate a unique split ID based on original event timestamp
        split_id = str(original_event.timestamp.timestamp())

        for i, activity in enumerate(activities):
            event = aw_core.Event(
                timestamp=activity.start_time,
                duration=datetime.timedelta(minutes=activity.duration_minutes, seconds=activity.duration_seconds),
                data={
                    DATA_KEY: activity.description,
                    "split": True,
                    "split_count": len(activities),
                    "split_index": i,
                    "split_id": split_id,
                },
            )

            for attempt in range(_POST_MAX_RETRIES):
                try:
                    self.client.insert_event(self.bucket_id, event)
                    logger.info(
                        f"Posted activity {i + 1}/{len(activities)}: '{activity.description}' "
                        f"({activity.duration_minutes}m {activity.duration_seconds}s)"
                    )
                    posted_count += 1
                    break
                except Exception as e:
                    if _is_transient_error(e) and attempt < _POST_MAX_RETRIES - 1:
                        logger.warning(
                            f"Transient error posting activity {i + 1}/{len(activities)} "
                            f"(attempt {attempt + 1}/{_POST_MAX_RETRIES}), "
                            f"retrying in {_POST_RETRY_INTERVAL}s: {e}"
                        )
                        time.sleep(_POST_RETRY_INTERVAL)
                    else:
                        logger.error(
                            f"Failed to post activity {i + 1}/{len(activities)} after {attempt + 1} attempt(s): {e}"
                        )
                        failed_count += 1
                        break  # move on to the next activity

        # Only mark original event as seen if ALL activities were posted successfully
        if failed_count == 0:
            self.state.mark_event_as_seen(original_event)
            logger.info(f"Successfully posted all {posted_count} split activities")
        else:
            logger.warning(
                f"Posted {posted_count}/{len(activities)} activities, "
                f"{failed_count} failed. Event will be prompted again."
            )
            # Don't mark as seen - user will be prompted again

    def _fetch_events_with_dynamic_limit(
        self,
        initial_limit: int = 10,
        max_limit: int = 1000,
        start_time: datetime.datetime | None = None,
    ):
        """Fetch events with dynamic limit scaling.

        If we only get AFK heartbeats without any non-afk events to mark the
        boundary, we need more events to detect the gap properly. This method
        automatically doubles the limit until we find at least one non-afk event
        or hit the max limit.

        When start_time is provided (backfill mode), fetches all events since that
        time using a large limit, bypassing the incremental doubling. This prevents
        stale events from an inactive watcher (e.g. lid watcher that stopped months
        ago) from polluting the event set and hiding real gaps.

        Returns:
            Tuple of (all_events, limit_used)
        """
        if start_time is not None:
            # Time-bounded fetch: use start_time to exclude stale events from inactive
            # buckets (e.g. a lid watcher that has been inactive for months). Use a large
            # limit — ActivityWatch merges heartbeats so event counts are manageable.
            fetch_limit = max(max_limit * 20, 2000)
            afk_events = self.client.get_events(self.afk_bucket_id, limit=fetch_limit, start=start_time)
            lid_events = []
            if self.lid_bucket_id:
                try:
                    lid_events = self.client.get_events(self.lid_bucket_id, limit=fetch_limit, start=start_time)
                except HTTPError:
                    logger.warning("Failed to get lid events, continuing with AFK events only")
            all_events = aw_transform.sort_by_timestamp(afk_events + filter_lid_events_for_presence(lid_events))
            logger.debug(
                f"Time-bounded fetch: {len(all_events)} events since "
                f"{start_time.astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M')}"
            )
            return all_events, fetch_limit

        limit = initial_limit

        while limit <= max_limit:
            # Fetch AFK events
            afk_events = self.client.get_events(self.afk_bucket_id, limit=limit)

            # Fetch lid events if enabled
            lid_events = []
            if self.lid_bucket_id:
                try:
                    lid_events = self.client.get_events(self.lid_bucket_id, limit=limit)
                except HTTPError:
                    logger.warning("Failed to get lid events, continuing with AFK events only")

            # Merge and sort (lid events only contribute AFK-ness, never presence)
            all_events = aw_transform.sort_by_timestamp(afk_events + filter_lid_events_for_presence(lid_events))

            if not all_events:
                return all_events, limit

            # Gap detection via get_gaps/pairwise needs at least 2 non-afk events
            # (one on each side of a gap) to detect any gap at all
            non_afk_count = sum(1 for e in all_events if not is_afk(e))

            if non_afk_count >= 2:
                # We have enough boundaries for gap detection
                if limit > initial_limit:
                    logger.debug(f"Dynamic limit scaling: needed {limit} events to find gap boundaries")
                return all_events, limit

            # All events are AFK - we might be missing the gap start
            # But first check if we got fewer events than requested (no more to fetch)
            if len(afk_events) < limit:
                logger.debug(f"Only AFK events found, but no more events available (got {len(afk_events)})")
                return all_events, limit

            # Double the limit and try again
            old_limit = limit
            limit *= 2
            logger.debug(f"Only AFK heartbeats found, increasing limit from {old_limit} to {limit}")

        logger.warning(f"Reached max limit ({max_limit}) without finding gap boundaries")
        return all_events, limit

    def get_ongoing_afk_event(self, durration_thresh: float) -> aw_core.Event | None:
        """Return a synthetic event for the currently-ongoing AFK period if long enough to prompt.

        Used to show a live-updating dialog before the user returns to the computer.
        Returns None if not currently AFK or the AFK duration is below the threshold.
        """
        all_events, _ = self._fetch_events_with_dynamic_limit()
        afk_start = get_ongoing_afk_start(all_events)
        if afk_start is None:
            return None
        duration = get_utc_now() - afk_start
        if duration.total_seconds() < durration_thresh:
            return None
        return aw_core.Event(None, afk_start, duration, {"status": "afk", "ongoing": True})

    def get_afk_period_end(
        self, afk_start: datetime.datetime, min_not_afk_duration: float = 0.0
    ) -> datetime.datetime | None:
        """Return when the AFK period that began at ``afk_start`` actually ended.

        The end is the start of the first not-afk event after ``afk_start``
        (skipping blips shorter than ``min_not_afk_duration``, except the most
        recent event, which is the "back at keyboard" signal and may still be
        short).  Returns None when no such event is found (still AFK, or the
        fetch failed) — callers should fall back to "now".
        """
        try:
            all_events, _ = self._fetch_events_with_dynamic_limit()
        except Exception:
            logger.warning("Could not fetch events to determine AFK period end, falling back to now")
            return None
        if not all_events:
            return None
        last = all_events[-1]
        candidates = [
            e.timestamp
            for e in all_events
            if not is_afk(e)
            and e.timestamp > afk_start
            and (e.duration.total_seconds() >= min_not_afk_duration or e is last)
        ]
        return min(candidates) if candidates else None

    def get_new_afk_events_to_note(
        self,
        seconds: float,
        durration_thresh: float,
        min_not_afk_duration: float = 0.0,
        start_time: datetime.datetime | None = None,
        include_while_afk: bool = False,
    ) -> Iterator[aw_core.Event] | None:
        """Check whether we recently finished a large AFK event.

        Fetches events from both regular AFK watcher and lid watcher (if enabled),
        then merges them to get a complete picture of away time.

        Uses dynamic limit scaling: starts with a small limit and automatically
        increases if only AFK heartbeats are found (indicating a long AFK period
        where we need more events to find the gap boundaries).

        Parameters
        ----------
        seconds : float
            The number of seconds to look into the past for events.
        durration_thresh : float
            The number of seconds you need to be away before reporting on it.
        include_while_afk : bool
            By default the scan yields nothing while the user is currently AFK
            (wait for their return before prompting). Set True for the
            still-AFK backfill path, which wants earlier *completed* unfilled
            gaps even now. The still-ongoing period is never included either
            way — gap detection needs a not-afk event on both sides.
        """
        # Fetch events with dynamic limit scaling (or time-bounded for backfill).
        # Connection errors (HTTPError, ConnectionError) are intentionally NOT caught here
        # so the caller can track server downtime and notify the user.
        all_events, limit_used = self._fetch_events_with_dynamic_limit(
            initial_limit=10, max_limit=self.history_limit, start_time=start_time
        )

        # Check if currently AFK (from either source)
        # Most recent event is LAST after sorting (ascending order)
        if all_events:
            most_recent = all_events[-1]  # Last element is most recent
            currently_afk = is_afk(most_recent)
            logger.debug(
                f"Most recent event: {most_recent.timestamp.astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')} | "
                f"status={most_recent.data.get('status')} | currently_afk={currently_afk}"
            )
            if currently_afk and not include_while_afk:
                # Currently AFK, wait to bring up the prompt
                logger.debug("Currently AFK, waiting for user to return")
                return

        # Fetch window events for gap-start adjustment (if window watcher is present).
        # The fetch must be time-bounded to the scanned range: a bare limit-N fetch
        # loses the events covering an older gap's idle countdown as newer window
        # events accumulate, making the adjustment (and thereby a near-threshold
        # gap's eligibility, and the seen-overlap check) flap between scans.
        window_events: list[aw_core.Event] = []
        if self.window_bucket_id:
            window_start = start_time
            if window_start is None and all_events:
                window_start = all_events[0].timestamp
            try:
                fetch_limit = max(self.history_limit * 20, 2000)
                window_events = self.client.get_events(self.window_bucket_id, limit=fetch_limit, start=window_start)
            except Exception:
                logger.warning("Failed to fetch window events for gap-start adjustment, skipping")

        yield from self.state.get_unseen_afk_events(
            all_events, seconds, durration_thresh, window_events, min_not_afk_duration
        )


class AWAfkPromptState:
    def __init__(self, recent_events: Iterable[aw_core.Event], seen_store: SeenEventsStore | None = None):
        self.recent_events = recent_events if isinstance(recent_events, deque) else deque(recent_events, 100)
        """The recent events we have posted to the aw-watcher-afk-prompt bucket.

        This is used to avoid asking the user to log an absence that they have already logged.

        Sorted from earliest to most recent."""
        self.seen_store = seen_store
        # Gaps that fell outside the depth window before the user answered them.
        # Kept in memory so they are re-presented the next time the user is at the
        # keyboard, rather than being silently discarded.
        self._deferred: list[aw_core.Event] = []
        # AFK-event starts for which we've already logged a gap-start advance, so a
        # still-pending gap doesn't re-log "Advancing gap start" at INFO every poll.
        self._logged_advances: set[datetime.datetime] = set()

    def has_event(self, new: aw_core.Event, overlap_thresh: float = 0.95) -> bool:
        """Check whether we have already posted an event that overlaps with the new event.

        Checks both in-memory recent events AND persistent storage.

        The self.recent_events data structure used to be a dictionary with keys as timestamp/durration.
        This method merely checked to see if the new event's (timestamp, durration) tuple was in the dictionary.

        However, for some reason the events coming from the aw-server seem to be slightly inconsistent at times.
        For example, look at the logs below:

            2023-09-23 19:33:45 [DEBUG]: Got events from the server: [('2023-09-23T23:33:37.730000+00:00', 'not-afk'), ...]
            2023-09-23 19:33:58 [DEBUG]: Got events from the server: [('2023-09-23T23:33:37.730000+00:00', 'not-afk'), ('2023-09-23T23:33:37.729000+00:00', 'not-afk'), ...]

        The second query returns an overlapping 'not-afk' event with a slightly earlier timestamp.
        This duplication + offset combination was causing us to double ask the user for input.
        Using overlaps with a percentage is more robust against this kind of thing.

        Note: We compare overlap against the SMALLER of the two durations because gaps can
        extend over time as new activity data comes in. If we compared against the new (larger)
        duration, we'd fail to recognize the same gap and ask the user again.
        """  # noqa: E501
        # First check persistent store (if available)
        if self.seen_store and self.seen_store.has_overlap(new, overlap_thresh):
            return True

        # Then check in-memory recent events
        for recent in self.recent_events:
            overlap_start = max(recent.timestamp, new.timestamp)
            overlap_end = min(recent.timestamp + recent.duration, new.timestamp + new.duration)
            overlap = overlap_end - overlap_start
            if overlap.total_seconds() <= 0:
                continue  # No overlap
            min_duration = min(recent.duration, new.duration)
            if overlap / min_duration > overlap_thresh:
                return True
        return False

    def mark_event_as_seen(self, event: aw_core.Event) -> None:
        """Mark an event as seen (add to recent_events) to prevent re-prompting.

        This should only be called AFTER the event has been successfully posted.
        Saves to both in-memory deque and persistent store.
        """
        if not self.has_event(event):
            logger.debug(f"Marking event as seen: {event}")
            self.recent_events.append(event)
            # Also persist to file
            if self.seen_store:
                self.seen_store.add(event)
        else:
            logger.debug(f"Event already marked as seen: {event}")

    def get_unseen_afk_events(
        self,
        events: list[aw_core.Event],
        recency_thresh: float,
        durration_thresh: float,
        window_events: list[aw_core.Event] | None = None,
        min_not_afk_duration: float = 0.0,
    ) -> Iterator[aw_core.Event]:
        """Check whether we recently finished a large AFK event.

        Parameters
        ----------
        events : list[aw_core.Event]
            The events to check for AFK events.
        seconds : float
            Events more than this many seconds ago will be ignored.
        durration_thresh : float
            Events with a durration less than this many seconds will be ignored.
        """
        events_log = [
            (e.timestamp.astimezone(LOCAL_TIMEZONE).isoformat(), e.duration.total_seconds(), e.data["status"])
            for e in events
        ]
        logger.debug(f"Checking for unseen in: {events_log}")

        # Filter out events that have zero length. Sometimes a zero length not-afk event is generated if you open
        # up your computer from being suspended but don't do anything with it. This event is overwritten soon and
        # doesn't exist in later queries. If we don't filter them out we can ask the user to fill the time in twice.
        events = [e for e in events if e.duration.total_seconds() > 0]

        # Use gaps in non-afk events instead of the afk-events themselves to handle when the computer
        # is suspended or powered off.
        non_afk_events = squash_overlaps([e for e in events if not is_afk(e)])
        if min_not_afk_duration > 0:
            # Always preserve the most recent not-afk event as the right boundary for gap
            # detection. If the user just returned (e.g. after boot/resume), the ongoing
            # not-afk event may still be very short, but it IS the "back at keyboard" signal
            # and must not be filtered out.
            last_idx = len(non_afk_events) - 1
            filtered = [
                e
                for i, e in enumerate(non_afk_events)
                if e.duration.total_seconds() >= min_not_afk_duration or i == last_idx
            ]
            if len(filtered) != len(non_afk_events):
                logger.debug(
                    f"Filtered {len(non_afk_events) - len(filtered)} short not-afk events (< {min_not_afk_duration:.0f}s), merging surrounding AFK periods"
                )
            non_afk_events = filtered
        logger.debug(f"Non-AFK events after squash: {len(non_afk_events)}")
        for evt in non_afk_events[-3:]:  # Last 3 events
            start = evt.timestamp.astimezone(LOCAL_TIMEZONE).strftime("%H:%M:%S")
            end = (evt.timestamp + evt.duration).astimezone(LOCAL_TIMEZONE).strftime("%H:%M:%S")
            logger.debug(f"  Event: {start} - {end} ({evt.duration.total_seconds():.1f}s)")
        pseudo_afk_events = list(get_gaps(non_afk_events))
        logger.debug(f"Gaps found: {len(pseudo_afk_events)}")
        for gap in pseudo_afk_events:
            logger.debug(
                f"  Gap: {gap.timestamp.astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')} | {gap.duration.total_seconds():.1f}s"
            )

        # Filter already-seen gaps BEFORE the window adjustment: stored seen events
        # are the adjusted (shrunk) gaps and lie fully inside the raw gap, so the
        # overlap check still matches — and we avoid re-adjusting (and re-logging
        # "Advancing gap start") for gaps the user already answered, every poll.
        pseudo_afk_events = [e for e in pseudo_afk_events if not self.has_event(e)]
        logger.debug(f"Gaps after filtering seen: {len(pseudo_afk_events)}")

        # If window events are provided, advance each gap's start past the idle
        # countdown when window activity was present (fixes 2-min systematic overlap).
        # Keep the raw duration alongside: eligibility below is judged on the raw
        # span, so the adjustment can never push a gap below the length threshold
        # in one scan but not another (which prompted gaps late and out of order).
        gap_pairs: list[tuple[aw_core.Event, datetime.timedelta]] = [(g, g.duration) for g in pseudo_afk_events]
        if window_events:
            adjusted: list[tuple[aw_core.Event, datetime.timedelta]] = []
            logged_now: set[datetime.datetime] = set()
            for gap in pseudo_afk_events:
                new_gap = adjust_gap_start_for_window_activity(gap, events, window_events)
                advance = (new_gap.timestamp - gap.timestamp).total_seconds()
                if advance > 0:
                    # Key on the advanced start (== the real AFK event start), which is
                    # stable across polls — the raw gap start jitters with heartbeats.
                    # Log INFO once per pending gap, DEBUG on repeats.
                    msg = f"Advancing gap start by {advance:.0f}s (window activity present during idle countdown)"
                    logged_now.add(new_gap.timestamp)
                    if new_gap.timestamp in self._logged_advances:
                        logger.debug(msg)
                    else:
                        logger.info(msg)
                adjusted.append((new_gap, gap.duration))
            # Keep only currently-pending advanced gaps so the set stays bounded and a
            # gap that disappears then reappears is logged afresh.
            self._logged_advances = logged_now
            gap_pairs = adjusted

        # Re-present any gaps that previously expired from the depth window but were
        # never answered.  Purge ones that have since been answered (has_event → True).
        self._deferred = [g for g in self._deferred if not self.has_event(g)]
        if self._deferred:
            logger.info(f"Re-presenting {len(self._deferred)} deferred gap(s) that expired from depth window")
        deferred_timestamps = {g.timestamp for g in self._deferred}
        for gap in self._deferred:
            yield gap

        buffered_now = get_utc_now() - datetime.timedelta(seconds=recency_thresh)
        for event, raw_duration in gap_pairs:
            if event.timestamp in deferred_timestamps:
                continue  # already yielded via deferred path above
            long_enough = raw_duration.total_seconds() > durration_thresh
            recent_enough = event.timestamp + event.duration > buffered_now
            logger.debug(
                f"  Checking gap at {event.timestamp.astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')}: "
                f"long_enough={long_enough} (raw {raw_duration.total_seconds():.1f}s > {durration_thresh}s), "
                f"recent_enough={recent_enough}"
            )
            if long_enough and recent_enough:
                logger.debug(f"Found event to note: {event}")
                yield event
            elif long_enough and not recent_enough:
                start_str = event.timestamp.astimezone(LOCAL_TIMEZONE).strftime("%H:%M:%S")
                end_str = (event.timestamp + event.duration).astimezone(LOCAL_TIMEZONE).strftime("%H:%M:%S")
                logger.warning(
                    f"Gap at {start_str}-{end_str} ({event.duration.total_seconds():.0f}s) "
                    f"expired from depth window, deferring until answered"
                )
                self._deferred.append(event)
