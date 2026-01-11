# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/tobixen/aw-watcher-afk-prompt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tobixen/aw-watcher-afk-prompt/releases/tag/v0.1.0
