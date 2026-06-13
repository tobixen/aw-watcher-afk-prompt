# ruff: noqa: EM101, EM102
import argparse
import time
from collections.abc import Iterable
from tkinter import messagebox

import aw_core
from aw_client.client import ActivityWatchClient
from aw_core.log import setup_logging
from requests.exceptions import ConnectionError, HTTPError

import aw_watcher_afk_prompt.dialog as aw_dialog
from aw_watcher_afk_prompt._version import __version__
from aw_watcher_afk_prompt.config import load_config
from aw_watcher_afk_prompt.core import (
    DATA_KEY,
    WATCHER_NAME,
    AWAfkPromptClient,
    AWAfkPromptError,
    logger,
)
from aw_watcher_afk_prompt.utils import format_age, format_duration, format_time_local

# After the user snoozes a dialog (Snooze button, Escape, empty Enter, timeout,
# or closing the window), no new prompts are shown for this long. The watcher
# keeps scanning in the meantime, so nothing is lost — just not asked about yet.
SNOOZE_SECONDS = 300


def prompt(
    event: aw_core.Event,
    recent_events: Iterable[aw_core.Event],
    queue_info: dict | None = None,
    stale_minutes: float = 15.0,
) -> str | None:
    from datetime import UTC, datetime, timedelta

    # TODO: Allow for customizing the prompt from the prompt interface.
    start_time_str = format_time_local(event.timestamp)
    end_time_str = format_time_local(event.timestamp + event.duration)
    # How long ago this AFK period ended, so the user knows how stale the prompt is
    # (with a warning symbol for periods older than stale_minutes).
    age_str = format_age(
        datetime.now(UTC) - (event.timestamp + event.duration),
        stale_threshold=timedelta(minutes=stale_minutes),
    )
    prompt_text = (
        f"What were you doing from {start_time_str} - {end_time_str} ({format_duration(event.duration)})?\n{age_str}"
    )
    title = "AFK Checkin"

    # Pass afk_start and afk_duration_seconds to enable Split button
    return aw_dialog.ask_string(
        title,
        prompt_text,
        [event.data.get(DATA_KEY, "") for event in recent_events],
        afk_start=event.timestamp,
        afk_duration_seconds=event.duration.total_seconds(),
        queue_info=queue_info,
    )


def prompt_ongoing(
    event: aw_core.Event,
    recent_events,
    still_afk_check=None,
    pending_count: int = 0,
) -> str | None:
    """Show a live-updating dialog for an AFK period that is still in progress.

    Unlike ``prompt()``, the end time is unknown so we show a ticking duration
    counter instead.  ``still_afk_check`` is polled so the dialog can notice the
    user returned (via the OS afk watcher) and freeze itself, even before they
    type anything.  The auto-snooze timeout is disabled while still away so the
    dialog persists until the user sits down.

    ``pending_count`` is the number of *earlier* unfilled AFK periods found by
    the deep scan; when nonzero the dialog warns that more answers are needed.
    """
    start_time_str = format_time_local(event.timestamp)
    prompt_text = f"What were you doing from {start_time_str}? (still AFK)"
    if pending_count:
        plural = "s" if pending_count != 1 else ""
        prompt_text += f"\n⚠️ {pending_count} earlier unfilled AFK period{plural} pending — asked next."
    return aw_dialog.ask_string(
        "AFK Checkin (ongoing)",
        prompt_text,
        [e.data.get(DATA_KEY, "") for e in recent_events],
        afk_start=event.timestamp,
        afk_duration_seconds=None,
        is_ongoing=True,
        still_afk_check=still_afk_check,
    )


def _deep_scan(state: AWAfkPromptClient, args) -> list[aw_core.Event]:
    """Run the full backfill-depth (e.g. 24h) lookup and return unfilled AFK periods.

    Uses a time-bounded fetch (``start_time``) so stale events from inactive
    buckets don't hide real gaps. Raises ConnectionError/HTTPError on server
    trouble so the caller can decide how to react.
    """
    from datetime import UTC, datetime, timedelta

    backfill_start = datetime.now(UTC) - timedelta(seconds=args.backfill_depth * 60)
    return list(
        state.get_new_afk_events_to_note(
            seconds=args.backfill_depth * 60,
            durration_thresh=args.length * 60,
            min_not_afk_duration=args.min_active,
            start_time=backfill_start,
        )
        or []
    )


