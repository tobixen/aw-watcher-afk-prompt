# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **AFK periods could be silently lost when the laptop lid was open**: aw-watcher-lid posts a single "not-afk" event spanning the entire lid-open period, regardless of whether anyone is at the keyboard. Once such an event was finalized (on the next lid close), it covered any unanswered AFK gap inside it, making the gap invisible to gap detection forever. Lid events now only contribute AFK evidence (lid closed / suspend), never presence.
- Answering the live "still AFK" dialog now stamps the period as ending when the afk watcher saw you return, rather than when you got around to clicking OK (which could overstate the period by minutes).
- Already-answered gaps are no longer re-adjusted (and re-logged with "Advancing gap start …" at INFO level) on every 5-second poll.

### Changed

- The **Cancel button is now called Snooze**, which is what it always did: close the dialog without an answer and re-ask later. Escape and an empty Enter do the same. An unanswered dialog now stays open until you act on it — there is no longer a timeout that auto-snoozes it, so a prompt can no longer be silently lost while you are away.
- Snoozing no longer blocks the whole watcher in a 5-minute `time.sleep`. Instead, prompts are suppressed for 5 minutes while scanning continues in the background, so AFK periods that begin or end during the snooze are still tracked.
- Snoozing a prompt now also stops the rest of the prompt queue (previously the next queued dialog popped up immediately after the 5-minute freeze). The remaining periods are re-presented after the snooze expires.

### Added

- Earlier unfilled AFK periods are now always prompted oldest-first, even while you are still away. When the live "still AFK" dialog would otherwise appear, the full backfill window is scanned first; any earlier *completed* periods are asked about oldest-first (each with "(N of total) — next: …" queue info), and the live dialog for the still-running period is shown only once those are cleared. Previously the live dialog appeared first (anchored at your most recent brief touch) and merely warned that earlier periods were pending.

- A live-updating check-in dialog can now appear *before* you return to the computer, showing a ticking "time away so far" counter for the ongoing AFK period. It offers the same Split button as the normal prompt (clicking Split assumes the period ends now). When you come back — either by typing into the dialog or by the OS afk watcher detecting activity, even without touching the dialog — it freezes the counter and drops the "still AFK" wording.
- The full backfill-depth (e.g. 24 h) scan now also runs during normal operation — on a ~10-minute cadence (configurable via `backfill_interval`) and immediately before prompting — instead of only at startup. Missed AFK periods are picked up within ~10 minutes rather than waiting for the next restart. Prompts are always shown oldest-first with queue info ("(N of total) — next: …") so it's clear when more periods remain to be backfilled.
- The check-in prompt now shows how long ago the AFK period ended (e.g. "5 minutes ago"), with a ⚠️ warning symbol for old periods (older than `stale_warning` minutes, default 15), so it's obvious when you're being prompted about a stale interval.
- Log at INFO level when the prompt dialog is cancelled (previously silent, making it impossible to audit missed gaps from the journal).
- Log a WARNING when an AFK gap expires from the depth window without being answered.
- Advance the detected AFK gap start to the idle-timeout event when window activity exists during the 2-minute idle countdown, preventing the countdown window from being double-counted as both work and AFK time.
- Retry posting events on transient server/network errors (ConnectionError, HTTP 5xx) with exponential backoff (up to 3 attempts), preventing re-prompting the user for the same gap after a momentary server hiccup.

### Fixed

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

[Unreleased]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.1...v0.1.4
[0.1.1]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tobixen/aw-watcher-afk-prompt/releases/tag/v0.1.0
