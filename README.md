# AW Watcher AFK Prompt

[![PyPI - Version](https://img.shields.io/pypi/v/aw-watcher-afk-prompt.svg)](https://pypi.org/project/aw-watcher-afk-prompt)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/aw-watcher-afk-prompt.svg)](https://pypi.org/project/aw-watcher-afk-prompt)

---

This [ActivityWatch](https://activitywatch.net) "watcher" prompts you with a dialog when you return from an AFK (away from keyboard) break, asking what you were doing.

## Installation

Install through pip or pipx or clone the repository and install using the Makefile:

```console
git clone https://github.com/tobixen/aw-watcher-afk-prompt.git
cd aw-watcher-afk-prompt
make install
```

For a complete setup including systemd service:
```console
make install-all
```

## Running

### Recommended: Using aw-qt

The recommended way to run this watcher is through [aw-qt](https://github.com/ActivityWatch/aw-qt), which manages both the server and watchers automatically. After installing aw-watcher-afk-prompt, aw-qt should detect and start it automatically.

### Alternative: Manual Start

If not using aw-qt, you can run it manually:
```console
aw-watcher-afk-prompt
```

Make sure `aw-server` and `aw-watcher-afk` are running first, as this watcher monitors AFK events.

### Alternative: systemd (Linux)

For users not using aw-qt who want automatic startup via systemd:

**Quick setup with Makefile:**
```console
make enable-service
```

**For Wayland users, also run:**
```console
make setup-wayland
```

This automatically configures your compositor to import the WAYLAND_DISPLAY environment variable.

## Configuration

The watcher can be configured via a config file or command-line arguments. Command-line arguments override config file settings.

### Config File

Configuration is stored in the ActivityWatch standard location:
```
~/.config/activitywatch/aw-watcher-afk-prompt/aw-watcher-afk-prompt.toml
```

The config file is created automatically with default values on first run if it doesn't exist.

### Command-line Arguments

You can override config file settings using command-line arguments:
```console
aw-watcher-afk-prompt --depth 15 --frequency 10 --length 3
```

Available options:
- `--depth`: Minutes to look into the past for events (default: from config or 10)
- `--frequency`: Seconds between AFK event checks (default: from config or 5)
- `--length`: Minimum AFK minutes before prompting (default: from config or 5)
- `--testing`: Run in testing mode
- `--verbose`: Enable verbose logging

### Lid Watcher Integration (Optional)

This watcher can integrate with [aw-watcher-lid](https://github.com/tobixen/aw-watcher-lid) to also prompt you about laptop lid closures and system suspends, not just regular AFK periods.

**Why use both watchers?**

Lid events may sometimes be more accurate than the ordinary afk watcher (particularly for short-lived afk events), but won't catch cases where the laptop is left with the lid open, as well as when someone leaves the desktop computer.

**Setup:**
1. Install aw-watcher-lid: `pipx install aw-watcher-lid`
2. Start it (see [aw-watcher-lid README](https://github.com/tobixen/aw-watcher-lid#readme) for setup)
3. aw-watcher-afk-prompt will automatically detect and use it

**To disable lid integration:**
I.e. if having an external keyboard it's possible to close the lid without being AFK.  Set `enable_lid_events = false` in your config file - or skip installing the aw-watcher-lid.

## Features

### Split AFK Periods

Quite often one ends up doing multiple tasks in my afk periods, for instance it could be "lunch" for 20 minutes and "phone call" for 10 minutes.  The pop-up dialog has a **Split** button for splitting the afk time on multiple event lines.  You can add as many lines as needed and then edit either the start time or the duration of the event lines.

## Contributing

Here are some helpful links:

- [How to create an ActivityWatch watcher](https://docs.activitywatch.net/en/latest/examples/writing-watchers.html).
- ["Manually tracking away/offline-time" forum discussion](https://forum.activitywatch.net/t/manually-tracking-away-offline-time/284)

## History

This project was originally created by [Jeremiah England](https://github.com/Jeremiah-England) as `aw-watcher-ask-away`. It was forked and renamed by [Tobias Brox](https://github.com/tobixen) to `aw-watcher-afk-prompt` in 2026 due to inactive maintainer of the original project (see https://github.com/Jeremiah-England/aw-watcher-ask-away/issues/8).

## License

`aw-watcher-afk-prompt` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
