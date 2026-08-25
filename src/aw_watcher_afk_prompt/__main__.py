# ruff: noqa: EM101, EM102
import argparse
import datetime
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
    get_utc_now,
    logger,
)
from aw_watcher_afk_prompt.utils import format_age, format_duration, format_time_local

# After the user snoozes a dialog (Snooze button, Escape, empty Enter, or
# closing the window), no new prompts are shown for this long. The watcher
# keeps scanning in the meantime, so nothing is lost — just not asked about yet.
SNOOZE_SECONDS = 300

# How far the reported start of the ongoing AFK period may wobble between fetches
# and still count as the same period (the server sometimes returns duplicated,
# millisecond-offset events). A real return-and-leave-again moves it by minutes.
_SAME_PERIOD_TOLERANCE_SECONDS = 5.0


# A suspend of at least this long counts as one worth re-checking the feed after.
# time.monotonic() stops while the machine is suspended and the wall clock does
# not, so the two drifting apart between polls *is* the resume signal.
RESUME_DETECTION_SECONDS = 60.0


class _Clocks(NamedTuple):
    """The pair of clocks whose disagreement reveals a suspend."""

    mono: float
    wall: datetime.datetime

    @classmethod
    def now(cls) -> "_Clocks":
        return cls(time.monotonic(), get_utc_now())


def _resumed(before: _Clocks, after: _Clocks, threshold: float = RESUME_DETECTION_SECONDS) -> bool:
    """Did the machine suspend between these two polls?"""
    return (after.wall - before.wall).total_seconds() - (after.mono - before.mono) > threshold


class _FeedState(NamedTuple):
    """What we know between polls about whether the AFK feed is still alive.

    ``anchor`` is a moment when the feed *had* to speak — this watcher starting,
    or the machine resuming — held until the feed proves it did. ``notified`` is
    sticky until the feed reports again, so one death raises one dialog.
    """

    anchor: datetime.datetime | None
    why: str = ""
    notified: bool = False

    @classmethod
    def disarmed(cls) -> "_FeedState":
        return cls(None)

    @classmethod
    def armed(cls, stale_after_minutes: float, why: str, at: datetime.datetime | None = None) -> "_FeedState":
        if not stale_after_minutes or stale_after_minutes <= 0:
            return cls.disarmed()
        return cls(get_utc_now() if at is None else at, why)


