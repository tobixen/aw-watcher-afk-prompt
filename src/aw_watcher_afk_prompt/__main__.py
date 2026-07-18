# ruff: noqa: EM101, EM102
import argparse
import time
from collections.abc import Callable, Iterable
from tkinter import messagebox
from typing import NamedTuple

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

# After the user snoozes a dialog (Snooze button, Escape, empty Enter, or
# closing the window), no new prompts are shown for this long. The watcher
# keeps scanning in the meantime, so nothing is lost — just not asked about yet.
SNOOZE_SECONDS = 300


def _make_prompt_text(event: aw_core.Event, stale_minutes: float) -> str:
    """The question for a completed AFK period, including how stale it is.

    Recomputed rather than stored, so a dialog that sits unanswered can refresh
    the age instead of insisting the period ended "2 minutes ago" an hour later.
    """
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
    return f"What were you doing from {start_time_str} - {end_time_str} ({format_duration(event.duration)})?\n{age_str}"


def _make_refresh(
    event: aw_core.Event,
    *,
    answered: int,
    stale_minutes: float,
    rescan: Callable[[], list[aw_core.Event]] | None = None,
    ongoing_check: Callable[[], bool] | None = None,
) -> Callable[[], dict]:
    """Build the callback an open dialog uses to keep itself up to date.

    Returns a dict of what changed: the prompt text (ageing) and, when a rescan
    is available, the recounted queue info — so leaving and returning a couple of
    times while a dialog waits turns "(1 of 1)" into "(1 of 3)" instead of
    surfacing later as stand-alone surprise prompts.

    A recount that fails against the server omits "queue_info" entirely: the
    dialog then keeps the count it has, rather than showing a made-up one.
    """

    def refresh() -> dict:
        update: dict = {"prompt": _make_prompt_text(event, stale_minutes)}
        if rescan is None:
            return update
        try:
            remaining = sorted(rescan(), key=lambda e: e.timestamp)
        except (ConnectionError, HTTPError) as e:
            logger.debug(f"Live queue recount failed, keeping the current count: {e}")
            return update
        # The period being prompted is unanswered by definition, so it counts even
        # if the scan window no longer reports it.
        if not any(e.timestamp == event.timestamp for e in remaining):
            remaining = [event, *remaining]
        ongoing = False
        if ongoing_check is not None:
            try:
                ongoing = bool(ongoing_check())
            except (ConnectionError, HTTPError) as e:
                logger.debug(f"Live ongoing-period check failed: {e}")
        update["queue_info"] = _build_queue_info(remaining, answered, ongoing)
        return update

    return refresh


def prompt(
    event: aw_core.Event,
    recent_events: Iterable[aw_core.Event],
    queue_info: dict | None = None,
    stale_minutes: float = 15.0,
    refresh: Callable[[], dict] | None = None,
) -> str | None:
    title = "AFK Checkin"

    # Pass afk_start and afk_duration_seconds to enable Split button
    return aw_dialog.ask_string(
        title,
        _make_prompt_text(event, stale_minutes),
        [event.data.get(DATA_KEY, "") for event in recent_events],
        afk_start=event.timestamp,
        afk_duration_seconds=event.duration.total_seconds(),
        queue_info=queue_info,
        refresh=refresh,
    )


def prompt_ongoing(
    event: aw_core.Event,
    recent_events,
    still_afk_check=None,
    refresh: Callable[[], dict] | None = None,
) -> str | None:
    """Show a live-updating dialog for an AFK period that is still in progress.

    Unlike ``prompt()``, the end time is unknown so we show a ticking duration
    counter instead.  ``still_afk_check`` is polled so the dialog can notice the
    user returned (via the OS afk watcher) and freeze itself, even before they
    type anything.

    This dialog is only shown once no *earlier* completed unfilled periods remain
    (those are prompted oldest-first beforehand — see ``_handle_still_afk``), so
    it always represents the most recent, still-running period.
    """
    start_time_str = format_time_local(event.timestamp)
    prompt_text = f"What were you doing from {start_time_str}? (still AFK)"
    return aw_dialog.ask_string(
        "AFK Checkin (ongoing)",
        prompt_text,
        [e.data.get(DATA_KEY, "") for e in recent_events],
        afk_start=event.timestamp,
        afk_duration_seconds=None,
        is_ongoing=True,
        still_afk_check=still_afk_check,
        refresh=refresh,
    )