def _process_events(
    state: AWAfkPromptClient, events: list[aw_core.Event], *, context: str, stale_minutes: float = 15.0
) -> bool:
    """Prompt the user for each unfilled AFK period, oldest first.

    Each prompt carries queue info ("(N of total) — next: …") so the user can see
    how many more periods remain to be backfilled. Split responses are posted as
    multiple activities, and normal responses are posted as a single event.

    A snoozed prompt (None) stops the queue: the remaining periods stay
    unanswered and will be re-found by the next deep scan. Returns True when the
    user snoozed, so the caller can suppress prompts for SNOOZE_SECONDS.
    """
    events = sorted(events, key=lambda e: e.timestamp)
    if not events:
        return False
    logger.info(f"{context}: {len(events)} unfilled AFK period(s) to prompt")
    for i, event in enumerate(events):
        response = prompt(
            event, state.state.recent_events, queue_info=_build_queue_info(events, i), stale_minutes=stale_minutes
        )
        if response is None:
            remaining = len(events) - i
            logger.info(
                f"Dialog snoozed for gap at "
                f"{format_time_local(event.timestamp)}-{format_time_local(event.timestamp + event.duration)} "
                f"({format_duration(event.duration)}) — suppressing prompts for {SNOOZE_SECONDS // 60} minutes, "
                f"{remaining} period(s) left for the next scan"
            )
            return True
        elif isinstance(response, tuple) and response[0] == "SPLIT_MODE":
            activities = response[1]
            logger.info(f"Posting {len(activities)} split activities")
            state.post_split_events(event, activities)
        else:
            logger.info(response)
            state.post_event(event, response)
    return False


def _post_ongoing_response(state: AWAfkPromptClient, ongoing: aw_core.Event, response, min_active: float = 0.0) -> None:
    """Dispatch the result of the live 'still AFK' dialog.

    The single-event path stamps the period as start..return, where the return
    time is taken from the afk watcher (start of the first not-afk event after
    the period began, ignoring blips shorter than ``min_active``). If that can't
    be determined, fall back to "now" — the user may have answered minutes after
    actually sitting down, so "now" can overstate the duration.
    Split results carry their own per-activity timestamps already.
    """
    if response is None:
        return
    from datetime import UTC, datetime

    if isinstance(response, tuple) and response[0] == "SPLIT_MODE":
        activities = response[1]
        logger.info(f"Posting {len(activities)} split activities")
        state.post_split_events(ongoing, activities)
    else:
        end = state.get_afk_period_end(ongoing.timestamp, min_active)
        if end is None or end <= ongoing.timestamp:
            end = datetime.now(UTC)
        actual_event = aw_core.Event(None, ongoing.timestamp, end - ongoing.timestamp)
        state.post_event(actual_event, response)


def _build_queue_info(events: list[aw_core.Event], index: int) -> dict | None:
    """Build queue info dict for the dialog when multiple AFK intervals are pending."""
    if len(events) <= 1:
        return None
    next_event = events[index + 1] if index + 1 < len(events) else None
    next_str = None
    if next_event:
        start = format_time_local(next_event.timestamp)
        end = format_time_local(next_event.timestamp + next_event.duration)
        next_str = f"{start}–{end} ({format_duration(next_event.duration)})"
    return {
        "position": index + 1,
        "total": len(events),
        "next_str": next_str,
    }


def parse_date(date_str: str):
    """Parse date string into start and end datetime."""
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    if date_str == "today":
        start = today
    elif date_str == "yesterday":
        start = today - timedelta(days=1)
    else:
        # Try to parse as YYYY-MM-DD
        try:
            start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            raise ValueError(f"Invalid date format: {date_str}. Use YYYY-MM-DD, 'today', or 'yesterday'.")

    end = start + timedelta(days=1)
    return start, end


