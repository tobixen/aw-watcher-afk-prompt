# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A dialog when the AFK feed itself is dead.** Every prompt comes from a gap *between* not-afk events, so when whatever writes the `aw-watcher-afk` bucket dies, this watcher detects nothing and — until now — said nothing: a laptop could sit idle all night and produce no prompt in the morning, with nothing in the log but cheerful silence. The feed is now given `feed_stale` minutes (default 10, `0` disables) to report something after each moment where a live feed *must* have something to say: this watcher starting, the machine resuming from suspend, aw-watcher-lid seeing you arrive (a lid opened or a resume — a separate process, so its word is independent), you answering a prompt (somebody is demonstrably at the keyboard), and another watcher reporting activity the feed missed (`presence_buckets`, empty by default — only name watchers that stay silent while you are away, since one that heartbeats will report a dead feed while you are merely absent). If the feed stays silent across such a moment it is declared dead, in the log and in a dialog naming both the bucket to go and look at and which of those moments caught it. All but the first of those moments also catch a feed that dies mid-session. Note that this is *not* a general staleness timeout, and cannot be: the feed says nothing at all while you are away, so silence on its own describes every lunch break as well as every dead watcher.

### Fixed

- **A stale not-afk event is no longer read as "you are at the keyboard".** The check for whether you are currently away only asked what the newest event's status was, so as long as the AFK feed's last word was a not-afk event the watcher believed you were sitting there — for 20 hours, in the case that prompted this. Nothing was asked about, and the live "still AFK" dialog never appeared either. A not-afk event now only means presence while it is still growing (`presence_timeout`, default 300 seconds — well above the ~2 minutes a live feed can go quiet between reports; raise it if yours is slower). Coming back to a machine whose AFK feed died therefore gets you the ongoing-period prompt, and users of a feed that emits few explicit afk events (aw-watcher-window-wayland) get the live "still AFK" dialog for the first time.
- **The watcher no longer dies when the display server isn't up yet.** The hidden Tk root was created at import time, so starting before the compositor killed the process inside `import`, before logging was even configured — 92 identical tracebacks in one observed reboot. The root is now created on demand, and startup waits for the display server (retrying every 5s for `display_wait` minutes, default 15) with the reason in the log. The bundled systemd unit also sets `StartLimitIntervalSec=0`: systemd's default rate limit is what turns a transient startup failure into a permanently dead watcher, which is exactly how the Wayland window watcher — and with it the whole AFK feed — stayed dead after a reboot.

## [0.2.0] - 2026-08-12

### Added

- Earlier unfilled AFK periods are now always prompted oldest-first, even while you are still away. When the live "still AFK" dialog would otherwise appear, the full backfill window is scanned first; any earlier *completed* periods are asked about oldest-first (each with "(N of total) — next: …" queue info), and the live dialog for the still-running period is shown only once those are cleared. Previously the live dialog appeared first (anchored at your most recent brief touch) and merely warned that earlier periods were pending.
- A live-updating check-in dialog can now appear *before* you return to the computer, showing a ticking "time away so far" counter for the ongoing AFK period. It offers the same Split button as the normal prompt (clicking Split assumes the period ends now). When you come back — either by typing into the dialog or by the OS afk watcher detecting activity, even without touching the dialog — it freezes the counter and drops the "still AFK" wording. Answering it stamps the period as ending when the afk watcher saw you return, not when you got around to clicking OK. Returning briefly and leaving again also counts as coming back: that starts a *new* AFK period, and the dialog for the old one stops ticking instead of presenting a ten-minute absence as hours of continuous AFK time.
- The full backfill-depth (e.g. 24 h) scan now also runs during normal operation — on a ~10-minute cadence (configurable via `backfill_interval`) and immediately before prompting — instead of only at startup. Missed AFK periods are picked up within ~10 minutes rather than waiting for the next restart. Prompts are always shown oldest-first with queue info ("(N of total) — next: …") so it's clear when more periods remain to be backfilled.
- The check-in prompt now shows how long ago the AFK period ended (e.g. "5 minutes ago"), with a ⚠️ warning symbol for old periods (older than `stale_warning` minutes, default 15), so it's obvious when you're being prompted about a stale interval. The age keeps counting while the dialog waits, so a prompt left open doesn't insist the period ended "2 minutes ago" an hour later.
- Log at INFO level when the prompt dialog is cancelled (previously silent, making it impossible to audit missed gaps from the journal).
- Log a WARNING when an AFK gap expires from the depth window without being answered.
- Advance the detected AFK gap start to the idle-timeout event when window activity exists during the 2-minute idle countdown, preventing the countdown window from being double-counted as both work and AFK time. Whether a period is long enough to ask about is still judged on the full gap, so a period just above `length` is still prompted about even though it now reports a couple of minutes less.
- Retry posting events on transient server/network errors (ConnectionError, HTTP 5xx) with exponential backoff (up to 3 attempts), preventing re-prompting the user for the same gap after a momentary server hiccup.

### Changed