def _check_afk_feed(
    state: AWAfkPromptClient,
    stale_after_minutes: float,
    feed: _FeedState,
    now: datetime.datetime | None = None,
) -> _FeedState:
    """Notice that the AFK feed has died, and say so once, in a dialog.

    Every prompt this watcher makes comes from a gap *between* not-afk events, so
    a dead afk watcher leaves this one silently useless: it keeps polling, finds
    nothing to ask about however long the user is away, and logs nothing at all.
    Observed in the wild as a night in bed with no prompt in the morning, the feed
    having died at a reboot 20 hours earlier.

    Silence on its own cannot be the trigger. The feed says nothing whatsoever
    while the user is away — that is what an absence *is* — so "no events for N
    minutes" describes every lunch break as accurately as every dead watcher, and
    since the dialog blocks this loop until dismissed, a false one costs real
    prompts. What is evidence is silence across an *anchor*: a moment when
    something other than the feed says the machine is up and a human is involved,
    so that a live feed would have reported within a heartbeat. Four of them:

    - this watcher starting up, or the machine resuming from suspend (_resumed),
    - the lid watcher seeing someone arrive — a separate process, so its word is
      independent of the feed,
    - the user answering a prompt, which is somebody demonstrably at the keyboard,
    - another watcher (browser, editor, terminal — see ``presence_buckets``)
      reporting activity the feed did not notice.

    All but the first also catch a feed that died mid-session.

    An anchor convicts the feed when the feed said nothing across it, and when
    enough time has passed that a live feed would have spoken. What counts as
    "enough" differs by kind. The first three are *arrivals* — single moments —
    so they wait ``stale_after_minutes`` from the moment itself. A presence bucket
    is a running commentary instead, whose newest event is always near now, so
    waiting from it would mean waiting forever; there the feed's debt is measured
    against the commentary: activity continuing ``stale_after_minutes`` past the
    feed's last word is what convicts.

    Events from up to ``stale_after_minutes`` *before* an anchor still count as
    proof of life: a feed that reported ten minutes ago has not died, it just has
    nothing to say — without that margin, restarting the watcher and immediately
    walking away would raise an alarm.
    """
    if not stale_after_minutes or stale_after_minutes <= 0:
        return _FeedState.disarmed()
    now = get_utc_now() if now is None else now
    grace = datetime.timedelta(minutes=stale_after_minutes)

    last_seen = state.get_feed_last_seen()
    if last_seen is not None and now - last_seen < grace:
        # The feed is talking. Nothing to corroborate, nothing to warn about, and
        # no other bucket worth querying — this runs on every poll.
        logger.debug(f"AFK feed is alive (last event {format_time_local(last_seen)}).")
        return _FeedState.disarmed()
    if feed.notified:
        # Already said, and only the feed itself can withdraw it. Checked before
        # the corroborating queries below rather than after: a genuinely dead feed
        # would otherwise have us interrogating every other bucket every poll, for
        # as long as it stays dead.
        return feed

    # (moment, why, has the feed had long enough to answer for itself)
    arrivals = [(feed.anchor, feed.why)] if feed.anchor is not None else []
    lid_at = state.get_lid_presence_last_seen()
    if lid_at is not None:
        arrivals.append((lid_at, "the lid watcher saw you arrive"))
    if state.last_answer_at is not None:
        arrivals.append((state.last_answer_at, "you answered a prompt"))
    anchors = [(at, why, now - at >= grace) for at, why in arrivals]

    presence_at = state.get_presence_last_seen()
    if presence_at is not None:
        # The feed's debt is measured against the commentary — except with nothing
        # in the feed at all to measure against, where the commentary is treated
        # as an arrival so that it too waits out the grace period. Convicting a
        # feed on its first poll, alone among the anchors, was an oversight.
        overdue = (presence_at - last_seen if last_seen is not None else now - presence_at) >= grace
        anchors.append((presence_at, "another watcher saw you working", overdue))
    if not anchors:
        return feed

    newest = max(at for at, _, _ in anchors)
    if last_seen is not None and last_seen >= newest - grace:
        # Quiet since, but it did speak after the anchor, so its silence is an
        # absence rather than a death.
        logger.debug(f"AFK feed answered for itself (last event {format_time_local(last_seen)}).")
        return _FeedState.disarmed()

    # An anchor only convicts on its own evidence: an older one the feed did
    # answer for proves nothing about a newer silence, or the other way round.
    convicting = [(at, why) for at, why, ready in anchors if ready and (last_seen is None or last_seen < at - grace)]
    if not convicting:
        return feed  # the feed still has time to answer for itself
    anchor, why = max(convicting, key=lambda a: a[0])

    if last_seen is None:
        silence = "has never reported anything"
    else:
        # Age, not just a wall-clock time: "last reported 14:04" reads as a time
        # today, which for a feed that died yesterday is actively misleading.
        silence = f"last reported {format_duration(now - last_seen)} ago, at {format_time_local(last_seen)}"
    logger.error(f"AFK feed looks dead: {state.afk_bucket_id} {silence}, and {why}.")
    messagebox.showwarning(
        "AW Watcher AFK Prompt: No AFK data",
        f"The AFK feed has stopped: {state.afk_bucket_id} {silence}, and {why}.\n\n"
        "AFK periods are found in that feed, so nothing will be asked about "
        "while it is stopped — however long you are away.\n\n"
        "Check that the watcher writing that bucket is still running.",
    )
    return feed._replace(notified=True)