def get_state_retries(
    client: ActivityWatchClient, enable_lid_events: bool = True, history_limit: int = 100
) -> AWAfkPromptClient:
    """When the computer is starting up sometimes the aw-server is not ready for requests yet.

    So we sit and retry for a while before giving up.
    """
    for _ in range(10):
        try:
            # This works because the constructor of AWAfkPromptState tries to get bucket names.
            # If it didn't we'd need to do something else here.
            return AWAfkPromptClient(client, enable_lid_events=enable_lid_events, history_limit=history_limit)
        except ConnectionError:
            logger.exception("Cannot connect to client.")
            time.sleep(10)  # 10 * 10 = wait for 100s before giving up.
    raise AWAfkPromptError("Could not get a connection to the server.")


def main() -> None:
    # Load config from file (falls back to defaults if file doesn't exist)
    config = load_config()

    parser = argparse.ArgumentParser()
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--depth",
        type=float,
        default=config.get("depth", 10),
        help="The number of minutes to look into the past for events. (default: from config or 10)",
    )
    parser.add_argument(
        "--frequency",
        type=float,
        default=config.get("frequency", 5),
        help="The number of seconds to wait before checking for AFK events again. (default: from config or 5)",
    )
    parser.add_argument(
        "--length",
        type=float,
        default=config.get("length", 5),
        help="The number of minutes you need to be away before reporting on it. (default: from config or 5)",
    )
    parser.add_argument("--testing", action="store_true", help="Run in testing mode.")
    parser.add_argument("--verbose", action="store_true", help="I want to see EVERYTHING!")
    parser.add_argument(
        "--test-dialog",
        action="store_true",
        help="Show test dialog immediately (for UI testing without AFK period).",
    )
    parser.add_argument(
        "--test-dialog-duration",
        type=float,
        default=30,
        help="Duration in minutes for test dialog (default: 30).",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=config.get("history_limit", 100),
        help="Number of events to fetch from each bucket (default: from config or 100).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        default=config.get("enable_backfill", True),
        help="Enable backfill mode - prompt for old unfilled AFK periods.",
    )
    parser.add_argument(
        "--backfill-depth",
        type=float,
        default=config.get("backfill_depth", 1440),
        help="How far back (in minutes) to look for unfilled AFK periods (default: 1440 = 24h).",
    )
    parser.add_argument(
        "--backfill-interval",
        type=float,
        default=config.get("backfill_interval", 10),
        help="How often (in minutes) to repeat the full backfill-depth scan during normal "
        "operation (default: from config or 10).",
    )
    parser.add_argument(
        "--stale-warning",
        type=float,
        default=config.get("stale_warning", 15),
        help="AFK periods older than this (in minutes) get a warning symbol in the prompt "
        "(default: from config or 15).",
    )
    parser.add_argument(
        "--min-active",
        type=float,
        default=config.get("min_active", 0.0),
        help="Minimum seconds of not-afk activity to count as real (default: 0 = disabled). "
        "Shorter events are ignored so brief touches don't split a long AFK period.",
    )
    parser.add_argument(
        "--edit",
        action="store_true",
        help="Edit mode - review and edit past entries, then exit.",
    )
    parser.add_argument(
        "--edit-date",
        type=str,
        default="today",
        help="Date to edit entries for (default: today). Format: YYYY-MM-DD or 'today', 'yesterday'.",
    )
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help="Run backfill for unfilled AFK periods, then exit (do not start polling loop).",
    )
    args = parser.parse_args()

    # Set up logging
    setup_logging(
        WATCHER_NAME,
        testing=args.testing,
        verbose=args.verbose,
        log_stderr=True,
        log_file=True,
    )

    # Test dialog mode - show dialog immediately for UI testing
    if args.test_dialog:
        from datetime import UTC, datetime, timedelta

        import aw_watcher_afk_prompt.dialog as aw_dialog

        # Create test AFK event data
        test_start = datetime.now(UTC) - timedelta(minutes=args.test_dialog_duration)
        test_duration_seconds = args.test_dialog_duration * 60

        start_time_str = format_time_local(test_start)
        end_time_str = format_time_local(test_start + timedelta(seconds=test_duration_seconds))
        test_prompt = (
            f"What were you doing from {start_time_str} - {end_time_str} ({format_duration(test_duration_seconds)})?"
        )
        title = "AFK Checkin (TEST MODE)"

        # Show dialog with split mode support
        result = aw_dialog.ask_string(
            title,
            test_prompt,
            history=["test1", "test2", "lunch", "meeting"],
            afk_start=test_start,
            afk_duration_seconds=test_duration_seconds,
        )

        logger.info("Test dialog closed, processing result...")

        if result is None:
            logger.info("Test dialog cancelled")
        elif isinstance(result, tuple) and result[0] == "SPLIT_MODE":
            activities = result[1]
            logger.info(f"Test dialog returned {len(activities)} activities:")
            for i, activity in enumerate(activities, 1):
                logger.info(
                    f"  {i}. '{activity.description}' - "
                    f"{activity.start_time.strftime('%H:%M:%S')} - "
                    f"{activity.duration_minutes}m {activity.duration_seconds}s"
                )
        else:
            logger.info(f"Test dialog result: '{result}'")

        logger.info("Exiting test dialog mode")
        # Exit after showing test dialog
        return

    # Edit mode - review and edit past entries
    if args.edit:
        from datetime import UTC, datetime

        import aw_transform

        from aw_watcher_afk_prompt.dialog import ask_batch_edit

        try:
            start_date, end_date = parse_date(args.edit_date)
        except ValueError as e:
            logger.error(str(e))
            return

        logger.info(f"Edit mode: reviewing entries from {args.edit_date}")

        try:
            client = ActivityWatchClient(client_name=WATCHER_NAME + "_edit", testing=args.testing)
            with client:
                bucket_id = f"{WATCHER_NAME}_{client.client_hostname}"

                # Fetch events for the date range
                events = client.get_events(bucket_id, limit=1000, start=start_date, end=end_date)
                events = aw_transform.sort_by_timestamp(events)

                if not events:
                    logger.info(f"No entries found for {args.edit_date}")
                    return

                logger.info(f"Found {len(events)} entries to review")

                # Show batch edit dialog
                title = f"Edit Entries - {args.edit_date}"
                result = ask_batch_edit(title, events, format_time_local)

                if result is None:
                    logger.info("Edit cancelled")
                    return

                # Process changes
                edited_count = 0
                for event, new_value in result:
                    current_msg = event.data.get(DATA_KEY, "")
                    if new_value != current_msg:
                        event.data[DATA_KEY] = new_value
                        client.insert_event(bucket_id, event)
                        logger.info(f"Updated: '{current_msg}' -> '{new_value}'")
                        edited_count += 1

                logger.info(f"Edit complete: {edited_count} entries updated")

        except Exception as e:
            logger.error(f"Edit mode error: {e}")
            raise

        return

    # backfill-only uses a distinct client name to avoid the single-instance lock
    # when the daemon is already running.
    effective_client_name = WATCHER_NAME + "_backfill" if args.backfill_only else WATCHER_NAME
    try:
        client = ActivityWatchClient(  # pyright: ignore[reportPrivateImportUsage]
            client_name=effective_client_name, testing=args.testing
        )
        with client:
            state = get_state_retries(
                client, enable_lid_events=config.get("enable_lid_events", True), history_limit=args.history_limit
            )
            logger.info("Successfully connected to the server.")

            # Backfill mode: on startup, prompt for old unfilled AFK periods.
            # last_deep_scan tracks when we last did a full backfill-depth lookup so the
            # normal loop can repeat it periodically (see DEEP_SCAN_INTERVAL_SECONDS).
            # snoozed_until: no prompts before this time (the watcher keeps scanning).
            last_deep_scan = 0.0
            snoozed_until = 0.0
            if args.backfill:
                logger.info(f"Backfill mode enabled, looking back {args.backfill_depth} minutes")
                try:
                    backfill_events = _deep_scan(state, args)
                except (ConnectionError, HTTPError) as e:
                    logger.warning(f"Backfill failed due to server error: {e}")
                    backfill_events = []
                if _process_events(
                    state, backfill_events, context="Startup backfill", stale_minutes=args.stale_warning
                ):
                    snoozed_until = time.monotonic() + SNOOZE_SECONDS
                last_deep_scan = time.monotonic()

            if args.backfill_only:
                logger.info("--backfill-only: exiting after backfill")
                return

            # Normal operation loop
            deep_scan_interval = args.backfill_interval * 60  # config is in minutes
            server_down_since = None
            server_down_notified = False
            # Track which ongoing AFK period we've already shown a pre-emptive dialog for,
            # identified by its start timestamp. Reset when a new AFK period starts.
            prompted_ongoing_start = None
            while True:
                try:
                    # Shallow real-time scan (small depth window) for responsiveness:
                    # catches a just-finished AFK period within one poll interval.
                    shallow = list(
                        state.get_new_afk_events_to_note(
                            seconds=args.depth * 60,
                            durration_thresh=args.length * 60,
                            min_not_afk_duration=args.min_active,
                        )
                    )

                    prompts_allowed = time.monotonic() >= snoozed_until

                    if not shallow and prompts_allowed:
                        # Still AFK (or nothing to do) — check if we should show a
                        # pre-emptive live dialog before the user sits back down.
                        ongoing = state.get_ongoing_afk_event(args.length * 60)
                        if ongoing and ongoing.timestamp != prompted_ongoing_start:
                            # Verify against the full backfill window first, so the
                            # dialog can warn about earlier periods awaiting answers.
                            pending_count = 0
                            if args.backfill:
                                try:
                                    pending_count = len(_deep_scan(state, args))
                                except (ConnectionError, HTTPError) as e:
                                    logger.warning(f"Pending-period check failed: {e}")
                            prompted_ongoing_start = ongoing.timestamp
                            response = prompt_ongoing(
                                ongoing,
                                state.state.recent_events,
                                still_afk_check=lambda: state.get_ongoing_afk_event(args.length * 60) is not None,
                                pending_count=pending_count,
                            )
                            _post_ongoing_response(state, ongoing, response, min_active=args.min_active)
                            if response is None:
                                snoozed_until = time.monotonic() + SNOOZE_SECONDS
                                prompts_allowed = False
                            # The dialog may have been open for a long time; force a
                            # deep scan so periods that finished meanwhile (or were
                            # pending already) are prompted right away.
                            last_deep_scan = 0.0

                    # Trigger a full deep (backfill-depth) scan either on a ~10-minute
                    # cadence or immediately before prompting (when the shallow scan found
                    # something). The deep scan is the authoritative, correctly-ordered
                    # list we actually prompt from, so periods that slipped out of the
                    # shallow window are picked up without waiting for a restart.
                    due_for_deep = (time.monotonic() - last_deep_scan) >= deep_scan_interval
                    if prompts_allowed:
                        if args.backfill and (shallow or due_for_deep):
                            pending = _deep_scan(state, args)
                            last_deep_scan = time.monotonic()
                            snoozed = _process_events(
                                state, pending, context="Backfill scan", stale_minutes=args.stale_warning
                            )
                        else:
                            snoozed = _process_events(
                                state, shallow, context="AFK check", stale_minutes=args.stale_warning
                            )
                        if snoozed:
                            snoozed_until = time.monotonic() + SNOOZE_SECONDS
                    # Poll succeeded — reset server down tracking
                    if server_down_since is not None:
                        logger.info("Server connection restored.")
                    server_down_since = None
                    server_down_notified = False
                except (ConnectionError, HTTPError) as e:
                    if server_down_since is None:
                        server_down_since = time.monotonic()
                        logger.warning(f"Server connection error: {e}")
                    else:
                        logger.debug(f"Server still unreachable: {e}")
                    down_duration = time.monotonic() - server_down_since
                    if down_duration >= 300 and not server_down_notified:
                        server_down_notified = True
                        logger.error(f"Server has been unreachable for {int(down_duration // 60)} minutes")
                        messagebox.showwarning(
                            "AW Watcher AFK Prompt: Server Unreachable",
                            "The ActivityWatch server has been unreachable for over "
                            "5 minutes.\n\n"
                            "AFK periods during this time will not be tracked.\n\n"
                            "Please check if aw-server is running.",
                        )
                time.sleep(args.frequency)
    except Exception as e:
        messagebox.showerror("AW Watcher Ask Away: Error", f"An unhandled exception occurred: {e}")
        raise


if __name__ == "__main__":
    main()
