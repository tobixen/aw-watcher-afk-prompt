"""Configuration management for aw-watcher-afk-prompt."""

from aw_core.config import load_config_toml

DEFAULT_CONFIG = """
# Number of minutes to look into the past for events (for real-time prompting)
depth = 10.0

# Number of seconds to wait before checking for AFK events again
frequency = 5.0

# Number of minutes you need to be away before reporting on it
length = 5.0

# Enable integration with aw-watcher-lid for lid/suspend events
# OPTIONAL: Requires aw-watcher-lid to be installed and running
# See: https://github.com/tobixen/aw-watcher-lid
# When enabled, you'll be prompted about lid closures in addition to regular AFK
enable_lid_events = true

# Number of events to fetch from each bucket (AFK and lid)
# Increase this if you have long AFK periods with many heartbeat events
history_limit = 100

# Enable backfill mode - prompt for old unfilled AFK periods on startup
# When enabled, you'll be asked about AFK periods that were missed
enable_backfill = true

# How far back (in minutes) to look for unfilled AFK periods in backfill mode
# Default: 1440 (24 hours)
backfill_depth = 1440

# How often (in minutes) to repeat the full backfill-depth scan during normal
# operation, in addition to scanning right before prompting. Lower values catch
# missed AFK periods sooner at the cost of slightly more frequent server queries.
# Default: 10
backfill_interval = 10

# AFK periods older than this (in minutes) are flagged with a ⚠️ warning symbol
# in the prompt, so it's obvious when you're being asked about a stale interval.
# Default: 15
stale_warning = 15

# Hide a prompt nobody answered after this many minutes and ask again later.
# Prevents a dialog from being buried under other windows and forgotten: it comes
# back re-scanned, with an updated age and queue count. Typing restarts the
# countdown, and the "still AFK" dialog only starts it once you're back.
# Default: 5 (0 disables the timeout).
prompt_timeout = 5

# Give the AFK feed (whatever writes the aw-watcher-afk bucket) this many minutes
# to report something after each moment when it had to have something to say:
# this watcher starting, the machine resuming from suspend, aw-watcher-lid seeing
# you arrive, you answering a prompt, or another watcher reporting activity (see
# presence_buckets). If it stays silent across such a moment it has died — and
# since AFK periods are found in that feed, nothing would ever be asked about
# again. You get a dialog saying so instead of discovering a whole night went
# unrecorded.
# Not a general staleness timeout: the feed is *meant* to be silent while you are
# away, so silence only means anything measured against one of those moments.
# Default: 10 (0 disables the check).
feed_stale = 10

# Buckets whose events count as "a human is doing something", used only to tell a
# dead AFK feed from an ordinary absence (see feed_stale): if one of these reports
# activity and the AFK feed says nothing, the feed is most likely dead. Matched as
# substrings of the bucket name. The AFK, window, lid and own buckets are never
# used, however they are matched.
#
# Empty by default, and worth checking your own data before filling it in. Only
# list watchers that fire for a *person*: one that heartbeats or reacts to
# background work will report a dead feed while you are merely away, and the
# dialog blocks prompting until someone dismisses it. Measured on one real setup,
# aw-watcher-web-chrome emitted an event every 90 seconds all night on a sleeping
# machine — enough to convict a perfectly healthy feed within minutes. To check a
# candidate, query it over a period you know you were away and see whether it
# says anything:
#
#   curl -s "localhost:5600/api/0/buckets/<bucket>/events?limit=100&start=<from>&end=<to>"
#
# A bucket that is silent while you are gone is good evidence; one that is not
# will cry wolf.
presence_buckets = []

# How long (in minutes) to keep waiting for the display server at startup before
# giving up and exiting. A watcher started alongside the graphical session may
# well come up before the compositor does.
# Default: 15
display_wait = 15

# How long (in seconds) a not-afk event keeps meaning "you are at the keyboard"
# after it stopped growing. A live AFK watcher keeps extending its newest event,
# so once one stops growing you have either left or the watcher has died — either
# way, this watcher should stop assuming you are present.
# Set it above your feed's quiet-time-while-present: aw-watcher-window-wayland
# reports in ~85-second chunks with gaps of up to ~2 minutes. Too short and a
# present user is called absent, and the live "still AFK" dialog turns up in front
# of somebody who is demonstrably sitting there.
# Default: 300
presence_timeout = 300

# Minimum duration (in seconds) for a not-afk event to count as "real" activity.
# Not-afk events shorter than this are ignored, so brief laptop touches don't
# split a long AFK period into smaller ones that fall below the threshold.
# Default: 0 (disabled).
min_active = 0
""".strip()


def load_config() -> dict:
    """Load configuration using ActivityWatch standard approach.

    Config location: ~/.config/activitywatch/aw-watcher-afk-prompt/aw-watcher-afk-prompt.toml
    """
    return load_config_toml("aw-watcher-afk-prompt", DEFAULT_CONFIG)