def _deep_scan(state: AWAfkPromptClient, args, while_afk: bool = False) -> list[aw_core.Event]:
    """Run the full backfill-depth (e.g. 24h) lookup and return unfilled AFK periods.

    Uses a time-bounded fetch (``start_time``) so stale events from inactive
    buckets don't hide real gaps. Raises ConnectionError/HTTPError on server
    trouble so the caller can decide how to react.

    ``while_afk=True`` is for the still-AFK path: the scan then returns earlier
    *completed* unfilled gaps even though the user is currently AFK (by default
    the scan yields nothing in that situation).
    """
    from datetime import UTC, datetime, timedelta

    backfill_start = datetime.now(UTC) - timedelta(seconds=args.backfill_depth * 60)
    return list(
        state.get_new_afk_events_to_note(
            seconds=args.backfill_depth * 60,
            durration_thresh=args.length * 60,
            min_not_afk_duration=args.min_active,
            start_time=backfill_start,
            include_while_afk=while_afk,
        )
        or []
    )


def _rescan_hook(state: AWAfkPromptClient, args) -> Callable[[], list[aw_core.Event]]:
    """Queue-refresh hook: repeat the full backfill-depth scan.

    Always in while-afk mode. The usual reason a dialog sits unanswered is that
    nobody is at the keyboard, and a scan that bails out on "currently AFK" would
    then report an empty queue — blanking the count in the open dialog and
    dropping the rest of the queue between prompts.
    """
    return lambda: _deep_scan(state, args, while_afk=True)


def _ongoing_check(state: AWAfkPromptClient, args) -> Callable[[], bool]:
    """Is an AFK period running right now?

    Used both to count the still-running period into queue totals and to tell an
    open dialog whether there is anyone there to see it.
    """
    return lambda: state.get_ongoing_afk_event(args.length * 60) is not None


def _process_events(
    state: AWAfkPromptClient,
    events: list[aw_core.Event],
    *,
    context: str,
    stale_minutes: float = 15.0,
    rescan: Callable[[], list[aw_core.Event]] | None = None,
    ongoing_check: Callable[[], bool] | None = None,
) -> bool:
    """Prompt the user for each unfilled AFK period, oldest first.

    Each prompt carries queue info ("(N of total) — next: …") so the user can see
    how many more periods remain to be backfilled. The queue is not a frozen
    snapshot: after every answer it is refreshed via ``rescan``, so a period
    that completed while a dialog sat unanswered joins the same queue run
    instead of surfacing minutes later as a stand-alone surprise prompt. When
    ``ongoing_check`` reports a still-running AFK period, it is counted into the
    total as well, so a lone completed gap announces "(1 of 2)" rather than
    pretending to be the only open interval.

    The same refresh happens *while* a dialog is open (see ``_make_refresh``), so
    an unanswered prompt updates its own age and count instead of going stale.

    Split responses are posted as multiple activities, and normal responses are
    posted as a single event.

    A snoozed prompt (None) stops the queue: the remaining periods stay
    unanswered and will be re-found by the next deep scan. Returns True when the
    user snoozed, so the caller can suppress prompts for SNOOZE_SECONDS.
    """
    queue = sorted(events, key=lambda e: e.timestamp)
    if not queue:
        return False
    logger.info(f"{context}: {len(queue)} unfilled AFK period(s) to prompt")
    answered = 0
    while queue:
        event = queue[0]
        ongoing = False
        if ongoing_check is not None:
            try:
                ongoing = bool(ongoing_check())
            except (ConnectionError, HTTPError) as e:
                logger.warning(f"Ongoing-period check failed: {e}")
        response = prompt(
            event,
            state.state.recent_events,
            queue_info=_build_queue_info(queue, answered, ongoing),
            stale_minutes=stale_minutes,
            refresh=_make_refresh(
                event,
                answered=answered,
                stale_minutes=stale_minutes,
                rescan=rescan,
                ongoing_check=ongoing_check,
            ),
        )
        if response is None:
            logger.info(
                f"Dialog snoozed for gap at "
                f"{format_time_local(event.timestamp)}-{format_time_local(event.timestamp + event.duration)} "
                f"({format_duration(event.duration)}) — suppressing prompts for {SNOOZE_SECONDS // 60} minutes, "
                f"{len(queue)} period(s) left for the next scan"
            )
            return True
        elif isinstance(response, tuple) and response[0] == "SPLIT_MODE":
            activities = response[1]
            logger.info(f"Posting {len(activities)} split activities")
            state.post_split_events(event, activities)
        else:
            logger.info(response)
            state.post_event(event, response)
        answered += 1
        queue = queue[1:]
        if rescan is not None:
            try:
                refreshed = sorted(rescan(), key=lambda e: e.timestamp)
            except (ConnectionError, HTTPError) as e:
                logger.warning(f"Queue refresh failed, keeping current queue: {e}")
            else:
                if len(refreshed) != len(queue):
                    logger.info(f"{context}: queue refreshed, {len(refreshed)} period(s) now pending")
                queue = refreshed
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