def _timeout_ms(minutes: float | None) -> int | None:
    """Convert a prompt-timeout setting in minutes to ms.

    None, 0 and negative values all mean "no timeout" — a negative Tk ``after``
    delay fires immediately, which would auto-snooze every dialog on sight.
    """
    if not minutes or minutes < 0:
        return None
    return int(minutes * 60_000)


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
    timeout_ms: int | None = None,
    still_afk_check: Callable[[], bool] | None = None,
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
        timeout_ms=timeout_ms,
        still_afk_check=still_afk_check,
    )


def prompt_ongoing(
    event: aw_core.Event,
    recent_events,
    still_afk_check=None,
    timeout_ms: int | None = None,
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

    ``timeout_ms`` is honoured only after the user has returned: hiding a dialog
    the user never had a chance to see would defeat its purpose.
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
        timeout_ms=timeout_ms,
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
    timeout_ms: int | None = None,
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
    an unanswered prompt updates its own age and count instead of going stale, and
    ``timeout_ms`` hides it after a while so it can be re-raised rather than lost
    behind other windows.

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
            timeout_ms=timeout_ms,
            # "Is an AFK period running?" doubles as "is the user away?", which the
            # dialog needs so it doesn't count down while nobody can see it.
            still_afk_check=ongoing_check,
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

    def still_in_this_afk_period() -> bool:
        """True while the *same* AFK period is still running.

        Comparing start timestamps matters: a brief touch ends this period and
        starts a new one, and a plain "is the user AFK now?" check would answer
        yes — leaving the live dialog ticking as though a ten-minute absence had
        been hours of continuous AFK time.

        Compared with a tolerance, not exactly: the server is known to return
        duplicated, millisecond-offset afk events (which is why ``has_event``
        compares with an overlap ratio), and a start that wobbles by a millisecond
        must not be read as the user coming back — that would freeze the counter
        and suppress the dialog for the rest of the period. A real touch moves the
        start by at least ``--length``, far beyond this tolerance.
        """
        current = state.get_ongoing_afk_event(args.length * 60)
        if current is None:
            return False
        drift = abs((current.timestamp - ongoing.timestamp).total_seconds())
        return drift <= _SAME_PERIOD_TOLERANCE_SECONDS

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
            timeout_ms=_timeout_ms(args.prompt_timeout),
        )
        return _StillAfkResult(prompted_ongoing_start, snoozed=snoozed, deep_scan="now")

    # Nothing earlier outstanding — show the live, self-updating ongoing dialog.
    response = prompt_ongoing(
        ongoing,
        state.state.recent_events,
        still_afk_check=still_in_this_afk_period,
        timeout_ms=_timeout_ms(args.prompt_timeout),
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
    client: ActivityWatchClient,
    enable_lid_events: bool = True,
    history_limit: int = 100,
    presence_buckets: Iterable[str] = (),
    presence_timeout: float = 300,
) -> AWAfkPromptClient:
    """When the computer is starting up sometimes the aw-server is not ready for requests yet.

    So we sit and retry for a while before giving up.
    """
    for _ in range(10):
        try:
            # This works because the constructor of AWAfkPromptState tries to get bucket names.
            # If it didn't we'd need to do something else here.
            return AWAfkPromptClient(
                client,
                enable_lid_events=enable_lid_events,
                history_limit=history_limit,
                presence_buckets=presence_buckets,
                presence_timeout=presence_timeout,
            )
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
        "--prompt-timeout",
        type=float,
        default=config.get("prompt_timeout", 5),
        help="Hide an unanswered prompt after this many minutes and re-ask later, so a "
        "dialog buried under other windows isn't forgotten (default: from config or 5; 0 disables).",
    )
    parser.add_argument(
        "--feed-stale",
        type=float,
        default=config.get("feed_stale", 10),
        help="How long the AFK feed may stay silent across a moment when it had to report — a "
        "startup, a resume, someone answering a prompt — before it is declared dead in the log "
        "and in a dialog. No feed means no prompts at all (default: from config or 10; 0 disables).",
    )
    parser.add_argument(
        "--presence-timeout",
        type=float,
        default=config.get("presence_timeout", 300),
        help="Seconds a not-afk event keeps meaning you are present after it stopped growing "
        "(default: from config or 300). Set it above the quiet time your AFK feed shows while "
        "you are actually there, or a present user gets called absent.",
    )
    parser.add_argument(
        "--presence-bucket",
        action="append",
        dest="presence_buckets",
        default=None,
        help="Bucket name (substring) whose events prove a human is around, used to tell a dead "
        "AFK feed from an absence. Repeatable; overrides the config list when given. Only name "
        "watchers that stay silent while you are away — one that heartbeats will cry wolf.",
    )
    parser.add_argument(
        "--display-wait",
        type=float,
        default=config.get("display_wait", aw_dialog.DISPLAY_WAIT_MINUTES),
        help="How many minutes to wait for the display server at startup before giving up "
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
    if args.presence_buckets is None:
        args.presence_buckets = config.get("presence_buckets", [])

    # Set up logging
    setup_logging(
        WATCHER_NAME,
        testing=args.testing,
        verbose=args.verbose,
        log_stderr=True,
        log_file=True,
    )

    # Nothing here can prompt without a display server, and when started with the
    # graphical session we may well be up before the compositor is. Wait for it
    # (logging why we are waiting) instead of dying and being restarted.
    aw_dialog.wait_for_display(args.display_wait)

    # A Tk without Xft renders the dialogs in a bitmap font and silently refuses
    # non-ASCII input, which is easy to mistake for a bug in this watcher.
    aw_dialog.warn_on_degraded_tk()

    # Test dialog mode - show dialog immediately for UI testing
    if args.test_dialog:
        from datetime import UTC, datetime, timedelta

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
                client,
                enable_lid_events=config.get("enable_lid_events", True),
                history_limit=args.history_limit,
                presence_buckets=args.presence_buckets,
                presence_timeout=args.presence_timeout,
            )
            logger.info("Successfully connected to the server.")

            # Backfill mode: on startup, prompt for old unfilled AFK periods.
            # last_deep_scan tracks when we last did a full backfill-depth lookup so the
            # normal loop can repeat it periodically (see DEEP_SCAN_INTERVAL_SECONDS).
            # snoozed_until: no prompts before this time (the watcher keeps scanning).
            last_deep_scan = 0.0
            snoozed_until = 0.0
            prompt_timeout_ms = _timeout_ms(args.prompt_timeout)

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
                    timeout_ms=prompt_timeout_ms,
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
            # The AFK feed check is armed now (this watcher just started) and
            # re-armed after every suspend; see _check_afk_feed.
            feed = _FeedState.armed(args.feed_stale, "this watcher started")
            clocks = _Clocks.now()
            # Track which ongoing AFK period we've already shown a pre-emptive dialog for,
            # identified by its start timestamp. Reset when a new AFK period starts.
            prompted_ongoing_start = None
            while True:
                try:
                    # A resume means the machine is up and time has passed, so a
                    # live feed is about to report -- the one moment its silence
                    # is worth anything. Re-arm the check for it.
                    clocks, before = _Clocks.now(), clocks
                    if _resumed(before, clocks):
                        logger.info(
                            f"Resumed after {format_duration((clocks.wall - before.wall).total_seconds())} suspended."
                        )
                        feed = _FeedState.armed(args.feed_stale, "the machine resumed", clocks.wall)

                    # Before doing the work: is there even a feed to find AFK
                    # periods in? Everything below is a no-op without one.
                    feed = _check_afk_feed(state, args.feed_stale, feed)

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
                                timeout_ms=prompt_timeout_ms,
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
                                timeout_ms=prompt_timeout_ms,
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
