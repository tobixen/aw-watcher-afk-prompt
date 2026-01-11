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
""".strip()


def load_config() -> dict:
    """Load configuration using ActivityWatch standard approach.

    Config location: ~/.config/activitywatch/aw-watcher-afk-prompt/aw-watcher-afk-prompt.toml
    """
    return load_config_toml("aw-watcher-afk-prompt", DEFAULT_CONFIG)