class _StillAfkResult(NamedTuple):
    """Outcome of handling a still-AFK poll (see ``_handle_still_afk``)."""

    prompted_ongoing_start: object  # datetime | None — the new "already shown" marker
    snoozed: bool
    # What the caller should do with last_deep_scan afterwards:
    #   "now"   -> set to time.monotonic() (we just ran a fresh deep scan here)
    #   "reset" -> set to 0.0 (force a deep scan next loop; the live dialog may
    #              have been open a long time, so periods finished meanwhile)
    #   "keep"  -> leave unchanged (nothing happened this poll)
    deep_scan: str


def _handle_still_afk(state: AWAfkPromptClient, args, prompted_ongoing_start) -> _StillAfkResult:
    """Decide what to prompt while the user is still AFK (shallow scan empty).

    Earlier *completed* unfilled periods are asked about oldest-first, before the
    live 'still AFK' dialog for the just-started ongoing period — so the user
    always answers the oldest interval first rather than the most recent one.
    The deep scan runs with ``while_afk=True`` since the user is currently AFK;
    it never includes the still-ongoing period (gap detection needs a not-afk
    event on both sides), so any results it gives are earlier, completed gaps.

    The live ongoing dialog is shown only once no earlier completed periods
    remain. While such periods are pending we leave ``prompted_ongoing_start``
    untouched, so the live dialog still appears on a later poll after they have
    been cleared.
    """
    ongoing = state.get_ongoing_afk_event(args.length * 60)
    if ongoing is None or ongoing.timestamp == prompted_ongoing_start:
        # Not AFK long enough, or we've already shown the live dialog for this period.
        return _StillAfkResult(prompted_ongoing_start, snoozed=False, deep_scan="keep")

    pending: list[aw_core.Event] = []
    if args.backfill:
        try:
            pending = _deep_scan(state, args, while_afk=True)
        except (ConnectionError, HTTPError) as e:
            logger.warning(f"Pending-period check failed: {e}")

    def ongoing_queue_refresh() -> dict:
        """Recount what is waiting behind the live dialog while it sits open.

        The live dialog is the one on screen while the user is away, so this is
        where "(1 of 3)" has to appear when periods pile up behind it. The ongoing
        period is the one being answered, hence first; the completed gaps follow.
        """
        try:
            others = sorted(_rescan_hook(state, args)(), key=lambda e: e.timestamp)
        except (ConnectionError, HTTPError) as e:
            logger.debug(f"Live queue recount failed, keeping the current count: {e}")
            return {}
        return {"queue_info": _build_queue_info([ongoing, *others])}

    if pending:
        # Ask about the earlier completed periods first, oldest-first. The queue
        # counts the ongoing period and refreshes after every answer (and while a
        # dialog is open), so any period completing while a dialog sits open joins
        # this same run.
        snoozed = _process_events(
            state,
            pending,
            context="Still-AFK backfill",
            stale_minutes=args.stale_warning,
            rescan=_rescan_hook(state, args),
            ongoing_check=_ongoing_check(state, args),
        )
        return _StillAfkResult(prompted_ongoing_start, snoozed=snoozed, deep_scan="now")

    # Nothing earlier outstanding — show the live, self-updating ongoing dialog.
    response = prompt_ongoing(
        ongoing,
        state.state.recent_events,
        still_afk_check=_ongoing_check(state, args),
        refresh=ongoing_queue_refresh if args.backfill else None,
    )
    _post_ongoing_response(state, ongoing, response, min_active=args.min_active)
    return _StillAfkResult(ongoing.timestamp, snoozed=response is None, deep_scan="reset")


