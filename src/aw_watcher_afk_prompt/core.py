# ruff: noqa: EM101, EM102
import datetime
import json
import logging
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
from requests.exceptions import HTTPError

from aw_watcher_afk_prompt.utils import LOCAL_TIMEZONE, format_time_local

# Import ActivityLine for split mode support
try:
    from aw_watcher_afk_prompt.split_dialog import ActivityLine
except ImportError:
    # Fallback if split_dialog not available
    ActivityLine = None

WATCHER_NAME = "aw-watcher-afk-prompt"
DATA_KEY = "message"
"""What field in the event data to store the user's message in."""


class AWAfkPromptError(Exception):
    pass


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def find_afk_bucket(buckets: dict[str, Any]) -> str:
    # Find aw-watcher-afk bucket, excluding our own bucket and lid bucket
    afk_buckets = [
        bucket for bucket in buckets
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


def squash_overlaps(events: list[aw_core.Event]) -> list[aw_core.Event]:
    # Make a deep copy because the period_union function edits the events instead of returning new ones.
    return aw_transform.sort_by_timestamp(aw_transform.period_union(deepcopy(events), []))


def get_utc_now() -> datetime.datetime:
    return datetime.datetime.now().astimezone(datetime.UTC)


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
    def __init__(self, client: ActivityWatchClient, enable_lid_events: bool = True,
                 history_limit: int = 100):
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
        recent_events.extend(aw_transform.sort_by_timestamp(
            client.get_events(self.bucket_id, limit=100)
        ))
        self.state = AWAfkPromptState(recent_events, self.seen_store)

        self.afk_bucket_id = find_afk_bucket(self._all_buckets)

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

        Only marks the event as "seen" after successful posting to avoid data loss.
        """
        try:
            # Update event with message
            event.data[DATA_KEY] = message
            event["id"] = None  # Wipe the ID so we don't edit the AFK event

            # Post to ActivityWatch FIRST
            self.client.insert_event(self.bucket_id, event)
            logger.info(f"Successfully posted event: {message}")

            # Only mark as seen AFTER successful posting
            self.state.mark_event_as_seen(event)

        except Exception as e:
            logger.error(f"Failed to post event: {e}")
            logger.error("Event will be queued for retry on next iteration")
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
            try:
                # Create a new event for this activity with split metadata
                event = aw_core.Event(
                    timestamp=activity.start_time,
                    duration=datetime.timedelta(
                        minutes=activity.duration_minutes,
                        seconds=activity.duration_seconds
                    ),
                    data={
                        DATA_KEY: activity.description,
                        "split": True,
                        "split_count": len(activities),
                        "split_index": i,
                        "split_id": split_id,
                    }
                )

                # Post to ActivityWatch
                self.client.insert_event(self.bucket_id, event)
                logger.info(f"Posted activity {i+1}/{len(activities)}: '{activity.description}' "
                          f"({activity.duration_minutes}m {activity.duration_seconds}s)")
                posted_count += 1

            except Exception as e:
                logger.error(f"Failed to post activity {i+1}/{len(activities)}: {e}")
                failed_count += 1
                # Continue trying to post remaining activities

        # Only mark original event as seen if ALL activities were posted successfully
        if failed_count == 0:
            self.state.mark_event_as_seen(original_event)
            logger.info(f"Successfully posted all {posted_count} split activities")
        else:
            logger.warning(f"Posted {posted_count}/{len(activities)} activities, "
                         f"{failed_count} failed. Event will be prompted again.")
            # Don't mark as seen - user will be prompted again

    def _fetch_events_with_dynamic_limit(self, initial_limit: int = 10, max_limit: int = 1000):
        """Fetch events with dynamic limit scaling.

        If we only get AFK heartbeats without any non-afk events to mark the
        boundary, we need more events to detect the gap properly. This method
        automatically doubles the limit until we find at least one non-afk event
        or hit the max limit.

        Returns:
            Tuple of (all_events, limit_used)
        """
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

            # Merge and sort
            all_events = aw_transform.sort_by_timestamp(afk_events + lid_events)

            if not all_events:
                return all_events, limit

            # Check if we have at least one non-afk event (to mark boundaries)
            has_non_afk = any(not is_afk(e) for e in all_events)

            if has_non_afk:
                # We have boundaries, good to go
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

    def get_new_afk_events_to_note(self, seconds: float, durration_thresh: float) -> Iterator[aw_core.Event] | None:
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
        """
        try:
            # Fetch events with dynamic limit scaling
            all_events, limit_used = self._fetch_events_with_dynamic_limit(
                initial_limit=10,
                max_limit=self.history_limit
            )

            # Check if currently AFK (from either source)
            # Most recent event is LAST after sorting (ascending order)
            if all_events:
                most_recent = all_events[-1]  # Last element is most recent
                currently_afk = is_afk(most_recent)
                logger.debug(f"Most recent event: {most_recent.timestamp.astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')} | "
                           f"status={most_recent.data.get('status')} | currently_afk={currently_afk}")
                if currently_afk:
                    # Currently AFK, wait to bring up the prompt
                    logger.debug("Currently AFK, waiting for user to return")
                    return

            yield from self.state.get_unseen_afk_events(all_events, seconds, durration_thresh)
        except HTTPError:
            logger.exception("Failed to get events from the server.")
            return


class AWAfkPromptState:
    def __init__(self, recent_events: Iterable[aw_core.Event],
                 seen_store: SeenEventsStore | None = None):
        self.recent_events = recent_events if isinstance(recent_events, deque) else deque(recent_events, 100)
        """The recent events we have posted to the aw-watcher-afk-prompt bucket.

        This is used to avoid asking the user to log an absence that they have already logged.

        Sorted from earliest to most recent."""
        self.seen_store = seen_store

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

    def get_unseen_afk_events(self, events: list[aw_core.Event], recency_thresh: float, durration_thresh: float) -> Iterator[aw_core.Event]:
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
        logger.debug(f"Non-AFK events after squash: {len(non_afk_events)}")
        for evt in non_afk_events[-3:]:  # Last 3 events
            start = evt.timestamp.astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')
            end = (evt.timestamp + evt.duration).astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')
            logger.debug(f"  Event: {start} - {end} ({evt.duration.total_seconds():.1f}s)")
        pseudo_afk_events = list(get_gaps(non_afk_events))
        logger.debug(f"Gaps found: {len(pseudo_afk_events)}")
        for gap in pseudo_afk_events:
            logger.debug(f"  Gap: {gap.timestamp.astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')} | {gap.duration.total_seconds():.1f}s")

        pseudo_afk_events = [e for e in pseudo_afk_events if not self.has_event(e)]
        logger.debug(f"Gaps after filtering seen: {len(pseudo_afk_events)}")
        buffered_now = get_utc_now() - datetime.timedelta(seconds=recency_thresh)
        for event in pseudo_afk_events:
            long_enough = event.duration.seconds > durration_thresh
            recent_enough = event.timestamp + event.duration > buffered_now
            logger.debug(f"  Checking gap at {event.timestamp.astimezone(LOCAL_TIMEZONE).strftime('%H:%M:%S')}: "
                       f"long_enough={long_enough} ({event.duration.seconds}s > {durration_thresh}s), "
                       f"recent_enough={recent_enough}")
            if long_enough and recent_enough:
                logger.debug(f"Found event to note: {event}")
                yield event