- The "(N of total)" queue info in prompts is now live instead of a frozen snapshot: the still-running AFK period is counted into the total (a lone completed gap announces "(1 of 2) — next: the current AFK period (still ongoing)" instead of pretending to be the only open interval), and the queue is re-scanned after every answer *and once a minute while a dialog is open*, so AFK periods that completed while a dialog sat unanswered join the same queue run rather than surfacing minutes later as a stand-alone surprise prompt. Going away and coming back a couple of times without answering turns "(1 of 1)" into "(1 of 3)" in the dialog you are looking at.
- The **Cancel button is now called Snooze**, which is what it always did: close the dialog without an answer and re-ask later. Escape and an empty Enter do the same.
- The timeout that hides an unanswered prompt is now configurable as `prompt_timeout` / `--prompt-timeout` and defaults to 5 minutes instead of 2 (`0` disables it). The prompt comes back re-scanned, with an updated age and queue count, so it cannot be buried under other windows and forgotten. Typing restarts the countdown, and no dialog starts counting down while the afk watcher says you are away — so a prompt raised while you were gone is still there waiting when you get back.
- `make install` now builds against a **system Python** (`uv tool install --no-managed-python`). The interpreters uv downloads ship a Tk without Xft, which draws the dialogs in a bitmap font and makes it impossible to type non-ASCII characters (æøå) into them. The watcher also logs a warning at startup when it detects such a Tk.
- Snoozing no longer blocks the whole watcher in a 5-minute `time.sleep`. Instead, prompts are suppressed for 5 minutes while scanning continues in the background, so AFK periods that begin or end during the snooze are still tracked.
- Snoozing a prompt now also stops the rest of the prompt queue (previously the next queued dialog popped up immediately after the 5-minute freeze). The remaining periods are re-presented after the snooze expires.

### Fixed

- **AFK periods could be silently lost when the laptop lid was open**: aw-watcher-lid posts a single "not-afk" event spanning the entire lid-open period, regardless of whether anyone is at the keyboard. Once such an event was finalized (on the next lid close), it covered any unanswered AFK gap inside it, making the gap invisible to gap detection forever. Lid events now only contribute AFK evidence (lid closed / suspend), never presence.
- Gaps that expired from the depth window (too old to prompt about) were never marked as seen, causing them to be re-reported as expired on every poll cycle (~5s) indefinitely. This blocked new gaps from ever being detected and prompted about.

## [0.1.6] - 2026-03-07

### Changed

- Work on pyproject.toml, github workflows, precommit, Makefile to adhere to what I consider "best current practices"
- "black syle" replaced with "ruff"

### Fixed

- Split dialog: editing a start time now interprets the entered HH:MM value in
  local time rather than UTC.  Previously `old_start.replace(hour, minute)` was
  applied to the stored UTC datetime, placing the new start time 1–2 hours in
  the future (depending on UTC offset) and producing a spurious "Adjusted
  duration would make last activity less than 1 minute" error.

## [0.1.5] - 2026-02-07

### Fixed

- Use `total_seconds()` instead of `timedelta.seconds` for AFK duration check — `.seconds` drops the days component, causing AFK periods >= 24 hours to be missed
- Dynamic limit scaling now requires at least 2 non-afk events (was 1), since `pairwise` gap detection needs events on both sides of a gap
- `ConnectionError` from server downtime no longer crashes the watcher — the main loop now retries and shows a warning dialog after 5 minutes of continuous unreachability

## [0.1.4] - 2026-01-12

### Fixed

- Remove auto-generated `_version.py` from git tracking
- Improve tag fetching for version detection in CI

## [0.1.1] - 2026-01-12

### Added

- GitHub Actions for CI and PyPI publishing

### Fixed

- Exclude old ask-away bucket and add error logging
- Resolve linting issues
- Add virtual display for tkinter tests in CI

## [0.1.0] - 2026-01-11

First release under the new name `aw-watcher-afk-prompt`. This release includes
all improvements made in the tobixen fork of the original `aw-watcher-ask-away`.

### Added

- Reusable `EnhancedEntry` widget with keyboard shortcuts (Ctrl+Backspace, Ctrl+w)
- Config file support using `aw_core.config` for persistent settings
- Optional lid watcher integration to detect laptop lid open/close events
- Split AFK period feature - split a single AFK period into multiple activities
- Backfill mode - prompt for old unfilled AFK periods on startup
- Persistent seen events store to avoid re-prompting for already-handled events
- Edit mode (`--edit`) to review and edit past entries
- Batch edit dialog to edit multiple entries at once
- Dynamic limit scaling for long AFK periods
- Human-readable duration display (hours/days instead of just minutes)
- Makefile for easier installation
- Systemd service file for running as a service

### Changed

- **Project renamed from `aw-watcher-ask-away` to `aw-watcher-afk-prompt`**
- Use `queued=True` for bucket creation for reliability during server outages
- Dynamic versioning using git tags (via hatch-vcs)
- Updated Python support to 3.11, 3.12, 3.13

### Fixed

- Use local timezone and locale-aware time format in dialogs
- IndexError when history is empty

[Unreleased]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.1...v0.1.4
[0.1.1]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tobixen/aw-watcher-afk-prompt/releases/tag/v0.1.0