def _build_queue_info(remaining: list[aw_core.Event], answered: int = 0, ongoing: bool = False) -> dict | None:
    """Build queue info dict for the dialog when multiple AFK intervals are open.

    ``remaining`` is the not-yet-answered queue (the event being prompted
    first), ``answered`` how many were already answered in this queue run, and
    ``ongoing`` whether the current, still-running AFK period will need an
    answer too — it counts into the total and is announced as the final "next".
    """
    total = answered + len(remaining) + (1 if ongoing else 0)
    if total <= 1:
        return None
    next_event = remaining[1] if len(remaining) > 1 else None
    if next_event is not None:
        start = format_time_local(next_event.timestamp)
        end = format_time_local(next_event.timestamp + next_event.duration)
        next_str = f"{start}–{end} ({format_duration(next_event.duration)})"
    elif ongoing:
        next_str = "the current AFK period (still ongoing)"
    else:
        next_str = None
    return {
        "position": answered + 1,
        "total": total,
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

            rescan_deep = _rescan_hook(state, args)
            ongoing_check = _ongoing_check(state, args)

            if args.backfill:
                logger.info(f"Backfill mode enabled, looking back {args.backfill_depth} minutes")
                try:
                    backfill_events = _deep_scan(state, args)
                except (ConnectionError, HTTPError) as e:
                    logger.warning(f"Backfill failed due to server error: {e}")
                    backfill_events = []
                if _process_events(
                    state,
                    backfill_events,
                    context="Startup backfill",
                    stale_minutes=args.stale_warning,
                    rescan=rescan_deep,
                    ongoing_check=ongoing_check,
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
                        # Still AFK (or nothing to do): ask about earlier completed
                        # unfilled periods oldest-first, then show a pre-emptive live
                        # dialog for the just-started period before the user sits back
                        # down (see _handle_still_afk).
                        result = _handle_still_afk(state, args, prompted_ongoing_start)
                        prompted_ongoing_start = result.prompted_ongoing_start
                        if result.snoozed:
                            snoozed_until = time.monotonic() + SNOOZE_SECONDS
                            prompts_allowed = False
                        if result.deep_scan == "reset":
                            last_deep_scan = 0.0
                        elif result.deep_scan == "now":
                            last_deep_scan = time.monotonic()

                    # Trigger a full deep (backfill-depth) scan either on a ~10-minute
                    # cadence or immediately before prompting (when the shallow scan found
                    # something). The deep scan is the authoritative, correctly-ordered
                    # list we actually prompt from, so periods that slipped out of the
                    # shallow window are picked up without waiting for a restart.
                    due_for_deep = (time.monotonic() - last_deep_scan) >= deep_scan_interval
                    if prompts_allowed:
                        if args.backfill and (shallow or due_for_deep):
                            pending = _deep_scan(state, args)
                            snoozed = _process_events(
                                state,
                                pending,
                                context="Backfill scan",
                                stale_minutes=args.stale_warning,
                                rescan=rescan_deep,
                                ongoing_check=ongoing_check,
                            )
                            # After the queue run: its rescans kept the scan fresh
                            # throughout, however long the dialogs sat open.
                            last_deep_scan = time.monotonic()
                        else:
                            snoozed = _process_events(
                                state,
                                shallow,
                                context="AFK check",
                                stale_minutes=args.stale_warning,
                                ongoing_check=ongoing_check,
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
